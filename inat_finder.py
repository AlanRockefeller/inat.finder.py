#!/usr/bin/env python3
"""
iNaturalist Observation Finder

Version 1.7.5 - By Alan Rockefeller - August 30, 2026

This script helps find the correct iNaturalist observation number when there are mistyped digits.
It works by systematically changing digits of the provided observation number and checking if
any of those variations match the specified genus, family, taxon ID, or username in the
iNaturalist database.

If the observation number has fewer than 9 digits, it will also try inserting one or two missing
digits at any position. Longer numbers are also tried with up to two digits removed, and adjacent
digits are swapped to catch transposition typos.

For very short numbers (5 digits or less), it will suggest that the number might be a
Mushroom Observer observation number instead.

The script can also parse observation numbers directly from iNaturalist URLs.

Usage:
    python inat_finder.py (--genus NAME | --family NAME | --taxon-id ID | --user USER | --project PROJECT) OBSERVATION [options]

Arguments:
    --genus <genus>         The genus name to match (e.g., "Galerina")
    --family <family>       The family name to match (e.g., "Amanitaceae")
    --taxon-id <id>         The iNaturalist taxon ID to match, at any rank (e.g., 48419).
                            Matches the taxon itself and every descendant of it. Use this
                            when several taxa share a name, when you already know the ID,
                            or to search a rank that --genus and --family cannot express.
    --user <username>       The iNaturalist username to match (e.g., "alan_rockefeller")
    --project <project>     The iNaturalist project to search within (ID, slug, URL, or title)
    observation_number_or_url  The potentially mistyped iNaturalist observation number
                               or a complete iNaturalist URL

Options:
    --digits N          Number of digits that might be wrong (default: 1)
    --verbose           Print detailed information about each attempt
    --no-progress       Hide the progress bar (progress bar is shown by default)
    --yes, -y           Assume "yes" at every confirmation prompt (never reads stdin)

Exit status:
    0   the search finished (whether or not matches were found)
    1   bad input, or the genus/family/taxon/user/project does not exist
    2   the search could not be completed because iNaturalist could not be reached
    130 the search was interrupted with Ctrl+C

    Command-line syntax errors caught by the argument parser - an unknown option, a
    missing observation number, or two conflicting search criteria - also exit 2,
    printing a usage message to stderr. A caller that needs to tell the two apart
    can check stderr: an API failure writes its explanation to stdout instead.
"""

import argparse
import itertools
import re
import sys
import textwrap
import time
import urllib.parse
from collections import namedtuple
from datetime import timedelta
from email.utils import parsedate_to_datetime

import requests
from tqdm import tqdm

VERSION = "1.7.5"
API_BASE_URL = "https://api.inaturalist.org/v1"
BATCH_SIZE = 200
REQUEST_TIMEOUT = 20
MAX_REQUEST_ATTEMPTS = 3
RATE_LIMIT_DELAY = 1.0
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
# Extra passes over batches that failed every attempt inside api_get().
BATCH_RETRY_ROUNDS = 1
# Stop pulling new lazy batches after this many batches fail both their initial
# request and all batch retry rounds. This bounds requests during a sustained
# outage while allowing isolated failures to recover.
MAX_CONSECUTIVE_FAILED_BATCHES = 4
LARGE_SEARCH_THRESHOLD = 5000
# Hard ceiling on how many candidates may be checked. Candidates are streamed, so
# this limit is about search time rather than memory, but it also keeps a mistyped
# --digits from starting a search that could never realistically finish.
MAX_SEARCH_CANDIDATES = 1_000_000
# The compatibility helper returns a real list, whose strings and references use
# substantially more memory than the streamed CLI search. Keep its ceiling lower.
MAX_EAGER_CANDIDATES = 100_000
OBSERVATION_FIELDS = "id,taxon,user,place_ids,place_guess"
PLACE_FIELDS = "id,name,admin_level,display_name"
# Exit status used when the API could not be reached, so a failed lookup or an
# incomplete search is never mistaken for a clean "nothing found" result.
API_FAILURE_EXIT_CODE = 2

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            f"inat-finder/{VERSION} "
            "(https://github.com/AlanRockefeller/inat.finder.py)"
        )
    }
)


class ApiError(RuntimeError):
    """An iNaturalist request could not be completed (network error or 5xx/429).

    This is deliberately distinct from a successful lookup that found nothing, so
    an outage is never reported to the user as "not found".
    """


class TaxonAmbiguityError(ValueError):
    """More than one distinct taxon exactly matched a requested name and rank."""

    def __init__(self, taxon_name, rank, candidates):
        self.taxon_name = taxon_name
        self.rank = rank
        self.candidates = tuple(candidates)
        super().__init__(
            f"{len(self.candidates)} distinct taxa match {rank} '{taxon_name}'"
        )


class RateLimiter:
    """Keep iNaturalist requests at roughly one per ``min_interval`` seconds.

    A single shared instance paces every API call the script makes, so validation
    lookups, the original-observation check, place lookups and the batched search
    all respect the same baseline instead of only delaying between batches.
    Explicit retry backoffs are routed through :meth:`sleep_for` so a backoff and
    the pacing delay never stack up into two sleeps for one request.
    """

    def __init__(self, min_interval=RATE_LIMIT_DELAY):
        self.min_interval = min_interval
        self._next_allowed = None

    def wait(self):
        """Sleep until the next request is allowed. Returns the seconds slept."""
        if self.min_interval <= 0 or self._next_allowed is None:
            return 0.0
        delay = self._next_allowed - time.monotonic()
        if delay <= 0:
            return 0.0
        time.sleep(delay)
        return delay

    def record_request(self):
        """Note that a request was just sent."""
        self._next_allowed = time.monotonic() + max(0.0, self.min_interval)

    def sleep_for(self, seconds):
        """Sleep an explicit backoff and count it as this request's pacing delay."""
        if seconds and seconds > 0:
            time.sleep(seconds)
        self._next_allowed = time.monotonic()

    def reset(self):
        """Forget the last request time (used by tests)."""
        self._next_allowed = None


RATE_LIMITER = RateLimiter()


def _retry_delay(response, attempt):
    """Return a retry delay, preferring a valid Retry-After header."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                now = parsedate_to_datetime(response.headers.get("Date", ""))
                return max(0.0, (retry_at - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2**attempt)


def api_get(path, **kwargs):
    """GET an iNaturalist API path, pacing and retrying requests.

    Rate limits and transient server errors are retried with backoff. Anything the
    caller can act on (including 404) is returned as a response; a request that
    could not be completed raises :class:`ApiError` instead of returning a value
    that could be mistaken for "no results".
    """
    url = path if path.startswith(("http://", "https://")) else API_BASE_URL + path
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        RATE_LIMITER.wait()
        try:
            response = SESSION.get(url, **kwargs)
        except requests.RequestException as error:
            RATE_LIMITER.record_request()
            if attempt + 1 == MAX_REQUEST_ATTEMPTS:
                raise ApiError(f"network error contacting {url}: {error}") from error
            RATE_LIMITER.sleep_for(float(2**attempt))
            continue

        RATE_LIMITER.record_request()
        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if attempt + 1 == MAX_REQUEST_ATTEMPTS:
            raise ApiError(
                f"iNaturalist returned HTTP {response.status_code} for {url} "
                f"after {MAX_REQUEST_ATTEMPTS} attempts"
            )
        RATE_LIMITER.sleep_for(_retry_delay(response, attempt))
    raise ApiError(f"could not complete request to {url}")


def api_get_json(path, allow_missing=False, **kwargs):
    """Return decoded JSON for an API path.

    Args:
        path: API path or full URL.
        allow_missing: When True a 404 returns None instead of raising, so callers
            can distinguish "definitely not there" from "could not look it up".

    Raises:
        ApiError: The request failed, or returned an unexpected status or body.
    """
    response = api_get(path, **kwargs)
    if allow_missing and response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ApiError(f"iNaturalist returned HTTP {response.status_code} for {path}")
    try:
        return response.json()
    except ValueError as error:
        raise ApiError(
            f"iNaturalist returned an unreadable response for {path}"
        ) from error


def unique_by_integer_value(seq):
    """Deduplicate digit strings by numeric ID while preserving first-seen order.

    Values that are not usable observation IDs (non-digits, or multi-digit values
    with a leading zero) are dropped.
    """
    seen = set()
    result = []
    for item in seq:
        if not isinstance(item, str) or not item.isdigit():
            continue
        if len(item) > 1 and item.startswith("0"):
            continue
        value = int(item)
        if value not in seen:
            seen.add(value)
            result.append(item)
    return result


def _is_valid_candidate(value):
    """True when a digit string is a usable observation ID (no leading zero)."""
    return bool(value) and (len(value) == 1 or not value.startswith("0"))


def parse_taxon_id_argument(value):
    """Return the positive integer taxon ID in ``value``, or None if it is invalid.

    Argparse cannot use ``type=int`` here: an invalid value must exit with the
    script's bad-input status 1, not argparse's own status 2. Zero, negative and
    non-numeric values are all rejected.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def parse_arguments():
    """
    Parses command-line arguments for the iNaturalist observation finder.

    This function sets up an argument parser to accept a genus, family, taxon ID,
    username, or project, and a
    potentially mistyped observation number (or URL). It also supports options to specify
    the number of digits that might be incorrect (default: 1), enable verbose output,
    disable the progress bar, and assume "yes" at every confirmation prompt. If no
    arguments are provided, the help message is printed and the program exits.

    Returns:
        argparse.Namespace: An object with attributes corresponding to the parsed arguments.
    """
    # Create a formatted description from the module docstring
    description = textwrap.dedent(__doc__)

    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,  # Use this to preserve formatting
    )
    search_group = parser.add_argument_group("search criteria (one required)")
    group = search_group.add_mutually_exclusive_group(required=True)
    group.add_argument("--genus", help="The genus name to match (e.g., 'Amanita')")
    group.add_argument(
        "--family", help="The family name to match (e.g., 'Amanitaceae')"
    )
    group.add_argument(
        "--taxon-id",
        dest="taxon_id",
        metavar="ID",
        help=(
            "The iNaturalist taxon ID to match at any rank (e.g., 48419). Matches "
            "the taxon itself and all of its descendants"
        ),
    )
    group.add_argument("--user", help="The iNaturalist username to match")
    group.add_argument(
        "--project",
        help="The iNaturalist project to search within (ID, slug, URL, or title)",
    )

    parser.add_argument(
        "observation_number",
        help="The potentially mistyped iNaturalist observation number or URL",
    )
    parser.add_argument(
        "--digits",
        type=int,
        default=1,
        help="Maximum number of digits that might be wrong (default: 1)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed information about each attempt",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Hide the progress bar (progress bar is shown by default)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Assume 'yes' at every confirmation prompt (never reads stdin)",
    )

    # If no arguments were provided, print help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()

    return args


def _replacement_digits(number_str, index, length):
    """Digits that may replace ``number_str[index]`` without a leading zero."""
    first_digit = 1 if index == 0 and length > 1 else 0
    return [
        str(digit)
        for digit in range(first_digit, 10)
        if str(digit) != number_str[index]
    ]


def iter_digit_variations(number_str, digits_off=1):
    """Yield replacement variations lazily, one candidate at a time.

    Candidates differ from the original in exactly one to ``digits_off`` positions,
    so every candidate is generated once and none equals the original number. This
    is a generator on purpose: the replacement space grows combinatorially and must
    never be materialized in full.
    """
    if digits_off <= 0:
        yield number_str
        return

    n = len(number_str)
    for num_changes in range(1, min(digits_off, n) + 1):
        for indices in itertools.combinations(range(n), num_changes):
            replacement_options = [
                _replacement_digits(number_str, index, n) for index in indices
            ]
            for replacements in itertools.product(*replacement_options):
                candidate = list(number_str)
                for index, replacement in zip(indices, replacements):
                    candidate[index] = replacement
                candidate = "".join(candidate)
                # Only reachable when the input itself has a leading zero.
                if _is_valid_candidate(candidate):
                    yield candidate


def count_digit_variations(number_str, digits_off=1):
    """Count replacement variations exactly, without generating them.

    The count is the number of strings differing from ``number_str`` in one to
    ``digits_off`` positions, honouring the rule that position 0 may never become
    a zero. It is computed with a small polynomial DP so that the search size is
    known before any candidate is built.
    """
    n = len(number_str)
    if digits_off <= 0 or n == 0:
        return 0

    # Coefficient k of ``counts`` is the number of variations changing k digits.
    counts = [1]
    for index in range(n):
        options = len(_replacement_digits(number_str, index, n))
        # A leading-zero input can only produce valid IDs by changing position 0.
        forced = index == 0 and n > 1 and number_str[0] == "0"
        updated = [0] * (len(counts) + 1)
        for changed, total in enumerate(counts):
            if not forced:
                updated[changed] += total
            updated[changed + 1] += total * options
        counts = updated

    return sum(counts[1 : min(digits_off, n) + 1])


def generate_digit_variations(number_str, digits_off=1):
    """
    Generate variations by altering up to a specified number of digits.

    This is the eager wrapper around :func:`iter_digit_variations`, kept for callers
    that want a list. The search itself streams candidates instead.

    Args:
        number_str (str): The original observation number.
        digits_off (int, optional): The maximum number of digits to change. Defaults to 1.

    Returns:
        List[str]: A list of unique observation number variations.
    """
    if digits_off <= 0:
        return [number_str]  # No variations if digits_off is 0 or negative
    candidate_count = count_digit_variations(number_str, digits_off)
    if candidate_count > MAX_EAGER_CANDIDATES:
        raise ValueError(
            f"generate_digit_variations() would eagerly materialize "
            f"{candidate_count} candidates, which is unsafe for this compatibility "
            f"helper (limit: {MAX_EAGER_CANDIDATES}). Use "
            "iter_digit_variations() to stream a search this large."
        )
    return unique_by_integer_value(iter_digit_variations(number_str, digits_off))


def iter_digit_insertions(number_str, max_added_digits=2):
    """
    Yield observation numbers with one or two missing digits inserted.

    One digit is tried at every position. When ``max_added_digits`` is at least 2,
    two digits are tried at every pair of positions - including two internal
    positions, not just the ends. Candidates are deduplicated by numeric value and
    values with a leading zero are skipped.

    Args:
        number_str: The original observation number as a string.
        max_added_digits: Maximum number of digits to add (default is 2).

    Yields:
        Unique numeric-ID variations as strings.
    """
    seen = set()

    def offer(value):
        """Return True the first time a usable candidate value is seen."""
        if not _is_valid_candidate(value):
            return False
        numeric = int(value)
        if numeric in seen:
            return False
        seen.add(numeric)
        return True

    one_inserted = [
        number_str[:position] + str(digit) + number_str[position:]
        for position in range(len(number_str) + 1)
        for digit in range(10)
    ]
    for candidate in one_inserted:
        if offer(candidate):
            yield candidate

    if max_added_digits >= 2:
        # Insert the second digit anywhere in each one-insertion result. Bases with
        # a leading zero are still expanded, because a second leading digit can make
        # them valid again (e.g. "0123" -> "50123").
        for base in one_inserted:
            for position in range(len(base) + 1):
                for digit in range(10):
                    candidate = base[:position] + str(digit) + base[position:]
                    if offer(candidate):
                        yield candidate


def generate_digit_additions(number_str, max_added_digits=2):
    """Eager wrapper around :func:`iter_digit_insertions`."""
    return list(iter_digit_insertions(number_str, max_added_digits))


def generate_digit_removals(number_str, max_removed_digits=2):
    """
    Generate observation number variations by removing digits.

    This function produces unique variations of the input observation number by removing a given
    number of digits from any position.

    Args:
        number_str (str): The original observation number.
        max_removed_digits (int, optional): The maximum number of digits to remove. Defaults to 2.

    Returns:
        List[str]: Unique numeric-ID variations without leading zeroes.
    """
    variations = set()
    n = len(number_str)

    if n == 0:
        return []

    # Determine how many digits to remove, from 1 to max_removed_digits
    for num_to_remove in range(1, min(max_removed_digits, n) + 1):
        # Find all combinations of indices to keep
        for indices_to_keep in itertools.combinations(range(n), n - num_to_remove):
            new_str = "".join(number_str[i] for i in indices_to_keep)
            if _is_valid_candidate(new_str):
                variations.add(new_str)

    return unique_by_integer_value(sorted(variations))


def generate_digit_transpositions(number_str):
    """
    Generate variations that swap two adjacent digits.

    Transposition is a common typing error (123456789 -> 123465789) and is cheap to
    cover: at most one candidate per digit boundary. Swaps of equal digits and
    results with a leading zero are skipped.

    Args:
        number_str (str): The original observation number.

    Returns:
        List[str]: Unique numeric-ID variations.
    """
    seen = set()
    variations = []
    for index in range(len(number_str) - 1):
        if number_str[index] == number_str[index + 1]:
            continue
        candidate = (
            number_str[:index]
            + number_str[index + 1]
            + number_str[index]
            + number_str[index + 2 :]
        )
        if not _is_valid_candidate(candidate):
            continue
        numeric = int(candidate)
        if numeric in seen:
            continue
        seen.add(numeric)
        variations.append(candidate)
    return variations


class CandidatePlan:
    """A deduplicated search space that is sized up front and streamed on demand.

    Replacement variations are counted exactly with :func:`count_digit_variations`
    and generated lazily, so a huge ``--digits`` value never builds a list. The much
    smaller insertion, removal and transposition classes are materialized once
    (a few thousand candidates at most) so the total is exact.

    The candidate classes cannot collide with each other: insertions and removals
    change the number's length, and transpositions - which do not - are only added
    when ``digits_off`` is below 2, because a two-digit replacement search already
    contains every adjacent swap. That means no observation ID is ever requested
    twice and ``total`` is the true number of API-checked candidates.
    """

    def __init__(self, number_str, digits_off, add_digits=True, remove_digits=True):
        self.number_str = number_str
        self.digits_off = digits_off
        self.replacement_count = count_digit_variations(number_str, digits_off)
        self.additions = []
        self.removals = []
        self.transpositions = []

        if digits_off > 0:
            if add_digits:
                self.additions = list(iter_digit_insertions(number_str, 2))
            if remove_digits:
                self.removals = generate_digit_removals(number_str, 2)
            if digits_off < 2:
                # With --digits 2 or more every adjacent swap is already a
                # two-digit replacement, so adding them would duplicate requests.
                self.transpositions = generate_digit_transpositions(number_str)

        seen = {int(number_str)} if number_str.isdigit() and number_str else set()
        self.extras = []
        for candidate in itertools.chain(
            self.additions, self.removals, self.transpositions
        ):
            numeric = int(candidate)
            if numeric in seen:
                continue
            seen.add(numeric)
            self.extras.append(candidate)

        self.total = self.replacement_count + len(self.extras)

    def __len__(self):
        return self.total

    def __iter__(self):
        if self.digits_off > 0:
            yield from iter_digit_variations(self.number_str, self.digits_off)
        yield from self.extras

    def describe(self):
        """Return human-readable lines describing where candidates come from."""
        lines = []
        if self.replacement_count:
            lines.append(
                f"{self.replacement_count} variations by changing up to "
                f"{self.digits_off} digit(s)"
            )
        if self.additions:
            lines.append(f"{len(self.additions)} variations by adding digits")
        if self.removals:
            lines.append(f"{len(self.removals)} variations by removing digits")
        if self.transpositions:
            lines.append(
                f"{len(self.transpositions)} variations by swapping adjacent digits"
            )
        return lines


def verify_user_exists(username):
    """
    Verify if a username exists on iNaturalist.

    Args:
        username: The username to verify.

    Returns:
        bool: True if the username exists, False if iNaturalist says it does not.

    Raises:
        ApiError: The lookup could not be completed, so existence is unknown.
    """
    data = api_get_json(
        f"/users/{urllib.parse.quote(username, safe='')}", allow_missing=True
    )
    if data is None:
        return False

    # Check if any user matches the exact username
    for user in data.get("results") or []:
        if isinstance(user, dict) and user.get("login", "").lower() == username.lower():
            return True

    return False


def find_taxon(taxon_name, rank):
    """
    Find an exact taxon of the requested rank in the iNaturalist taxonomy.

    The returned taxon includes the ID needed to match observations through their
    ``ancestor_ids`` taxonomy path.

    Args:
        taxon_name: The scientific taxon name to verify.
        rank: The exact iNaturalist rank to require.

    Returns:
        dict | None: The sole matching taxon, or None when no exact match is found.

    Raises:
        ApiError: The lookup could not be completed, so existence is unknown.
        TaxonAmbiguityError: Distinct taxon IDs share the requested name and rank.
    """
    requests_to_try = (
        ("/taxa/autocomplete", {"q": taxon_name, "rank": rank, "per_page": 30}),
        ("/taxa", {"q": taxon_name, "rank": rank, "per_page": 30}),
    )
    exact_matches = {}
    for path, params in requests_to_try:
        data = api_get_json(path, params=params)
        for result_index, taxon in enumerate(data.get("results") or []):
            if not isinstance(taxon, dict):
                continue
            name = taxon.get("name")
            if (
                isinstance(name, str)
                and name.casefold() == taxon_name.casefold()
                and taxon.get("rank") == rank
            ):
                taxon_id = taxon.get("id")
                # IDs are normally integers, but normalize strings defensively so
                # the same taxon returned by both endpoints is still one match.
                key = str(taxon_id) if taxon_id is not None else (path, result_index)
                exact_matches.setdefault(key, taxon)

        # Once one endpoint independently establishes ambiguity, another lookup
        # cannot make the distinct IDs unambiguous.
        if len(exact_matches) > 1:
            raise TaxonAmbiguityError(taxon_name, rank, exact_matches.values())

    if len(exact_matches) == 1:
        return next(iter(exact_matches.values()))
    return None


def find_taxon_by_id(taxon_id):
    """
    Look up a single taxon by its iNaturalist taxon ID.

    The returned taxon carries the metadata needed to describe the search target
    (name, rank, common name, iconic taxon) and the canonical ID used for the same
    ancestry-based matching that verified genus and family searches use.

    Args:
        taxon_id: A positive integer iNaturalist taxon ID.

    Returns:
        dict | None: The taxon record, or None when iNaturalist says the ID does
        not exist.

    Raises:
        ApiError: The lookup could not be completed, or the API returned a body
            that cannot be interpreted. An unreadable answer is never downgraded
            to "not found".
    """
    data = api_get_json(f"/taxa/{int(taxon_id)}", allow_missing=True)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ApiError(
            f"iNaturalist returned an unexpected response for taxon ID {taxon_id}"
        )

    results = data.get("results")
    if results is None or not isinstance(results, list):
        raise ApiError(
            f"iNaturalist returned an unexpected response for taxon ID {taxon_id}"
        )
    if not results:
        # A well-formed empty result really does mean the ID is not there.
        return None

    for taxon in results:
        if isinstance(taxon, dict) and str(taxon.get("id")) == str(taxon_id):
            return taxon

    raise ApiError(
        f"iNaturalist returned results that do not describe taxon ID {taxon_id}"
    )


def describe_taxon(taxon):
    """Return a short 'Name (rank)' label for a verified taxon record."""
    name = taxon.get("name") or "Unknown taxon"
    rank = taxon.get("rank")
    return f"{name} ({rank})" if rank else str(name)


def format_taxon_reference(taxon, taxon_id):
    """Return 'Name (taxon ID 48419)', or just the ID when the name is unknown."""
    name = (taxon or {}).get("name")
    if not name:
        return f"taxon ID {taxon_id}"
    return f"{name} (taxon ID {taxon_id})"


def parse_project_slug_from_url(project_input):
    """
    Extracts the project slug from an iNaturalist project URL.

    Args:
        project_input: A string that might be a project URL.

    Returns:
        The extracted slug string if found, or None.
    """
    # Look for 'projects/' pattern only if the input looks URL-like or mentions iNaturalist
    if "projects/" in project_input and (
        "//" in project_input or "inaturalist.org" in project_input
    ):
        match = re.search(r"projects/([^/?#]+)", project_input)
        if match:
            return match.group(1)
    return None


def search_projects_by_query(query):
    """
    Search for projects on iNaturalist by title or slug.

    Args:
        query: The search term (title or slug).

    Returns:
        A list of project dictionaries containing 'id', 'slug', and 'title'.

    Raises:
        ApiError: The search could not be completed.
    """
    data = api_get_json("/projects", params={"q": query, "per_page": 10})
    return data.get("results") or []


def resolve_project_identifier(project_input):
    """
    Resolves a project input string to a valid project ID/slug and metadata.

    Args:
        project_input: The input string (ID, slug, URL, or title).

    Returns:
        tuple: (project_id_or_slug, project_metadata_dict)

    Raises:
        ApiError: The project could not be looked up (network/API failure).

    Exits the program if the project is ambiguous or definitely does not exist.
    """
    slug_from_url = parse_project_slug_from_url(project_input)
    direct_identifier = project_input if project_input.isdigit() else slug_from_url
    if direct_identifier is None and " " not in project_input:
        direct_identifier = project_input

    # Numeric IDs and slug-like inputs can be resolved without fuzzy search.
    if direct_identifier:
        data = api_get_json(
            f"/projects/{urllib.parse.quote(direct_identifier, safe='')}",
            allow_missing=True,
        )
        results = (data or {}).get("results") or []
        if results:
            project = results[0]
            return str(project.get("id", direct_identifier)), project

        if project_input.isdigit():
            print(f"Error: Project ID '{project_input}' not found on iNaturalist.")
            sys.exit(1)
        if slug_from_url:
            print(f"Error: Project URL slug '{slug_from_url}' not found.")
            sys.exit(1)

    # 3. Determine if it's likely a title or a slug
    # Conservative slug detection:
    # - If contains spaces -> Title
    # - If all digits -> ID (handled above)
    # - Else -> Treat as Slug candidate, but verify exactly.
    #   If verification fails, fallback to title search.

    is_likely_slug = " " not in project_input

    candidates = search_projects_by_query(project_input)

    if not candidates:
        print(f"Error: Project '{project_input}' not found on iNaturalist.")
        sys.exit(1)

    # Try to find exact match
    project_input_lower = project_input.lower()
    exact_matches = [
        p
        for p in candidates
        if (is_likely_slug and p.get("slug", "").lower() == project_input_lower)
        or p.get("title", "").lower() == project_input_lower
    ]

    if len(exact_matches) == 1:
        p = exact_matches[0]
        # Prefer ID if available, else slug
        limit_param = str(p.get("id", p.get("slug")))
        return limit_param, p

    if len(exact_matches) > 1:
        # This shouldn't happen often for slugs, maybe for titles
        print(f"Found multiple exact matches for '{project_input}':")
        for p in exact_matches:
            print(f" - {p.get('title')} (ID: {p.get('id')}, Slug: {p.get('slug')})")
        print("Please use the specific ID or Slug.")
        sys.exit(1)

    # If no exact match, but we have candidates, show disambiguation
    print(f"No exact match found for '{project_input}', but found similar projects:")
    for p in candidates[:5]:
        print(f" - {p.get('title')} (ID: {p.get('id')}, Slug: {p.get('slug')})")
    print("\nPlease re-run with the specific Project ID or Slug.")
    sys.exit(1)


def preprocess_argv_for_project_name(argv, warn=print):
    """
    Pre-processes sys.argv to handle unquoted project names.

    Example: --project Coastal and Marine Mycology 2024 123456
    Becomes: --project "Coastal and Marine Mycology 2024" 123456

    A numeric token of five or more digits, or an observation URL, is always the
    observation number. A short trailing number is genuinely ambiguous (it could be
    a year in the title), so it is treated as the observation number and a note
    suggests quoting the title if that guess is wrong.

    Args:
        argv: List of command line arguments (usually sys.argv).
        warn: Callable used to report an ambiguous parse.

    Returns:
        Modified list of arguments.
    """
    if "--project" not in argv:
        return argv

    new_argv = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        new_argv.append(arg)
        i += 1

        if arg == "--project":
            # Collect a possibly unquoted title. Long IDs and observation URLs are
            # unambiguous boundaries; a final short numeric token is separated below.
            project_tokens = []
            stopped_at_observation = False
            while i < len(argv):
                next_arg = argv[i]

                # Stop if it's the observation number/URL
                # Rule: contains "observations/" OR (digits >= 5)
                is_obs = "observations/" in next_arg or (
                    next_arg.isdigit() and len(next_arg) >= 5
                )

                # Stop if it's a new flag
                is_flag = next_arg.startswith("-")

                if is_obs or is_flag:
                    stopped_at_observation = is_obs
                    break

                project_tokens.append(next_arg)
                i += 1

            # A short observation ID is otherwise indistinguishable from a year in
            # the middle of a title. At the end of the collected value it is the
            # required positional argument, so put it back into argv separately.
            trailing_observation = None
            if (
                not stopped_at_observation
                and project_tokens
                and (
                    project_tokens[-1].isdigit()
                    or re.search(r"observations/(\d+)", project_tokens[-1])
                )
            ):
                trailing_observation = project_tokens.pop()
                if project_tokens and trailing_observation.isdigit():
                    warn(
                        f"Note: reading '{trailing_observation}' as the observation "
                        f"number and '{' '.join(project_tokens)}' as the project title. "
                        "Quote the title if that number is part of it."
                    )

            # If we collected multiple tokens, join them.
            # If just one, it might be quoted or just a slug, effectively same result.
            if project_tokens:
                new_argv.append(" ".join(project_tokens))
            if trailing_observation:
                new_argv.append(trailing_observation)

    return new_argv


def parse_inat_url(url_or_number):
    """
    Extracts the observation number from an iNaturalist URL.

    If the input is a URL containing an observation number in the expected format,
    the function extracts and returns that observation number as a string.
    If the input does not match the URL pattern or is already an observation number,
    the original string is returned unchanged.

    Args:
        url_or_number: A string representing either an iNaturalist URL or an observation number.

    Returns:
        A string containing the extracted observation number, or the original input if no valid
        observation number is found.
    """

    if url_or_number.isdigit():
        return url_or_number
    match = re.search(r"observations/(\d+)", url_or_number)
    return match.group(1) if match else url_or_number


BatchCheckResult = namedtuple(
    "BatchCheckResult", ["observations", "unchecked", "failed_batches"]
)


def _iter_batches(candidates, batch_size):
    """Yield lists of at most ``batch_size`` candidates from any iterable."""
    batch = []
    for candidate in candidates:
        batch.append(candidate)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def fetch_observations(ids, project_id=None, batch_size=BATCH_SIZE):
    """Fetch observations by ID in a single request.

    Raises:
        ApiError: The request could not be completed.
    """
    params = {
        "id": ",".join(str(observation_id) for observation_id in ids),
        "per_page": batch_size,
        "fields": OBSERVATION_FIELDS,
    }
    if project_id:
        params["project_id"] = project_id
    data = api_get_json("/observations", params=params)
    return data.get("results") or []


def batch_check_observations(
    variations,
    batch_size=BATCH_SIZE,
    project_id=None,
    progress_callback=None,
    batch_callback=None,
    results_callback=None,
    message_callback=print,
    total=None,
    retry_rounds=BATCH_RETRY_ROUNDS,
    collect_results=True,
):
    """
    Check observation IDs by querying the iNaturalist API in batches.

    ``variations`` may be any iterable, including a lazy generator, so the whole
    search space never has to exist in memory at once. A failed batch is retried
    before another batch is pulled. Batches that still fail are counted as
    *unchecked*, and a sustained run of permanent failures stops the iterator
    early when its planned ``total`` is known.

    Args:
        variations: An iterable of observation ID strings to be verified.
        batch_size: Maximum number of observation IDs per API request (default 200).
        project_id: Optional project ID or slug to filter by.
        progress_callback: Optional callable ``(count, checked)`` where ``checked``
            is False for candidates whose request permanently failed.
        batch_callback: Optional callable receiving batch, start index, and total count.
        results_callback: Optional callable receiving each batch's observations as soon
            as they arrive, so callers can report matches while the search runs.
        message_callback: Callable used for request error messages.
        total: Known total number of candidates, for progress reporting and exact
            counting of candidates skipped by outage fail-fast. Sized iterables
            are counted automatically when this is omitted.
        retry_rounds: Extra passes over batches that failed.
        collect_results: Keep every returned observation in the result. Callers
            that consume ``results_callback`` should pass False, so a search over
            hundreds of thousands of candidates does not accumulate them all.

    Returns:
        BatchCheckResult: observations found, the number of candidates that could
        not be checked, and how many batches permanently failed.
    """
    all_results = []
    if total is None:
        total = getattr(variations, "total", None)
    if total is None:
        try:
            total = len(variations)
        except TypeError:
            pass

    unchecked = 0
    failed_count = 0
    consecutive_failures = 0
    start = 0

    def run_batch(batch, batch_start):
        """Return True when the batch was checked; False when its request failed."""
        if batch_callback:
            batch_callback(batch, batch_start, total)
        try:
            batch_results = fetch_observations(
                batch, project_id=project_id, batch_size=batch_size
            )
        except ApiError as error:
            message_callback(
                f"Error fetching batch: {error} "
                f"({len(batch)} candidate(s) not checked yet)"
            )
            return False

        if collect_results:
            all_results.extend(batch_results)
        if results_callback:
            results_callback(batch_results)
        if progress_callback:
            progress_callback(len(batch), True)
        return True

    for batch in _iter_batches(variations, batch_size):
        checked = run_batch(batch, start)
        for round_number in range(retry_rounds):
            if checked:
                break
            message_callback(f"Retrying failed batch (attempt {round_number + 2})...")
            checked = run_batch(batch, start)

        start += len(batch)
        if checked:
            consecutive_failures = 0
            continue

        unchecked += len(batch)
        failed_count += 1
        consecutive_failures += 1

        if total is not None and consecutive_failures >= MAX_CONSECUTIVE_FAILED_BATCHES:
            skipped = max(0, total - start)
            unchecked += skipped
            message_callback(
                f"Stopping after {consecutive_failures} consecutive batches "
                f"failed permanently; {skipped} planned candidate(s) will remain "
                "unchecked."
            )
            break

    if unchecked and progress_callback:
        progress_callback(unchecked, False)

    return BatchCheckResult(all_results, unchecked, failed_count)


def fetch_places(place_ids, batch_size=BATCH_SIZE, message_callback=print):
    """Resolve unique place IDs through the iNaturalist Places API in batches.

    Locations are cosmetic, so a failed lookup is reported and skipped rather than
    aborting the search; callers fall back to the observation's own place guess.
    """
    unique_ids = unique_by_integer_value([str(place_id) for place_id in place_ids])
    places = {}
    for start in range(0, len(unique_ids), batch_size):
        batch = unique_ids[start : start + batch_size]
        try:
            data = api_get_json(
                f"/places/{','.join(batch)}",
                params={"fields": PLACE_FIELDS, "per_page": batch_size},
            )
        except ApiError as error:
            message_callback(f"Error resolving locations: {error}")
            continue
        for place in data.get("results") or []:
            if isinstance(place, dict) and place.get("id") is not None:
                places[str(place["id"])] = place
    return places


def format_place_label(places):
    """Format the most specific standard administrative place as plain text."""
    administrative_places = [
        place
        for place in places
        if isinstance(place, dict)
        and isinstance(place.get("admin_level"), int)
        and place["admin_level"] >= 0
    ]
    if not administrative_places:
        return "Unknown location"

    most_specific = max(administrative_places, key=lambda place: place["admin_level"])
    label = most_specific.get("display_name") or most_specific.get("name")
    if not isinstance(label, str) or not label.strip():
        return "Unknown location"

    label = re.sub(r"\bCounty\b", "Co.", label)
    label = re.sub(r"\bUnited States\b", "US", label)
    return " ".join(part.strip() for part in label.split(",") if part.strip())


def resolve_observation_locations(observations, message_callback=print):
    """Return observation-ID-to-label mappings resolved from ``place_ids``.

    When the structured place lookup produces nothing usable - no standard place,
    or a failed places request - the observation's own ``place_guess`` text is used
    instead of reporting an unknown location.
    """
    place_ids = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        observation_place_ids = observation.get("place_ids") or []
        if isinstance(observation_place_ids, (list, tuple, set)):
            place_ids.extend(observation_place_ids)

    places_by_id = fetch_places(place_ids, message_callback=message_callback)
    labels = {}
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("id") is None:
            continue
        observation_place_ids = observation.get("place_ids") or []
        if not isinstance(observation_place_ids, (list, tuple, set)):
            observation_place_ids = []
        observation_places = [
            places_by_id[str(place_id)]
            for place_id in observation_place_ids
            if str(place_id) in places_by_id
        ]
        label = format_place_label(observation_places)
        if label == "Unknown location":
            place_guess = observation.get("place_guess")
            if isinstance(place_guess, str) and place_guess.strip():
                label = place_guess.strip()
        labels[observation["id"]] = label
    return labels


def _taxon_id_matches(taxon, target_taxon_id):
    """True when the observation's taxonomy contains ``target_taxon_id``."""
    target = str(target_taxon_id)
    if str(taxon.get("id")) == target:
        return True

    ancestor_ids = taxon.get("ancestor_ids") or []
    if not isinstance(ancestor_ids, (list, tuple, set)):
        ancestor_ids = []
    if any(str(ancestor_id) == target for ancestor_id in ancestor_ids):
        return True

    ancestors = taxon.get("ancestors") or []
    if not isinstance(ancestors, (list, tuple)):
        ancestors = []
    return any(
        isinstance(ancestor, dict) and str(ancestor.get("id")) == target
        for ancestor in ancestors
    )


def check_observation_taxon(
    observation, target_name, target_rank, target_taxon_id=None
):
    """
    Determine if an observation belongs to a specified taxon.

    When a verified ``target_taxon_id`` is available the decision is made purely by
    ID: the observation's taxon carries the full ``ancestor_ids`` path (which
    includes the taxon's own ID), so membership is decidable without guessing.
    Taxon names are not globally unique - the same genus name exists in different
    kingdoms - so a name/rank match is never allowed to override an ID mismatch.

    Name-based comparisons, including the genus name-prefix heuristic (matching
    "Amanita muscaria" against genus "Amanita"), are only used as a fallback when
    no verified taxon ID is available.

    Args:
        observation: A dictionary containing observation details with taxonomic information.
        target_name: The taxon name to match (case-insensitive).
        target_rank: The exact taxonomic rank to match.
        target_taxon_id: The verified iNaturalist taxon ID, when available.

    Returns:
        True if the observation's taxonomy includes the target taxon; otherwise, False.
    """
    if not isinstance(observation, dict) or not observation:
        return False

    taxon = observation.get("taxon") or {}
    if not isinstance(taxon, dict) or not taxon:
        return False

    if target_taxon_id is not None:
        return _taxon_id_matches(taxon, target_taxon_id)

    # No verified ID: fall back to exact name/rank comparisons.
    ancestors = taxon.get("ancestors") or []
    if not isinstance(ancestors, (list, tuple)):
        ancestors = []
    for ancestor in ancestors:
        ancestor_name = ancestor.get("name") if isinstance(ancestor, dict) else None
        if (
            isinstance(ancestor_name, str)
            and ancestor.get("rank") == target_rank
            and ancestor_name.lower() == target_name.lower()
        ):
            return True

    # Check the taxon itself
    name = taxon.get("name")
    if (
        taxon.get("rank") == target_rank
        and isinstance(name, str)
        and name.lower() == target_name.lower()
    ):
        return True

    # A species-level scientific name begins with its genus. This heuristic can
    # false-positive, so it is only used when no verified taxon ID is available.
    if target_rank == "genus" and isinstance(name, str) and " " in name:
        first_token = name.split(" ")[0]
        if first_token.lower() == target_name.lower():
            return True

    return False


def check_observation_genus(observation, target_genus, target_taxon_id=None):
    """Determine if an observation belongs to the specified genus."""
    return check_observation_taxon(observation, target_genus, "genus", target_taxon_id)


def check_observation_family(observation, target_family, target_taxon_id=None):
    """Determine if an observation belongs to the specified family."""
    return check_observation_taxon(
        observation, target_family, "family", target_taxon_id
    )


def check_observation_taxon_id(observation, target_taxon_id):
    """Determine if an observation belongs to a taxon ID or any of its descendants.

    This deliberately delegates to :func:`check_observation_taxon` with no name or
    rank, so explicit ``--taxon-id`` searches use exactly the same strict, ID-only
    ancestry test as verified genus and family searches, and never fall back to a
    taxon-name heuristic.
    """
    return check_observation_taxon(observation, None, None, target_taxon_id)


def check_observation_user(observation, target_username):
    """
    Determine if an observation was created by the specified username.

    This function checks if the user who created the observation matches the
    provided username. It performs a case-insensitive comparison between the
    target username and the observation's user login name.

    Args:
        observation: A dictionary containing observation details including user information.
        target_username: The username to match (case-insensitive).

    Returns:
        True if the observation was created by the target user; otherwise, False.
    """
    if not isinstance(observation, dict) or not observation:
        return False

    user = observation.get("user") or {}
    if not isinstance(user, dict):
        return False

    login = user.get("login")
    if login and isinstance(login, str):
        return login.lower() == target_username.lower()

    return False


def get_user_confirmation(prompt, default_yes=False, assume_yes=False):
    """
    Get yes/no confirmation from user with better input handling.

    Args:
        prompt: The question to ask the user
        default_yes: If True, empty input defaults to 'yes'
        assume_yes: If True, answer 'yes' immediately without reading stdin

    Returns:
        bool: True if user confirms, False otherwise
    """
    if assume_yes:
        print(f"{prompt}y")
        return True

    while True:
        try:
            response = input(prompt)
        except EOFError:
            # Non-interactive stdin / no input available: fall back to default
            return default_yes
        except KeyboardInterrupt:
            print("\nSearch cancelled.")
            raise SystemExit(130)

        response = response.strip().lower()

        if not response:
            return default_yes

        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        else:
            print("Please enter 'y' or 'n'")


def run_search():
    """
    Executes the iNaturalist observation finder process.

    This function orchestrates the search for valid iNaturalist observations by:
    - Parsing command-line arguments and extracting an observation number from a URL or plain input.
    - Verifying that the specified genus, family, username, or project exists in iNaturalist.
    - Validating the observation number and warning the user if it appears too short (suggesting a possible Mushroom Observer observation).
    - Optionally confirming whether the original observation number already matches the criteria.
    - Sizing the candidate search space before generating it, and refusing or confirming large searches.
    - Streaming candidates through batched API calls and displaying progress.
    - Reporting matches incrementally as each batch of results arrives.
    - Presenting a summary of potential matches and the overall search duration.

    Ctrl+C during the search closes the progress bar, prints the partial results found so
    far, and exits with status 130. ``--yes`` answers every confirmation prompt without
    reading stdin, which makes the tool safe to run non-interactively. If any candidate
    could not be checked because of an API failure, the summary says the search was
    incomplete and the program exits with a nonzero status.

    Note: This function interacts with the user via input prompts and exits if critical validation fails.
    """
    # Pre-process sys.argv to handle unquoted project names
    sys.argv = preprocess_argv_for_project_name(sys.argv)

    args = parse_arguments()

    genus = args.genus
    family = args.family
    taxon_id_input = args.taxon_id
    username = args.user
    project_input = args.project
    obs_input = args.observation_number
    digits_off = args.digits
    verbose = args.verbose
    show_progress = not args.no_progress
    assume_yes = args.yes

    start_time = time.time()

    def confirm(prompt, default_yes=False):
        """Ask for confirmation, honouring --yes without touching stdin."""
        return get_user_confirmation(prompt, default_yes, assume_yes=assume_yes)

    if digits_off < 0:
        print("Error: --digits must be 0 or greater.")
        sys.exit(1)
    if digits_off == 0:
        print("Note: --digits 0 only checks the original observation number.")

    # Determine search mode
    if genus:
        search_mode = "genus"
        search_term = genus
    elif family:
        search_mode = "family"
        search_term = family
    elif taxon_id_input is not None:
        search_mode = "taxon_id"
        search_term = taxon_id_input
    elif username:
        search_mode = "user"
        search_term = username
    else:
        search_mode = "project"
        search_term = project_input

    project_id_param = None
    project_metadata = None
    # Verified taxon metadata shared by the genus, family and --taxon-id paths.
    # Once it is set, matching is decided purely from taxon IDs.
    target_taxon = None
    target_taxon_id = None
    taxon_display = None

    # Validate the requested taxon ID before any request, so bad input exits 1
    # and is never confused with a lookup failure.
    requested_taxon_id = None
    if search_mode == "taxon_id":
        requested_taxon_id = parse_taxon_id_argument(taxon_id_input)
        if requested_taxon_id is None:
            print(
                "Error: --taxon-id must be a positive iNaturalist taxon ID "
                f"(got '{taxon_id_input}')."
            )
            sys.exit(1)

    # Verify that the taxon, user, or project exists before proceeding.
    # An ApiError here propagates to main(), which reports an operational failure
    # instead of claiming the taxon, user, or project does not exist.
    if search_mode == "taxon_id":
        print(f"Verifying taxon ID {requested_taxon_id} exists on iNaturalist...")
    else:
        print(f"Verifying {search_mode} '{search_term}' exists on iNaturalist...")

    if search_mode == "taxon_id":
        target_taxon = find_taxon_by_id(requested_taxon_id)
        if not target_taxon:
            print(f"Error: Taxon ID {requested_taxon_id} not found on iNaturalist.")
            print(
                "Please check the ID on iNaturalist, or search by name with --genus "
                "or --family instead."
            )
            sys.exit(1)
        target_taxon_id = target_taxon.get("id") or requested_taxon_id
        print(f"✓ Taxon ID {target_taxon_id} verified: {describe_taxon(target_taxon)}")
        common_name = target_taxon.get("preferred_common_name")
        if common_name:
            print(f"  Common name: {common_name}")
        iconic_taxon = target_taxon.get("iconic_taxon_name")
        if iconic_taxon:
            print(f"  Iconic taxon: {iconic_taxon}")
        taxon_display = format_taxon_reference(target_taxon, target_taxon_id)

    elif search_mode in ("genus", "family"):
        try:
            verified_taxon = find_taxon(search_term, search_mode)
        except TaxonAmbiguityError as error:
            print(
                f"Error: {search_mode.title()} '{search_term}' is ambiguous in "
                "the iNaturalist taxonomy."
            )
            print("Exact matches:")
            for taxon in error.candidates:
                details = [
                    f"ID: {taxon.get('id', 'unknown')}",
                    f"scientific name: {taxon.get('name', 'unknown')}",
                    f"rank: {taxon.get('rank', 'unknown')}",
                ]
                common_name = taxon.get("preferred_common_name")
                if common_name:
                    details.append(f"common name: {common_name}")
                iconic_taxon = taxon.get("iconic_taxon_name")
                if iconic_taxon:
                    details.append(f"iconic taxon: {iconic_taxon}")
                ancestors = taxon.get("ancestor_ids")
                if ancestors:
                    details.append(
                        "ancestor IDs: " + ", ".join(str(value) for value in ancestors)
                    )
                print("  - " + "; ".join(details))
            example_id = next(
                (
                    taxon.get("id")
                    for taxon in error.candidates
                    if taxon.get("id") is not None
                ),
                "ID",
            )
            example_number = parse_inat_url(obs_input)
            if not example_number.isdigit():
                example_number = "OBSERVATION"
            example_command = (
                f"  python inat_finder.py --taxon-id {example_id} {example_number}"
            )
            if digits_off != 1:
                example_command += f" --digits {digits_off}"
            print()
            print("Re-run the search using the desired taxon ID, for example:")
            print()
            print(example_command)
            return 1
        if not verified_taxon:
            print(
                f"Error: {search_mode.title()} '{search_term}' not found in iNaturalist taxonomy."
            )
            print(f"Please check the spelling or try a different {search_mode} name.")
            sys.exit(1)
        target_taxon = verified_taxon
        target_taxon_id = verified_taxon.get("id")
        print(
            f"✓ {search_mode.title()} '{search_term}' verified in iNaturalist taxonomy."
        )

    elif search_mode == "user":
        if not verify_user_exists(username):
            print(f"Error: Username '{username}' not found on iNaturalist.")
            print("Please check the spelling or try a different username.")
            sys.exit(1)
        print(f"✓ Username '{username}' verified on iNaturalist.")

    elif search_mode == "project":
        project_id_param, project_metadata = resolve_project_identifier(project_input)
        title = project_metadata.get("title", "Unknown Project")
        pid = project_metadata.get("id")
        slug = project_metadata.get("slug")
        print(f"✓ Project verified: {title} (ID: {pid}, Slug: {slug})")
        if slug:
            print(f"  Project URL: https://www.inaturalist.org/projects/{slug}")

    # Parse URL if provided
    obs_number = parse_inat_url(obs_input)

    if verbose:
        print(f"Input: {obs_input}")
        if obs_input != obs_number:
            print(f"Extracted observation number: {obs_number}")

    if not obs_number.isdigit():
        print("Error: Observation number must contain only digits")
        print("Input provided: " + obs_input)
        sys.exit(1)

    # Observation IDs have no leading zeroes; normalising keeps candidate counting
    # exact and avoids generating IDs the API would never accept.
    normalized = obs_number.lstrip("0") or "0"
    if normalized != obs_number:
        print(f"Note: reading observation number {obs_number} as {normalized}.")
        obs_number = normalized

    # Check for Mushroom Observer numbers (5 digits or less)
    if len(obs_number) <= 5:
        print(
            f"Note: The observation number {obs_number} is very short (5 digits or less)."
        )
        print("This might be a Mushroom Observer observation rather than iNaturalist.")
        print(f"Consider checking: https://mushroomobserver.org/{obs_number}")

        if not confirm("Continue with iNaturalist search anyway? (y/n): "):
            print("Exiting search.")
            return 0

    def print_summary(found, interrupted=False, unchecked=0, message_callback=print):
        """Print the deduplicated match list, search state, and elapsed time."""
        # Defensive API-result deduplication: an ID should only be reported once.
        # This also collapses the original observation if a candidate returned it.
        deduplicated = list({match.get("id"): match for match in found}.values())
        location_labels = resolve_observation_locations(
            deduplicated, message_callback=message_callback
        )

        if interrupted:
            print("\nSearch interrupted!")
        elif unchecked:
            print("\nSearch incomplete - results may be incomplete.")
        else:
            print("\nSearch complete!")

        if unchecked:
            print(
                f"\nWarning: {unchecked} candidate(s) could not be checked because "
                "iNaturalist requests failed."
            )

        if deduplicated:
            if interrupted:
                suffix = " so far (partial results)"
            elif unchecked:
                suffix = " (partial results)"
            else:
                suffix = ""
            print(f"\nFound {len(deduplicated)} potential matches{suffix}:")
            for i, match in enumerate(deduplicated, 1):
                match_id = match.get("id")
                taxon_name = (match.get("taxon") or {}).get("name", "Unknown taxon")
                creator = (match.get("user") or {}).get("login", "Unknown user")
                location = location_labels.get(match_id, "Unknown location")
                print(f"{i}. Observation #{match_id} - {taxon_name}")
                print(f"   Created by: {creator}")
                print(f"   Location: {location}")
                print(f"   URL: https://www.inaturalist.org/observations/{match_id}")
        elif interrupted:
            print("\nNo matches found before the search was interrupted.")
        elif unchecked:
            print("\nNo matches found among the candidates that were checked.")
            print(
                "Because part of the search did not run, a matching observation may "
                "still exist. Please try again."
            )
        else:
            print("\nNo matches found. Consider these possibilities:")
            print("1. The observation may have more than one digit mistyped")
            if search_mode == "genus":
                print("2. The genus name might be incorrect")
            elif search_mode == "family":
                print("2. The family name might be incorrect")
            elif search_mode == "taxon_id":
                print("2. The taxon ID might be incorrect")
            elif search_mode == "user":
                print("2. The username might be incorrect")
            elif search_mode == "project":
                print(
                    "2. The project might be incorrect (try ID instead of slug/title)"
                )

            print("3. The observation might not exist or has been removed")
            if len(obs_number) <= 5:
                print(
                    "4. This might be a Mushroom Observer number: https://mushroomobserver.org/"
                    + obs_number
                )

        print(f"\nTotal time: {timedelta(seconds=int(time.time() - start_time))}")

    # First, check if the original observation number is correct
    if verbose:
        print(
            f"Checking if original observation number {obs_number} matches {search_mode} '{search_term}'..."
        )

    # Make a single API call to check the original number. A failure here does not
    # stop the search, but it does mean the results are incomplete.
    original_check_failed = False
    try:
        original_check = fetch_observations([obs_number], project_id=project_id_param)
    except ApiError as error:
        original_check = []
        original_check_failed = True
        print(f"Warning: could not check the original observation number - {error}")

    # In project mode, if we get results passing project_id param, they are matches.
    # In other modes, we need to check check_observation_* functions.

    original_match = None

    if original_check:
        match_found = False
        obs = original_check[0]

        if search_mode == "project":
            match_found = True
            print(
                f"✓ Good news! The original observation number {obs_number} is in project '{project_metadata.get('title')}'."
            )
        elif search_mode == "genus" and check_observation_genus(
            obs, genus, target_taxon_id
        ):
            match_found = True
            print(
                f"✓ Good news! The original observation number {obs_number} already matches genus {genus}."
            )
        elif search_mode == "family" and check_observation_family(
            obs, family, target_taxon_id
        ):
            match_found = True
            print(
                f"✓ Good news! The original observation number {obs_number} already matches family {family}."
            )
        elif search_mode == "taxon_id" and check_observation_taxon_id(
            obs, target_taxon_id
        ):
            match_found = True
            print(
                f"✓ Good news! The original observation number {obs_number} "
                f"already belongs to {taxon_display}."
            )
        elif search_mode == "user" and check_observation_user(obs, username):
            match_found = True
            print(
                f"✓ Good news! The original observation number {obs_number} was created by user {username}."
            )

        if match_found:
            original_match = obs
            original_location = resolve_observation_locations([obs]).get(
                obs.get("id"), "Unknown location"
            )
            print(f"  Taxon: {(obs.get('taxon') or {}).get('name', 'Unknown taxon')}")
            print(f"  Creator: {(obs.get('user') or {}).get('login', 'Unknown user')}")
            print(f"  Location: {original_location}")
            print(f"  URL: https://www.inaturalist.org/observations/{obs_number}")
            if not confirm("Continue searching for other potential matches? (y/n): "):
                print("Exiting search.")
                # The original observation is a real match and must be reported.
                print_summary([original_match])
                return API_FAILURE_EXIT_CODE if original_check_failed else 0
        elif search_mode == "taxon_id":
            print(
                f"The original observation #{obs.get('id', obs_number)} exists but "
                f"does not belong to taxon ID {target_taxon_id} "
                f"({target_taxon.get('name', 'unknown')})."
            )
        else:
            print(
                f"The original observation #{obs.get('id', obs_number)} exists but does not match {search_mode} '{search_term}'."
            )
        if not match_found:
            print(
                f"  Actual taxon: {(obs.get('taxon') or {}).get('name', 'Unknown taxon')}"
            )
            print(f"  Creator: {(obs.get('user') or {}).get('login', 'Unknown user')}")

    if search_mode == "genus":
        print(
            f"Looking for iNaturalist observations with genus '{genus}' that might be up to {digits_off} digit(s) off from '{obs_number}'"
        )
    elif search_mode == "family":
        print(
            f"Looking for iNaturalist observations in family '{family}' that might be up to {digits_off} digit(s) off from '{obs_number}'"
        )
    elif search_mode == "taxon_id":
        print(
            f"Looking for iNaturalist observations belonging to {taxon_display} "
            f"that might be up to {digits_off} digit(s) off from '{obs_number}'"
        )
    elif search_mode == "user":
        print(
            f"Looking for iNaturalist observations created by user '{username}' that might be up to {digits_off} digit(s) off from '{obs_number}'"
        )
    else:
        print(
            f"Looking for iNaturalist observations in project '{project_metadata.get('title')}' that might be up to {digits_off} digit(s) off from '{obs_number}'"
        )

    # Size the search space before building it. Only the small candidate classes
    # are materialized here; replacement variations are counted, not generated.
    add_digits = digits_off > 0 and len(obs_number) < 9
    remove_digits = digits_off > 0 and len(obs_number) > 5
    if add_digits:
        print(
            "Observation number has fewer than 9 digits. Will also try adding digits..."
        )
    if remove_digits:
        print(
            "Observation number has more than 5 digits. Will also try removing up to 2 digits..."
        )

    plan = CandidatePlan(
        obs_number, digits_off, add_digits=add_digits, remove_digits=remove_digits
    )
    for line in plan.describe():
        print(f"Generated {line}")

    total_variations = plan.total
    print(f"Generated {total_variations} total unique variations to check")

    # With --digits 0 the only candidate is the original number, which the check above
    # already fetched. Skip the redundant API call and report that result directly.
    if total_variations == 0:
        if digits_off <= 0:
            print(
                "Only the original observation number was generated; it has already been checked."
            )
            print_summary(
                [original_match] if original_match else [],
                unchecked=1 if original_check_failed else 0,
            )
            return API_FAILURE_EXIT_CODE if original_check_failed else 0
        print("Error: No variations could be generated from the observation number.")
        sys.exit(1)

    if total_variations > MAX_SEARCH_CANDIDATES:
        print(
            f"Error: this search would need {total_variations} API-checked candidates, "
            f"which is far more than the limit of {MAX_SEARCH_CANDIDATES}."
        )
        print(
            "Please use a smaller --digits value; searches this large cannot finish "
            "in a reasonable time."
        )
        sys.exit(1)

    estimated_batches = (total_variations + BATCH_SIZE - 1) // BATCH_SIZE
    estimated_seconds = estimated_batches * 1.5
    print(
        f"Estimated API search time: about {timedelta(seconds=int(estimated_seconds))} "
        f"across {estimated_batches} batch(es)"
    )

    if total_variations > LARGE_SEARCH_THRESHOLD and not confirm(
        f"This is a large search ({total_variations} variations). Continue? (y/n): "
    ):
        print("Exiting search.")
        if original_match:
            print_summary([original_match])
        return API_FAILURE_EXIT_CODE if original_check_failed else 0

    # Set up progress bar if requested
    pbar = None

    if show_progress:
        pbar = tqdm(total=total_variations, desc="Checking variations", unit="var")

    # The original observation is a real match and belongs in the final results.
    # print_summary() deduplicates, so a candidate returning it again is harmless.
    matches = [original_match] if original_match else []
    unchecked_reported = 0

    def output(message):
        """Write without corrupting an active tqdm display."""
        if pbar is not None:
            pbar.write(message)
        else:
            print(message)

    def report_progress(count, checked):
        """Advance the bar only for candidates that were really checked."""
        nonlocal unchecked_reported
        if checked:
            if pbar is not None:
                pbar.update(count)
            return
        unchecked_reported += count
        if pbar is not None:
            pbar.set_postfix_str(f"{unchecked_reported} unchecked")

    def describe_batch(batch, start, total):
        if verbose:
            output(
                f"\nChecking batch of {len(batch)} variations "
                f"({start + 1}-{start + len(batch)} of {total})"
            )
            output(f"Variations in this batch: {', '.join(batch)}")

    def evaluate_results(batch_results):
        """Report each batch's matches as soon as the batch comes back."""
        for obs in batch_results:
            obs_id = obs.get("id")
            match_found = False

            if search_mode == "project":
                # Server side filtering has already ensured membership
                match_found = True
                matches.append(obs)
                if verbose:
                    output(f"✓ Match found: Observation {obs_id} is in project")
            elif search_mode == "genus" and check_observation_genus(
                obs, genus, target_taxon_id
            ):
                match_found = True
                matches.append(obs)
                if verbose:
                    output(f"✓ Match found: Observation {obs_id} has genus {genus}")
            elif search_mode == "family" and check_observation_family(
                obs, family, target_taxon_id
            ):
                match_found = True
                matches.append(obs)
                if verbose:
                    output(f"✓ Match found: Observation {obs_id} is in family {family}")
            elif search_mode == "taxon_id" and check_observation_taxon_id(
                obs, target_taxon_id
            ):
                match_found = True
                matches.append(obs)
                if verbose:
                    output(
                        f"✓ Match found: Observation {obs_id} belongs to "
                        f"{taxon_display}"
                    )
            elif search_mode == "user" and check_observation_user(obs, username):
                match_found = True
                matches.append(obs)
                if verbose:
                    output(
                        f"✓ Match found: Observation {obs_id} was created by user {username}"
                    )

            taxon_name = (obs.get("taxon") or {}).get("name")
            if match_found and verbose and taxon_name:
                output(f"  Taxon: {taxon_name}")

            if not match_found and verbose:
                if search_mode == "genus":
                    output(f"✗ Observation {obs_id} does not match genus {genus}")
                elif search_mode == "family":
                    output(f"✗ Observation {obs_id} does not match family {family}")
                elif search_mode == "taxon_id":
                    output(f"✗ Observation {obs_id} does not belong to {taxon_display}")
                elif search_mode == "user":
                    output(f"✗ Observation {obs_id} was not created by user {username}")

    interrupted = False
    unchecked = 1 if original_check_failed else 0
    try:
        result = batch_check_observations(
            plan,
            BATCH_SIZE,
            project_id=project_id_param,
            progress_callback=report_progress,
            batch_callback=describe_batch,
            results_callback=evaluate_results,
            message_callback=output,
            total=total_variations,
            collect_results=False,
        )
        unchecked += result.unchecked
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if pbar is not None:
            pbar.close()
            pbar = None

    if interrupted:
        print("\nSearch cancelled - reporting the matches found so far.")

    print_summary(matches, interrupted=interrupted, unchecked=unchecked)

    if interrupted:
        return 130
    if unchecked:
        return API_FAILURE_EXIT_CODE
    return 0


def main():
    """Run the search and translate operational failures into an exit status."""
    try:
        status = run_search()
    except KeyboardInterrupt:
        # Ctrl+C outside the batch loop (during validation or a lookup).
        print("\nSearch cancelled.")
        sys.exit(130)
    except ApiError as error:
        print(f"Error: could not reach the iNaturalist API - {error}")
        print(
            "This is a connection problem, not a search result. "
            "Please check your network and try again."
        )
        sys.exit(API_FAILURE_EXIT_CODE)
    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()
