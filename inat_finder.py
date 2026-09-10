#!/usr/bin/env python3
"""
iNaturalist Observation Finder

Version 1.8.1 - By Alan Rockefeller - September 10, 2026

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

Auto mode (--auto) turns the tool into one command for a foray: supply whichever
clues you happen to have - genus, family, taxon ID, collector, project, any
combination, or none - and it climbs a ladder of progressively wider typo
hypotheses, stopping after the first rung on which something matches every clue.
It finishes that rung rather than stopping at the hit, so every equally good
candidate is reported together and ranked; with a single clue the first hit is
often a neighbouring observation by the same uploader rather than the one you
want. A clue that turns out to be wrong is reported and dropped rather than
ending the search, because on a foray the mistaken element is as often the genus
as the number.

Usage:
    python inat_finder.py (--genus NAME | --family NAME | --taxon-id ID | --user USER | --project PROJECT) OBSERVATION [options]
    python inat_finder.py --auto [--genus NAME] [--user USER] [...] OBSERVATION [options]

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
    --auto              Try the common failure modes in order, widening the search
                        until something matches. Accepts any combination of clues.
    --digits N          Number of digits that might be wrong (default: 1, or 3 with
                        --auto, where it caps how wide the ladder may go)
    --auto-resume TOKEN Continue an --auto search from where a previous run stopped,
                        using the token that run printed
    --json              Print one machine-readable JSON result on stdout and send
                        all human narration to stderr
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
import contextlib
import enum
import hashlib
import itertools
import json
import math
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

VERSION = "1.8.1"
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
# Above this many candidates, an --auto stage stops at the batch that produced a
# full match instead of finishing. Below it, the stage always runs to the end.
#
# Finishing is the default because "the first full match" is weak evidence here:
# iNaturalist assigns observation IDs sequentially at upload, so the IDs either
# side of a mistyped one very often share an uploader and frequently a taxon.
# With a single clue, is_full_match() is satisfied by that coincidence, and the
# stage used to abort on it while the real observation sat further down the same
# stage, never requested. Checking the rest of a stage this size costs seconds;
# reporting one confidently wrong observation costs the answer.
EARLY_STOP_MIN_CANDIDATES = 5000
OBSERVATION_FIELDS = "id,taxon,user,place_ids,place_guess"
# How wide --auto is allowed to climb when --digits is not given explicitly.
AUTO_DEFAULT_MAX_DIGITS = 3
# Bumped whenever candidate generation changes order or content. A resume cursor
# from an older generation would point at the wrong place, so it is part of the
# resume fingerprint and an old token is rejected rather than silently misused.
CANDIDATE_GENERATION_VERSION = 1
RESUME_TOKEN_VERSION = 1
# Schema version of the --json result object.
JSON_RESULT_VERSION = 1
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


class UsageError(SystemExit):
    """A command-line syntax error: exits 2, and carries its message for --json.

    Argparse normally writes its message to stderr and exits, which leaves a
    --json caller with a non-zero status and no JSON at all. Raising instead lets
    the JSON writer report the same message it printed, without changing either
    the stderr output or the documented exit status.
    """

    def __init__(self, message):
        self.message = message
        super().__init__(2)


class SearchInterrupted(KeyboardInterrupt):
    """Ctrl+C during a batched search, carrying what the search had already done.

    A KeyboardInterrupt subclass on purpose: every existing ``except
    KeyboardInterrupt`` keeps working unchanged, while a caller that wants the
    partial counts - how many candidates were really checked before the key was
    pressed - can read them off ``result`` instead of inventing zeroes.
    """

    def __init__(self, result):
        self.result = result
        super().__init__()


class InputError(SystemExit):
    """Input the script itself rejects: exits 1, and carries why for --json.

    Distinct from :class:`UsageError`, which is argparse's own status 2. Both keep
    printing what they always printed; raising rather than exiting is only so the
    JSON writer can report the same explanation.
    """

    def __init__(self, message):
        self.message = message
        super().__init__(1)


class _ArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose errors can be caught rather than only exiting."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise UsageError(f"{self.prog}: error: {message}")


def wants_json(argv):
    """True when argv asks for --json, before argparse has had a chance to say so.

    The JSON contract has to cover command lines argparse itself rejects, so the
    flag must be detected by scanning. Argparse accepts unambiguous abbreviations
    and no other option begins with "j", so any "--j" prefix counts. A bare "--"
    ends option parsing, so scanning stops there.
    """
    for token in argv[1:]:
        if token == "--":
            break
        if len(token) > 2 and "--json".startswith(token):
            return True
    return False


class Evidence(enum.Enum):
    """What one clue has to say about one observation.

    ``UNKNOWN`` is the important member: project membership is answered by a
    separate request, and when that request fails the genus, user and taxon
    evidence for the same observation is still perfectly good. Saying "unknown"
    keeps that evidence instead of throwing the whole batch away, and it never
    counts toward a score, so an unconfirmed clue can never end the search early.
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


# Evidence about a whole batch that cannot be read off an individual observation.
# ``project_member_ids`` is the set of observation IDs in the batch that belong to
# the requested project, or None when that could not be determined.
BatchContext = namedtuple("BatchContext", ["project_member_ids"])
BatchContext.__new__.__defaults__ = (None,)

# A clue the user supplied that iNaturalist could not resolve. In --auto mode the
# search continues without it; ``ambiguity`` carries a TaxonAmbiguityError when the
# name matched more than one taxon, so the caller can print the --taxon-id list.
UnusableClue = namedtuple("UnusableClue", ["kind", "value", "reason", "ambiguity"])
UnusableClue.__new__.__defaults__ = (None,)

# One observation that matched at least one clue, with the clues it matched, the
# clues that could not be checked, and the ladder stage that found it.
ScoredMatch = namedtuple("ScoredMatch", ["observation", "matched", "unknown", "stage"])


class Criterion:
    """One clue the user supplied, and how to test an observation against it.

    Criteria are evaluated against a batch context rather than an observation
    alone, because project membership is decided server-side and arrives with the
    batch, not with the observation record.
    """

    def __init__(self, kind, value, label, evaluator):
        self.kind = kind
        self.value = value
        self.label = label
        self._evaluator = evaluator
        # Set for taxonomic clues, so the single-criterion path can still report
        # the verified taxon it matched through.
        self.taxon_id = None
        self.taxon = None

    def evaluate(self, observation, context):
        """Return the :class:`Evidence` this clue gives about ``observation``."""
        return self._evaluator(observation, context)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Criterion({self.kind!r}, {self.value!r})"


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


def _argv_position(flag, argv=None):
    """Where ``flag`` first appears in argv, or a large number when it does not."""
    if argv is None:
        argv = sys.argv
    for index, token in enumerate(argv):
        if token == flag or token.startswith(flag + "="):
            return index
    return len(argv) + 1


def parse_arguments():
    """
    Parses command-line arguments for the iNaturalist observation finder.

    This function sets up an argument parser to accept a genus, family, taxon ID,
    username, or project, and a
    potentially mistyped observation number (or URL). It also supports options to specify
    the number of digits that might be incorrect (default: 1), enable verbose output,
    disable the progress bar, and assume "yes" at every confirmation prompt. If no
    arguments are provided, the help message is printed and the program exits.

    The search criteria are mutually exclusive and one is required - unless --auto
    is given, where any combination, including none, is accepted. Argparse cannot
    express "required unless", so the rule is enforced by hand through
    ``parser.error`` in order to keep the documented exit status: a command-line
    syntax error exits 2 with a usage message on stderr, exactly as before.

    Returns:
        argparse.Namespace: An object with attributes corresponding to the parsed arguments.
    """
    # Create a formatted description from the module docstring
    description = textwrap.dedent(__doc__)

    parser = _ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,  # Use this to preserve formatting
    )
    group = parser.add_argument_group(
        "search criteria (one required, or any combination with --auto)"
    )
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
        "--auto",
        action="store_true",
        help=(
            "Try the common failure modes in order, widening the search until "
            "something matches. Accepts any combination of search criteria, "
            "including none, and keeps going when one of them turns out to be wrong"
        ),
    )
    parser.add_argument(
        "--auto-resume",
        dest="auto_resume",
        metavar="TOKEN",
        help=(
            "Continue an --auto search from where a previous run stopped, using "
            "the token that run printed"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print one machine-readable JSON result on stdout, with all human "
            "narration on stderr"
        ),
    )
    parser.add_argument(
        "--digits",
        type=int,
        default=None,
        help=(
            "Maximum number of digits that might be wrong (default: 1; with "
            f"--auto it caps how wide the ladder may go, default "
            f"{AUTO_DEFAULT_MAX_DIGITS})"
        ),
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

    supplied = [
        "--" + name.replace("_", "-")
        for name in ("genus", "family", "taxon_id", "user", "project")
        if getattr(args, name)
    ]
    # Report conflicts in the order the flags were typed, the way argparse's own
    # mutually exclusive group did before --auto made the group conditional.
    supplied.sort(key=lambda flag: _argv_position(flag))
    if not args.auto:
        if not supplied:
            parser.error(
                "one of the arguments --genus --family --taxon-id --user --project "
                "is required (or use --auto to search with any combination of "
                "them, including none)"
            )
        if len(supplied) > 1:
            parser.error(
                f"argument {supplied[-1]}: not allowed with argument "
                f"{supplied[0]} (use --auto to search with both)"
            )
    if args.auto_resume and not args.auto:
        parser.error("argument --auto-resume: only meaningful with --auto")

    # --digits means "how many digits might be wrong" in a normal search and "how
    # wide may the ladder go" in an auto one, so its default depends on the mode.
    if args.digits is None:
        args.digits = AUTO_DEFAULT_MAX_DIGITS if args.auto else 1

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


_STAGE_ORDINALS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def auto_stage_label(index, plan):
    """Describe a ladder stage in terms of what it really searches.

    Deliberately derived from the plan rather than hard-coded, because
    :class:`CandidatePlan` does not add one edit class per ``digits_off``: it
    always tries up to two inserted and two removed digits when those classes are
    enabled at all, and it contributes adjacent swaps only below two substituted
    digits. Describing stage 1 as "one digit off" would be a plain lie about what
    the tool checked.
    """
    if index <= 0:
        return "the number exactly as supplied"

    ordinal = _STAGE_ORDINALS.get(index, str(index))
    digit_word = "digit" if index == 1 else "digits"
    parts = [f"{ordinal} substituted {digit_word}"]
    if plan.transpositions:
        parts.append("adjacent swaps")
    if plan.additions and plan.removals:
        parts.append("missing or extra digits")
    elif plan.additions:
        parts.append("missing digits")
    elif plan.removals:
        parts.append("extra digits")

    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


class AutoStage:
    """One rung of the --auto ladder: a plan minus every candidate already tried.

    The ladder works because the plans nest -
    ``CandidatePlan(n, 1) < CandidatePlan(n, 2) < CandidatePlan(n, 3)`` as sets -
    so stage ``k`` is just plan ``k`` with everything an earlier stage already
    yielded filtered out. Insertions and removals are identical across plans, and
    stage 1's transpositions are all two-digit substitutions, so nothing is lost.

    The size is ``plan.total - len(seen_ids)``, measured when the stage starts.
    That, and not ``plan_k.total - plan_(k-1).total``, is the correct total: a
    stage can end before its candidates run out - a permanent run of failed
    batches stops it, and so does finding a full match - which leaves candidates
    that were never yielded and so never entered ``seen_ids``. The next stage
    picks them up, and only this form declares a total that matches what will
    really be attempted. The progress bar and the unchecked accounting both
    depend on that total being right.

    ``plan_position`` counts entries pulled from the *plan*, not candidates
    yielded, because that is what a resume cursor has to replay.
    """

    def __init__(self, index, plan, seen_ids):
        self.index = index
        self.plan = plan
        self.seen_ids = seen_ids
        self.label = auto_stage_label(index, plan)
        self.total = max(0, plan.total - len(seen_ids))
        self.plan_position = 0

    def __len__(self):
        return self.total

    def __iter__(self):
        for position, candidate in enumerate(self.plan, start=1):
            self.plan_position = position
            value = int(candidate)
            if value in self.seen_ids:
                continue
            self.seen_ids.add(value)
            yield candidate

    def exhausted(self):
        """True when every entry of the underlying plan has been pulled."""
        return self.plan_position >= self.plan.total


def build_candidate_plan(number_str, digits_off):
    """Build the plan for one ladder stage, using the caller-independent rules.

    Insertions only make sense below nine digits and removals only above five, so
    those switches come from the number, never from ``digits_off``.
    """
    add_digits = digits_off > 0 and len(number_str) < 9
    remove_digits = digits_off > 0 and len(number_str) > 5
    return CandidatePlan(
        number_str, digits_off, add_digits=add_digits, remove_digits=remove_digits
    )


def search_fingerprint(number_str, criteria, digits_cap):
    """Return a short hash binding a resume cursor to the search that made it.

    A bare "stage 2, offset 400" cursor is meaningless - worse, silently wrong -
    if it is replayed against a different observation number, a different set of
    clues, or a build whose candidate order has changed. The fingerprint is not a
    secret and does not need to be; it exists so an accidental mismatch is an
    error instead of a search that quietly skips the wrong candidates.
    """
    parts = [
        str(CANDIDATE_GENERATION_VERSION),
        number_str,
        str(digits_cap),
    ]
    parts.extend(sorted(f"{item.kind}={item.value}" for item in criteria))
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:8]


def build_resume_token(stage, offset, fingerprint):
    """Format a resume cursor as ``v1:<stage>:<offset>:<fingerprint>``."""
    return f"v{RESUME_TOKEN_VERSION}:{stage}:{offset}:{fingerprint}"


def parse_resume_token(token):
    """Return ``(stage, offset, fingerprint)`` from a resume token.

    Raises:
        ValueError: The token is not a resume cursor this version understands.
    """
    try:
        parts = token.strip().split(":")
    except AttributeError as error:
        raise ValueError(
            f"resume token must be text, not {type(token).__name__}"
        ) from error
    if len(parts) != 4:
        raise ValueError(f"'{token}' is not a resume token")
    version, stage, offset, fingerprint = parts
    if not version.startswith("v") or not version[1:].isdigit():
        raise ValueError(f"'{token}' is not a resume token")
    if int(version[1:]) != RESUME_TOKEN_VERSION:
        raise ValueError(
            f"resume token version {version[1:]} is not supported by this release"
        )
    if not stage.isdigit() or not offset.isdigit() or not fingerprint:
        raise ValueError(f"'{token}' is not a resume token")
    return int(stage), int(offset), fingerprint


def restore_seen_ids(number_str, stage_index, offset):
    """Rebuild the already-tried set a resume cursor implies. No API calls.

    Replaying is exact rather than approximate: candidate generation is
    deterministic, so iterating the earlier plans in full and the first ``offset``
    entries of the cursor's own plan reproduces precisely the IDs the original run
    had already put in ``seen_ids``.
    """
    seen = set()
    for index in range(1, stage_index):
        for candidate in build_candidate_plan(number_str, index):
            seen.add(int(candidate))
    if offset > 0 and stage_index > 0:
        for position, candidate in enumerate(build_candidate_plan(number_str, stage_index)):
            if position >= offset:
                break
            seen.add(int(candidate))
    return seen


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


def resolve_project_identifier(project_input, strict=True, message_callback=None):
    """
    Resolves a project input string to a valid project ID/slug and metadata.

    Args:
        project_input: The input string (ID, slug, URL, or title).
        strict: When True (the default, and what every non-auto search uses) an
            ambiguous or missing project ends the program with status 1. --auto
            passes False, because there a project that cannot be resolved is a
            clue that turned out to be wrong rather than a fatal input error: the
            same explanation is printed, ``(None, None)`` comes back, and the
            remaining clues carry the search.
        message_callback: Where the explanation is written.

    Returns:
        tuple: (project_id_or_slug, project_metadata_dict), or (None, None) when
        the project could not be resolved and ``strict`` is False.

    Raises:
        ApiError: The project could not be looked up (network/API failure). This
            is never downgraded to "unresolvable", in either mode.

    Exits the program if the project is ambiguous or definitely does not exist,
    unless ``strict`` is False.
    """

    # Resolved here rather than as a default so that a caller (or a test) which
    # replaces the built-in print still sees these explanations.
    if message_callback is None:
        message_callback = print

    def give_up(detail):
        """Fail in strict mode; hand an unresolved project back in auto mode.

        The explanation has already been printed; ``detail`` carries it into the
        exception so that --json reports the same reason rather than a bare
        status with the useful part left on stderr.
        """
        if strict:
            raise InputError(detail)
        return None, None

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
            message_callback(
                f"Error: Project ID '{project_input}' not found on iNaturalist."
            )
            return give_up(
                f"Project ID '{project_input}' not found on iNaturalist."
            )
        if slug_from_url:
            message_callback(f"Error: Project URL slug '{slug_from_url}' not found.")
            return give_up(f"Project URL slug '{slug_from_url}' not found.")

    # 3. Determine if it's likely a title or a slug
    # Conservative slug detection:
    # - If contains spaces -> Title
    # - If all digits -> ID (handled above)
    # - Else -> Treat as Slug candidate, but verify exactly.
    #   If verification fails, fallback to title search.

    is_likely_slug = " " not in project_input

    candidates = search_projects_by_query(project_input)

    if not candidates:
        message_callback(f"Error: Project '{project_input}' not found on iNaturalist.")
        return give_up(f"Project '{project_input}' not found on iNaturalist.")

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
        message_callback(f"Found multiple exact matches for '{project_input}':")
        for p in exact_matches:
            message_callback(
                f" - {p.get('title')} (ID: {p.get('id')}, Slug: {p.get('slug')})"
            )
        message_callback("Please use the specific ID or Slug.")
        return give_up(
            f"'{project_input}' matches more than one project exactly; "
            "use the specific ID or slug."
        )

    # If no exact match, but we have candidates, show disambiguation
    message_callback(
        f"No exact match found for '{project_input}', but found similar projects:"
    )
    for p in candidates[:5]:
        message_callback(
            f" - {p.get('title')} (ID: {p.get('id')}, Slug: {p.get('slug')})"
        )
    message_callback("\nPlease re-run with the specific Project ID or Slug.")
    return give_up(
        f"No project exactly matches '{project_input}'; re-run with the "
        "specific project ID or slug."
    )


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
    "BatchCheckResult",
    [
        "observations",
        "unchecked",
        "failed_batches",
        # Candidates pulled from the iterator, which is what a resume cursor needs.
        "consumed",
        # True when the caller asked to stop before the candidates ran out. The
        # candidates left behind are NOT unchecked: nothing failed, they were
        # deliberately skipped, so they must not push the exit status to 2.
        "stopped_early",
        # Batches whose observations were fetched but whose project membership
        # could not be, leaving that one clue unknown for those observations.
        "membership_unknown",
    ],
)
BatchCheckResult.__new__.__defaults__ = (0, False, 0)


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


def fetch_project_membership(ids, project_id, batch_size=BATCH_SIZE):
    """Return the subset of ``ids`` that belongs to ``project_id``.

    Project membership has to be answered by iNaturalist rather than read off the
    observation record: a collection project's membership is rule-based and does
    not appear in an observation's own ``project_ids``. This is the second request
    a batch needs when --project is combined with other clues, since the plain
    request must not be filtered down to project members only.

    Raises:
        ApiError: The membership request could not be completed.
    """
    observations = fetch_observations(
        ids, project_id=project_id, batch_size=batch_size
    )
    return {
        str(observation.get("id"))
        for observation in observations
        if isinstance(observation, dict) and observation.get("id") is not None
    }


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
    context_callback=None,
    stop_callback=None,
    membership_project_id=None,
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
        context_callback: Optional callable receiving the :class:`BatchContext` for
            a batch immediately *before* ``results_callback`` is given that batch's
            observations. It carries the evidence that belongs to the batch rather
            than to any one observation, which today means project membership.
        stop_callback: Optional callable invoked after each successfully checked
            batch. Returning True ends the search without requesting another batch.
            Candidates never pulled are reported through ``consumed``, not through
            ``unchecked`` - nothing failed, so the result is still complete.
        membership_project_id: When set, each batch gets a second request to work
            out which of its observations belong to that project. Used when
            --project is one clue among several and the main request therefore
            must not be filtered by it. A failed probe leaves membership unknown
            for that batch and is counted in ``membership_unknown``; it does not
            discard the genus, user or taxon evidence the batch did return.
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
    membership_unknown = 0
    stopped_early = False
    start = 0

    def run_batch(batch, batch_start):
        """Return True when the batch was checked; False when its request failed."""
        nonlocal membership_unknown
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

        context = BatchContext()
        if project_id:
            # The request was already filtered server-side, so everything it
            # returned is a member and nothing else in the batch is.
            context = BatchContext(
                project_member_ids={
                    str(observation.get("id"))
                    for observation in batch_results
                    if isinstance(observation, dict)
                    and observation.get("id") is not None
                }
            )
        elif membership_project_id and batch_results:
            try:
                context = BatchContext(
                    project_member_ids=fetch_project_membership(
                        batch, membership_project_id, batch_size=batch_size
                    )
                )
            except ApiError as error:
                # The observations themselves came back fine. Keep that evidence
                # and mark only the project clue unknown for this batch.
                membership_unknown += 1
                message_callback(
                    f"Error checking project membership: {error} "
                    f"(project membership unknown for {len(batch_results)} "
                    "observation(s) in this batch)"
                )

        if collect_results:
            all_results.extend(batch_results)
        if context_callback:
            context_callback(context)
        if results_callback:
            results_callback(batch_results)
        if progress_callback:
            progress_callback(len(batch), True)
        return True

    def collected():
        """The result as it stands right now."""
        return BatchCheckResult(
            all_results,
            unchecked,
            failed_count,
            start,
            stopped_early,
            membership_unknown,
        )

    try:
        for batch in _iter_batches(variations, batch_size):
            checked = run_batch(batch, start)
            for round_number in range(retry_rounds):
                if checked:
                    break
                message_callback(
                    f"Retrying failed batch (attempt {round_number + 2})..."
                )
                checked = run_batch(batch, start)

            start += len(batch)
            if checked:
                consecutive_failures = 0
                if stop_callback and stop_callback():
                    stopped_early = True
                    break
                continue

            unchecked += len(batch)
            failed_count += 1
            consecutive_failures += 1

            if (
                total is not None
                and consecutive_failures >= MAX_CONSECUTIVE_FAILED_BATCHES
            ):
                skipped = max(0, total - start)
                unchecked += skipped
                message_callback(
                    f"Stopping after {consecutive_failures} consecutive batches "
                    f"failed permanently; {skipped} planned candidate(s) will "
                    "remain unchecked."
                )
                break
    except KeyboardInterrupt as error:
        # Hand the caller what was actually done, so a cancelled search can still
        # say "searched 600 of 2,836" instead of reporting nothing.
        if unchecked and progress_callback:
            progress_callback(unchecked, False)
        raise SearchInterrupted(collected()) from error

    if unchecked and progress_callback:
        progress_callback(unchecked, False)

    return BatchCheckResult(
        all_results,
        unchecked,
        failed_count,
        start,
        stopped_early,
        membership_unknown,
    )


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


def _taxon_criterion(kind, value, label, taxon_id, name=None, rank=None, taxon=None):
    """Build a clue that matches through the observation's taxonomic ancestry."""

    def evaluate(observation, context):
        del context  # taxonomy is decidable from the observation alone
        if check_observation_taxon(observation, name, rank, taxon_id):
            return Evidence.MATCH
        return Evidence.NO_MATCH

    criterion = Criterion(kind, value, label, evaluate)
    criterion.taxon_id = taxon_id
    criterion.taxon = taxon
    return criterion


def _user_criterion(username):
    """Build a clue that matches the observation's creator."""

    def evaluate(observation, context):
        del context
        if check_observation_user(observation, username):
            return Evidence.MATCH
        return Evidence.NO_MATCH

    return Criterion("user", username, f"user '{username}'", evaluate)


def _project_criterion(value, label):
    """Build a clue answered by the batch, not by the observation.

    Whether an observation is in a project is something only iNaturalist can say -
    a collection project's membership is rule-based - so this reads the answer out
    of the batch context. When that answer is missing the verdict is UNKNOWN, not
    NO_MATCH: an unanswered question must never look like a negative one.
    """

    def evaluate(observation, context):
        members = context.project_member_ids
        if members is None:
            return Evidence.UNKNOWN
        if str(observation.get("id")) in members:
            return Evidence.MATCH
        return Evidence.NO_MATCH

    return Criterion("project", value, label, evaluate)


ResolvedCriteria = namedtuple(
    "ResolvedCriteria",
    [
        "criteria",
        "unusable",
        # Server-side project filter, set only when the project is the only clue.
        "project_id_param",
        # Per-batch membership probe target, set when the project shares the
        # search with other clues and so must not filter the main request.
        "membership_project_id",
        "project_metadata",
    ],
)


def resolve_criteria(args, strict=True, message_callback=None):
    """Verify every clue the user supplied, in one place for both search modes.

    ``strict`` is the whole difference between the two modes. A normal search has
    exactly one criterion and an unresolvable one is a fatal input error, exactly
    as before. An --auto search may have several, and one that cannot be resolved
    is reported, dropped, and left out of scoring while the rest carry on - the
    point of auto mode being that the wrong element is as often the genus as the
    number.

    What ``strict`` does *not* change: malformed input is always fatal, and an
    :class:`ApiError` is always an outage rather than an unresolvable clue. A clue
    must never be silently discarded because iNaturalist was unreachable.

    Raises:
        ApiError: A lookup could not be completed.
        SystemExit: Malformed input, in either mode.
    """
    if message_callback is None:
        message_callback = print

    criteria = []
    unusable = []
    project_id_param = None
    membership_project_id = None
    project_metadata = None

    def reject(kind, value, reason, ambiguity=None):
        """Record a clue that iNaturalist could not resolve."""
        unusable.append(UnusableClue(kind, value, reason, ambiguity))
        if not strict:
            message_callback(f"  Ignoring the {kind} clue and continuing.")

    for kind, rank in (("genus", "genus"), ("family", "family")):
        name = getattr(args, kind)
        if not name:
            continue
        message_callback(f"Verifying {kind} '{name}' exists on iNaturalist...")
        try:
            taxon = find_taxon(name, rank)
        except TaxonAmbiguityError as error:
            message_callback(
                f"Error: {kind.title()} '{name}' is ambiguous in "
                "the iNaturalist taxonomy."
            )
            message_callback("Exact matches:")
            for candidate in error.candidates:
                message_callback("  - " + describe_taxon_candidate(candidate))
            reject(kind, name, "ambiguous in the iNaturalist taxonomy", error)
            continue
        if not taxon:
            message_callback(
                f"Error: {kind.title()} '{name}' not found in iNaturalist taxonomy."
            )
            message_callback(
                f"Please check the spelling or try a different {kind} name."
            )
            reject(kind, name, "not found in the iNaturalist taxonomy")
            continue
        message_callback(f"✓ {kind.title()} '{name}' verified in iNaturalist taxonomy.")
        criteria.append(
            _taxon_criterion(
                kind,
                name,
                f"{kind} {name}",
                taxon.get("id"),
                name=name,
                rank=rank,
                taxon=taxon,
            )
        )

    if args.taxon_id is not None:
        # Malformed input is fatal in both modes: "abc" is not a clue that turned
        # out to be wrong, it is a command line that cannot be read.
        requested = parse_taxon_id_argument(args.taxon_id)
        if requested is None:
            detail = (
                "--taxon-id must be a positive iNaturalist taxon ID "
                f"(got '{args.taxon_id}')."
            )
            message_callback(f"Error: {detail}")
            raise InputError(detail)
        message_callback(f"Verifying taxon ID {requested} exists on iNaturalist...")
        taxon = find_taxon_by_id(requested)
        if not taxon:
            message_callback(f"Error: Taxon ID {requested} not found on iNaturalist.")
            message_callback(
                "Please check the ID on iNaturalist, or search by name with --genus "
                "or --family instead."
            )
            reject("taxon_id", args.taxon_id, "not found on iNaturalist")
        else:
            taxon_id = taxon.get("id") or requested
            message_callback(
                f"✓ Taxon ID {taxon_id} verified: {describe_taxon(taxon)}"
            )
            common_name = taxon.get("preferred_common_name")
            if common_name:
                message_callback(f"  Common name: {common_name}")
            iconic_taxon = taxon.get("iconic_taxon_name")
            if iconic_taxon:
                message_callback(f"  Iconic taxon: {iconic_taxon}")
            criteria.append(
                _taxon_criterion(
                    "taxon_id",
                    args.taxon_id,
                    format_taxon_reference(taxon, taxon_id),
                    taxon_id,
                    taxon=taxon,
                )
            )

    if args.user:
        message_callback(f"Verifying user '{args.user}' exists on iNaturalist...")
        if verify_user_exists(args.user):
            message_callback(f"✓ Username '{args.user}' verified on iNaturalist.")
            criteria.append(_user_criterion(args.user))
        else:
            message_callback(f"Error: Username '{args.user}' not found on iNaturalist.")
            message_callback("Please check the spelling or try a different username.")
            reject("user", args.user, "not found on iNaturalist")

    if args.project:
        message_callback(f"Verifying project '{args.project}' exists on iNaturalist...")
        project_key, project_metadata = resolve_project_identifier(
            args.project, strict=strict, message_callback=message_callback
        )
        if project_key is None:
            reject("project", args.project, "not found on iNaturalist")
        else:
            title = project_metadata.get("title", "Unknown Project")
            pid = project_metadata.get("id")
            slug = project_metadata.get("slug")
            message_callback(f"✓ Project verified: {title} (ID: {pid}, Slug: {slug})")
            if slug:
                message_callback(
                    f"  Project URL: https://www.inaturalist.org/projects/{slug}"
                )
            criteria.append(_project_criterion(args.project, f"project '{title}'"))
            # A project on its own can be answered by filtering the main request,
            # which is both cheaper and correct for collection projects. Sharing
            # the search with other clues rules that out - the filter would hide
            # every observation the other clues might have matched - so membership
            # moves to a second request per batch instead.
            if len(criteria) == 1 and not any(
                getattr(args, other)
                for other in ("genus", "family", "taxon_id", "user")
            ):
                project_id_param = project_key
            else:
                membership_project_id = project_key

    return ResolvedCriteria(
        criteria, unusable, project_id_param, membership_project_id, project_metadata
    )


def describe_taxon_candidate(taxon):
    """Format one line of the homonym list shown for an ambiguous taxon name."""
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
        details.append("ancestor IDs: " + ", ".join(str(value) for value in ancestors))
    return "; ".join(details)


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


SearchOutcome = namedtuple(
    "SearchOutcome",
    [
        "status",
        "stop_reason",
        "matches",
        "unusable",
        "notices",
        "original",
        "stage",
        "attempted",
        "stage_total",
        "unchecked",
        "stages",
        "resume",
        "estimated_candidates",
        "estimated_seconds",
        "criteria",
        "message",
        # Whether the search actually ran to a conclusion. Stated rather than
        # inferred from the status, because "paused before a stage we have not
        # searched" and "finished, nothing there" are both non-failures and only
        # one of them is complete.
        "complete",
        "exit_code",
    ],
)
SearchOutcome.__new__.__defaults__ = (
    None,  # stop_reason
    (),  # matches
    (),  # unusable
    (),  # notices
    None,  # original
    None,  # stage
    0,  # attempted
    None,  # stage_total
    0,  # unchecked
    (),  # stages
    None,  # resume
    None,  # estimated_candidates
    None,  # estimated_seconds
    (),  # criteria
    None,  # message
    False,  # complete
    None,  # exit_code
)


def outcome_is_complete(status, stop_reason):
    """True only when the ladder really finished the work it set out to do.

    ``needs_confirmation`` and a declined or over-sized stage all leave candidates
    deliberately unsearched, so none of them may claim completeness even though
    none of them is a failure either.
    """
    return status in ("match_found", "no_match") and stop_reason not in (
        "declined",
        "too_large",
        "large_stage",
        "early_exit",
        "usage",
        "bad_input",
    )

# The documented exit-code contract, expressed once. Callers must not invent a
# status without deciding what it means to a script.
STATUS_EXIT_CODES = {
    "match_found": 0,
    "no_match": 0,
    "needs_confirmation": 0,
    "incomplete": API_FAILURE_EXIT_CODE,
    "cancelled": 130,
    "error": 1,
}


def rank_matches(matches, origin=None):
    """Deduplicate by observation ID and sort best-first.

    An observation can be scored more than once - the original number may also
    turn up as a candidate - so the highest-scoring copy wins.

    Score decides the order first. Everything after it exists because equal scores
    are the common case, not the rare one: with a single clue every hit scores
    1 of 1, so without a tie-break the "best" match would be whichever candidate
    happened to have the lowest ID. Ties therefore break on the stage that found
    the match (fewer digits off is a likelier typo), then on numeric distance from
    the number the user actually typed, and only then on ID, which keeps the order
    stable between runs - something a page that diffs results depends on.

    Args:
        origin: The observation number as supplied, if known. Only used for the
            distance tie-break; without it that term is constant and the order
            falls through to ID as before.
    """
    try:
        origin_value = int(origin) if origin is not None else None
    except (TypeError, ValueError):
        origin_value = None

    best = {}
    for match in matches:
        obs_id = match.observation.get("id")
        current = best.get(obs_id)
        if current is None or len(match.matched) > len(current.matched):
            best[obs_id] = match

    def sort_key(match):
        obs_id = match.observation.get("id") or 0
        # A non-auto search leaves stage None; those sort after every staged
        # match rather than ahead of stage 0.
        stage = match.stage if match.stage is not None else math.inf
        distance = abs(obs_id - origin_value) if origin_value is not None else 0
        return (-len(match.matched), stage, distance, obs_id)

    return sorted(best.values(), key=sort_key)


def format_match_score(match, criteria):
    """Render '2 of 3: genus, user; project: unknown' for one scored match."""
    if not criteria:
        return ""
    parts = [f"{len(match.matched)} of {len(criteria)}"]
    if match.matched:
        parts.append(": " + ", ".join(match.matched))
    if match.unknown:
        parts.append("; " + ", ".join(match.unknown) + ": unknown")
    return "".join(parts)


def _observation_summary(observation, location):
    """The fields both renderers show for one observation."""
    obs_id = observation.get("id")
    return {
        "id": obs_id,
        "taxon": (observation.get("taxon") or {}).get("name"),
        "user": (observation.get("user") or {}).get("login"),
        "location": location,
        "url": f"https://www.inaturalist.org/observations/{obs_id}",
    }


def build_json_result(outcome, locations):
    """Build the single JSON object a machine consumer reads off stdout."""
    matches = []
    for match in outcome.matches:
        entry = _observation_summary(
            match.observation, locations.get(match.observation.get("id"))
        )
        entry.update(
            {
                "score": len(match.matched),
                "matched": list(match.matched),
                "unknown": list(match.unknown),
                "stage": match.stage,
            }
        )
        matches.append(entry)

    original = None
    if outcome.original is not None:
        original = _observation_summary(
            outcome.original, locations.get(outcome.original.get("id"))
        )

    return {
        "version": JSON_RESULT_VERSION,
        "status": outcome.status,
        "complete": bool(outcome.complete),
        "exit_code": (
            outcome.exit_code
            if outcome.exit_code is not None
            else STATUS_EXIT_CODES.get(outcome.status, 1)
        ),
        "stop_reason": outcome.stop_reason,
        "stage": outcome.stage,
        "attempted": outcome.attempted,
        "stage_total": outcome.stage_total,
        "unchecked": outcome.unchecked,
        "clues": [criterion.kind for criterion in outcome.criteria],
        "matches": matches,
        "original": original,
        "unusable_clues": [
            {"kind": clue.kind, "value": clue.value, "reason": clue.reason}
            for clue in outcome.unusable
        ],
        "notices": list(outcome.notices),
        "stages": [dict(stage) for stage in outcome.stages],
        "estimated_candidates": outcome.estimated_candidates,
        "estimated_seconds": outcome.estimated_seconds,
        "resume": dict(outcome.resume) if outcome.resume else None,
        "message": outcome.message,
    }


def print_outcome(outcome, locations, obs_number, message_callback=None):
    """Print the human-readable summary of a finished (or stopped) search."""
    if message_callback is None:
        message_callback = print

    if outcome.status == "cancelled":
        message_callback("\nSearch interrupted!")
    elif outcome.status == "incomplete":
        message_callback("\nSearch incomplete - results may be incomplete.")
    elif outcome.status == "needs_confirmation":
        message_callback("\nSearch paused before a much larger stage.")
    else:
        message_callback("\nSearch complete!")

    if outcome.unchecked:
        message_callback(
            f"\nWarning: {outcome.unchecked} candidate(s) could not be checked "
            "because iNaturalist requests failed."
        )

    for clue in outcome.unusable:
        message_callback(
            f"\nNote: the {clue.kind} clue '{clue.value}' was unusable "
            f"({clue.reason}); the search ran without it."
        )

    for notice in outcome.notices:
        message_callback(f"\nNote: {notice}")

    if outcome.matches:
        if outcome.status == "cancelled":
            suffix = " so far (partial results)"
        elif outcome.status == "incomplete":
            suffix = " (partial results)"
        else:
            suffix = ""
        message_callback(
            f"\nFound {len(outcome.matches)} potential matches{suffix}:"
        )
        for position, match in enumerate(outcome.matches, 1):
            observation = match.observation
            obs_id = observation.get("id")
            taxon_name = (observation.get("taxon") or {}).get("name", "Unknown taxon")
            creator = (observation.get("user") or {}).get("login", "Unknown user")
            location = locations.get(obs_id, "Unknown location")
            score = format_match_score(match, outcome.criteria)
            heading = f"{position}. Observation #{obs_id} - {taxon_name}"
            if score:
                heading += f"   [{score}]"
            message_callback(heading)
            message_callback(f"   Created by: {creator}")
            message_callback(f"   Location: {location}")
            message_callback(
                f"   URL: https://www.inaturalist.org/observations/{obs_id}"
            )
            if match.stage is not None:
                message_callback(f"   Found at stage {match.stage}")

        best = len(outcome.matches[0].matched)
        if outcome.criteria and best < len(outcome.criteria):
            message_callback(
                "\nNo observation matched every clue. When the best match is a "
                "partial one, the element that is wrong is often a clue rather "
                "than the number."
            )
    elif outcome.original is not None and not outcome.criteria:
        observation = outcome.original
        obs_id = observation.get("id")
        message_callback(
            f"\nObservation #{obs_id} exists: "
            f"{(observation.get('taxon') or {}).get('name', 'Unknown taxon')}"
        )
        message_callback(
            f"   Created by: {(observation.get('user') or {}).get('login', 'Unknown user')}"
        )
        message_callback(
            f"   Location: {locations.get(obs_id, 'Unknown location')}"
        )
        message_callback(f"   URL: https://www.inaturalist.org/observations/{obs_id}")

    if outcome.message:
        message_callback(f"\n{outcome.message}")

    if outcome.resume:
        message_callback("\nTo keep searching from here, re-run with:")
        message_callback(f"  --auto-resume {outcome.resume['token']}")

    return outcome


PassResult = namedtuple(
    "PassResult",
    [
        "matches",
        "unchecked",
        "consumed",
        "stopped_early",
        "membership_unknown",
        "interrupted",
    ],
)
PassResult.__new__.__defaults__ = (False,)


def score_observation(observation, context, criteria):
    """Return ``(matched_kinds, unknown_kinds)`` for one observation.

    Scoring is any-of on purpose. Requiring every clue to agree would hide the
    real observation whenever one supplied element was itself wrong, which is the
    common case this feature exists for; ranking by how many clues agreed keeps
    the best answer at the top without throwing the near misses away.
    """
    matched = []
    unknown = []
    for criterion in criteria:
        verdict = criterion.evaluate(observation, context)
        if verdict is Evidence.MATCH:
            matched.append(criterion.kind)
        elif verdict is Evidence.UNKNOWN:
            unknown.append(criterion.kind)
    return matched, unknown


def is_full_match(matched, criteria):
    """True when every clue agreed. An UNKNOWN clue can never make this true."""
    return bool(criteria) and len(matched) == len(criteria)


def run_one_pass(
    candidates,
    total,
    criteria,
    stage_index=None,
    project_id_param=None,
    membership_project_id=None,
    stop_on_full_match=False,
    show_progress=True,
    verbose=False,
    message_callback=None,
):
    """Check one set of candidates against every clue, and report what matched.

    This is the single-pass unit the ladder repeats. ``stop_on_full_match`` is
    what makes "stops at the first hit" literally true: without it, a hit in the
    first batch of a 59,000-candidate stage would still cost the whole stage.
    Every match in the batch that triggered the stop is kept; the candidates never
    requested are reported through ``consumed``, not as unchecked, because nothing
    failed.

    Ctrl+C is caught here rather than left to propagate, so that matches already
    found earlier in this stage are returned instead of being lost with the stack
    frame. "Ctrl+C prints what was found so far" has to include the stage that was
    running when the key was pressed.
    """
    if message_callback is None:
        message_callback = print

    pbar = None
    if show_progress and total:
        pbar = tqdm(total=total, desc="Checking variations", unit="var")

    matches = []
    unchecked_reported = 0
    found_full = False
    context_holder = [BatchContext()]

    def output(message):
        """Write without corrupting an active tqdm display."""
        if pbar is not None:
            pbar.write(message)
        else:
            message_callback(message)

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

    def describe_batch(batch, start, batch_total):
        if verbose:
            output(
                f"\nChecking batch of {len(batch)} variations "
                f"({start + 1}-{start + len(batch)} of {batch_total})"
            )
            output(f"Variations in this batch: {', '.join(batch)}")

    def record_context(context):
        context_holder[0] = context

    def evaluate_results(batch_results):
        """Score each batch's observations as soon as the batch comes back."""
        nonlocal found_full
        context = context_holder[0]
        for observation in batch_results:
            if not isinstance(observation, dict):
                continue
            matched, unknown = score_observation(observation, context, criteria)
            if not matched:
                if verbose:
                    output(
                        f"✗ Observation {observation.get('id')} matched none of "
                        "the clues"
                    )
                continue
            matches.append(ScoredMatch(observation, matched, unknown, stage_index))
            if is_full_match(matched, criteria):
                found_full = True
            if verbose:
                output(
                    f"✓ Match found: Observation {observation.get('id')} matched "
                    f"{', '.join(matched)}"
                )

    def should_stop():
        return stop_on_full_match and found_full

    try:
        result = batch_check_observations(
            candidates,
            BATCH_SIZE,
            project_id=project_id_param,
            progress_callback=report_progress,
            batch_callback=describe_batch,
            results_callback=evaluate_results,
            context_callback=record_context,
            stop_callback=should_stop,
            membership_project_id=membership_project_id,
            message_callback=output,
            total=total,
            collect_results=False,
        )
    except SearchInterrupted as error:
        partial = error.result
        return PassResult(
            matches,
            partial.unchecked,
            partial.consumed,
            partial.stopped_early,
            partial.membership_unknown,
            True,
        )
    except KeyboardInterrupt:
        # Interrupted outside the batch loop, so there are no partial counts.
        return PassResult(matches, 0, 0, False, 0, True)
    finally:
        if pbar is not None:
            pbar.close()

    return PassResult(
        matches,
        result.unchecked,
        result.consumed,
        result.stopped_early,
        result.membership_unknown,
    )


def stdin_is_interactive():
    """True when there is a human at a terminal who could answer a prompt."""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def estimate_stage_seconds(total, membership_project_id=None):
    """Roughly how long a stage will take, in seconds.

    Combining --project with other clues costs a second request per batch, since
    the main request cannot be filtered by the project without hiding everything
    the other clues might have matched.
    """
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    if membership_project_id:
        batches *= 2
    return int(batches * 1.5)


def check_original_observation(obs_number, resolved, message_callback=None):
    """Fetch the number as supplied and score it. Stage 0 of the ladder.

    Returns ``(observation_or_None, matched, unknown, failed, membership_unknown)``.
    ``failed`` means the observation request could not be completed, which is not
    the same as the observation not existing and must not be reported as such.
    ``membership_unknown`` means the observation came back but its project
    membership could not be checked - reported separately because it leaves the
    search incomplete just as surely, and stage 0 is never re-run on a resume.

    Unlike the later stages, this request is never filtered by project. Stage 0
    asks "what is this number?", and a project filter answers a different
    question: it would hide a real observation that simply is not a member,
    leaving the run to report it as nonexistent. Membership is asked separately
    so that "not in the project" stays distinct from "not there at all".
    """
    if message_callback is None:
        message_callback = print
    try:
        found = fetch_observations([obs_number])
    except ApiError as error:
        message_callback(
            f"Warning: could not check the original observation number - {error}"
        )
        return None, [], [], True, False

    if not found:
        return None, [], [], False, False

    observation = found[0]
    membership_unknown = False
    context = BatchContext()
    project_id = resolved.project_id_param or resolved.membership_project_id
    if project_id:
        try:
            context = BatchContext(
                project_member_ids=fetch_project_membership([obs_number], project_id)
            )
        except ApiError as error:
            # Stage 0 is not repeated when a search resumes, so an unanswered
            # question here would otherwise be skipped for good.
            membership_unknown = True
            message_callback(
                f"Warning: could not check project membership for the original "
                f"observation number - {error}"
            )

    matched, unknown = score_observation(observation, context, resolved.criteria)
    return observation, matched, unknown, False, membership_unknown


def run_auto_mode(
    obs_number,
    resolved,
    digits_cap,
    resume_token=None,
    assume_yes=False,
    as_json=False,
    show_progress=True,
    verbose=False,
    message_callback=None,
):
    """Climb the ladder of typo hypotheses until something matches, or nothing does.

    The ladder is stage 0 (the number exactly as supplied) then one plan per
    ``digits_off`` up to ``digits_cap``, each stage yielding only what earlier
    stages did not already try. It stops after the first stage that produced a
    *full* match - one where every usable clue agreed - because a partial match
    usually means one of the clues is itself wrong, and that is worth widening the
    search to check.

    It stops after the stage, not at the match. With a single clue a full match is
    just one agreeing field, and IDs adjacent to a mistyped one routinely belong to
    the same uploader, so the first hit is frequently a coincidence sitting in front
    of the real observation later in the same stage. Finishing the stage collects
    every equally-good candidate and hands the choice to the reader, ranked by
    rank_matches(). Only a stage larger than ``EARLY_STOP_MIN_CANDIDATES`` keeps the
    old abort-on-hit behaviour, where finishing would cost minutes rather than
    seconds.
    """
    if message_callback is None:
        message_callback = print

    criteria = resolved.criteria
    fingerprint = search_fingerprint(obs_number, criteria, digits_cap)
    interactive = stdin_is_interactive() and not as_json

    seen_ids = set()
    start_stage = 1
    resuming = False
    if resume_token:
        try:
            start_stage, start_offset, token_fingerprint = parse_resume_token(
                resume_token
            )
        except ValueError as error:
            return SearchOutcome(
                status="error",
                stop_reason="bad_resume",
                message=str(error),
                exit_code=1,
            )
        if token_fingerprint != fingerprint:
            return SearchOutcome(
                status="error",
                stop_reason="bad_resume",
                message=(
                    "This resume token belongs to a different search. Tokens are "
                    "bound to the observation number, the clues and the --digits "
                    "cap, so that a cursor can never be replayed against a search "
                    "it did not come from. Start again without --auto-resume."
                ),
                exit_code=1,
            )
        if not 1 <= start_stage <= digits_cap:
            return SearchOutcome(
                status="error",
                stop_reason="bad_resume",
                message=(
                    f"This resume token points at stage {start_stage}, which is "
                    f"not a stage this search has (1 to {digits_cap}). Raise "
                    "--digits if you meant to search further."
                ),
                exit_code=1,
            )
        resume_plan_total = build_candidate_plan(obs_number, start_stage).total
        if not 0 <= start_offset <= resume_plan_total:
            return SearchOutcome(
                status="error",
                stop_reason="bad_resume",
                message=(
                    f"This resume token points {start_offset} candidate(s) into "
                    f"stage {start_stage}, which only has {resume_plan_total}."
                ),
                exit_code=1,
            )
        seen_ids = restore_seen_ids(obs_number, start_stage, start_offset)
        resuming = True
        message_callback(
            f"Resuming at stage {start_stage}, {len(seen_ids)} candidate(s) "
            "already tried."
        )

    matches = []
    notices = []
    stages = []
    unchecked_total = 0
    membership_unknown_total = 0
    original = None
    interrupted = False
    stop_reason = "exhausted"
    last_stage = None
    last_attempted = 0
    last_stage_total = None
    resume = None

    def cursor(stage_index, offset):
        """A resume cursor, but only when nothing was left unchecked.

        A failed batch's IDs are already in ``seen_ids``, so a cursor issued after
        a failure would skip them forever and quietly report a clean "no match"
        over a gap. Until the cursor can carry those IDs, a search with anything
        unchecked is retried from the beginning instead.
        """
        if unchecked_total or membership_unknown_total:
            return None
        if stage_index > digits_cap:
            return None
        return {
            "stage": stage_index,
            "offset": offset,
            "token": build_resume_token(stage_index, offset, fingerprint),
        }

    def finish(status, reason, message=None, estimated=None, seconds=None):
        """Assemble the outcome, and never let a failure hide behind a clean status.

        Every early return comes through here so that one rule holds everywhere:
        if anything went unchecked, the search is incomplete and says so. Reporting
        "no match", or pausing for confirmation, over a gap left by a failed
        request would be exactly the false negative this tool is built to avoid.
        """
        if status not in ("cancelled", "error") and (
            unchecked_total or membership_unknown_total
        ):
            status, reason = "incomplete", "failures"
            if membership_unknown_total and not message:
                message = (
                    "Project membership could not be checked for part of this "
                    "search, so the project clue is unknown for some results."
                )
        return SearchOutcome(
            status=status,
            stop_reason=reason,
            matches=rank_matches(matches, origin=obs_number),
            unusable=tuple(resolved.unusable),
            notices=tuple(notices),
            original=original,
            stage=last_stage,
            attempted=last_attempted,
            stage_total=last_stage_total,
            unchecked=unchecked_total,
            stages=tuple(stages),
            resume=resume,
            estimated_candidates=estimated,
            estimated_seconds=seconds,
            criteria=tuple(criteria),
            message=message,
            complete=outcome_is_complete(status, reason),
            exit_code=STATUS_EXIT_CODES.get(status, 1),
        )

    try:
        if not resuming:
            last_stage = 0
            (
                observation,
                matched,
                unknown,
                failed,
                original_membership_unknown,
            ) = check_original_observation(
                obs_number, resolved, message_callback=message_callback
            )
            if failed:
                unchecked_total += 1
            if original_membership_unknown:
                membership_unknown_total += 1
            if observation is not None:
                original = observation
                last_attempted = 1
                last_stage_total = 1
                stages.append(
                    {"stage": 0, "total": 1, "attempted": 1, "unchecked": 0}
                )
                if matched:
                    matches.append(ScoredMatch(observation, matched, unknown, 0))
                if is_full_match(matched, criteria):
                    message_callback(
                        f"✓ The observation number {obs_number} as supplied "
                        "matches every clue."
                    )
                    resume = cursor(1, 0)
                    return finish("match_found", "full_match")
                if criteria:
                    # The number points at a real observation, just not the one
                    # the clues describe. Worth saying out loud: it is the first
                    # thing a reader wants to know before the ladder starts.
                    message_callback(
                        f"Observation #{observation.get('id', obs_number)} exists "
                        f"but matched {len(matched)} of {len(criteria)} clue(s): "
                        f"{(observation.get('taxon') or {}).get('name', 'Unknown taxon')}"
                        f", by {(observation.get('user') or {}).get('login', 'Unknown user')}."
                    )
            else:
                stages.append(
                    {
                        "stage": 0,
                        "total": 1,
                        "attempted": 0 if failed else 1,
                        "unchecked": 1 if failed else 0,
                    }
                )
                if failed:
                    # check_original_observation() has already explained the
                    # failure. Saying "does not exist" here would turn an outage
                    # into a fact about the observation.
                    message_callback(
                        f"Observation {obs_number} as supplied could not be "
                        "checked; the ladder will continue."
                    )
                else:
                    message_callback(
                        f"Observation {obs_number} as supplied does not exist on "
                        "iNaturalist."
                    )

        if not criteria:
            # Nothing to filter on. Enumerating thousands of neighbouring IDs
            # would return every observation that happens to exist near this
            # number, which is noise rather than an answer.
            return finish(
                "no_match",
                "no_clues",
                "No usable clue was supplied, so there was nothing to search for. "
                "Add --genus, --family, --taxon-id, --user or --project to widen "
                "the search beyond the number as given.",
            )

        for index in range(max(1, start_stage), digits_cap + 1):
            plan = build_candidate_plan(obs_number, index)
            stage = AutoStage(index, plan, seen_ids)
            last_stage = index
            last_stage_total = stage.total
            last_attempted = 0
            if stage.total <= 0:
                continue

            if stage.total > MAX_SEARCH_CANDIDATES:
                # This stage was never searched, so the run has not established
                # that there is nothing there. Saying "no match" would be a lie.
                return finish(
                    "error",
                    "too_large",
                    f"Stage {index} would need {stage.total} API-checked "
                    f"candidates, more than the limit of {MAX_SEARCH_CANDIDATES}. "
                    "Lower --digits.",
                )

            seconds = estimate_stage_seconds(
                stage.total, resolved.membership_project_id
            )
            message_callback(
                f"\nStage {index}: {stage.label} - {stage.total} new candidate(s), "
                f"about {timedelta(seconds=seconds)}"
            )

            if stage.total > LARGE_SEARCH_THRESHOLD and not assume_yes:
                if not interactive:
                    # No one is there to answer. Stop cleanly and hand back a
                    # cursor so the caller can decide and continue, rather than
                    # blocking on a prompt or silently spending seven minutes.
                    resume = cursor(index, 0)
                    return finish(
                        "needs_confirmation",
                        "large_stage",
                        f"Stage {index} would check {stage.total} more "
                        "possibilities. Re-run with --yes and the resume token "
                        "to continue.",
                        estimated=stage.total,
                        seconds=seconds,
                    )
                if not get_user_confirmation(
                    f"This is a large stage ({stage.total} variations). Continue? (y/n): "
                ):
                    resume = cursor(index, 0)
                    return finish(
                        "no_match" if not matches else "match_found",
                        "declined",
                        "Stopped before the larger stage at your request.",
                    )

            result = run_one_pass(
                stage,
                stage.total,
                criteria,
                stage_index=index,
                project_id_param=resolved.project_id_param,
                membership_project_id=resolved.membership_project_id,
                stop_on_full_match=stage.total > EARLY_STOP_MIN_CANDIDATES,
                show_progress=show_progress,
                verbose=verbose,
                message_callback=message_callback,
            )
            matches.extend(result.matches)
            unchecked_total += result.unchecked
            membership_unknown_total += result.membership_unknown
            last_attempted = result.consumed
            stages.append(
                {
                    "stage": index,
                    "total": stage.total,
                    "attempted": result.consumed,
                    "unchecked": result.unchecked,
                }
            )

            if result.interrupted:
                # The matches this stage had already found are in hand, and are
                # reported rather than lost with the interrupted call.
                interrupted = True
                break

            full_here = [
                match
                for match in result.matches
                if is_full_match(match.matched, criteria)
            ]
            if full_here:
                stop_reason = "full_match"
                if len(full_here) > 1 and len(criteria) == 1:
                    # Worth saying plainly rather than leaving the reader to infer
                    # it from a list: one clue cannot separate these, and nearby
                    # IDs share an uploader far more often than chance.
                    notices.append(
                        f"{len(full_here)} nearby observations all match the only "
                        f"clue you gave ({criteria[0].kind}), so the one listed "
                        "first is a best guess rather than an answer. iNaturalist "
                        "numbers observations in the order they were uploaded, so "
                        "numbers next to each other often belong to the same "
                        "person and the same taxon. Check all of them, or add a "
                        "second clue."
                    )
                if stage.exhausted():
                    resume = cursor(index + 1, 0)
                else:
                    # The message counts new candidates, which is what the stage
                    # announced; the cursor counts plan entries, which is what a
                    # resume has to replay. They are deliberately different numbers.
                    resume = cursor(index, stage.plan_position)
                    message_callback(
                        f"\nStopped at stage {index} after {result.consumed} of "
                        f"{stage.total} new candidate(s) because a full match "
                        "was found."
                    )
                break
    except KeyboardInterrupt:
        interrupted = True

    if interrupted:
        return finish("cancelled", "interrupted")
    if matches:
        return finish("match_found", stop_reason)
    return finish("no_match", stop_reason)


def run_search():
    """Choose the output surface, then run the search.

    With --json the caller wants exactly one JSON object on stdout, so every human
    line the search prints is redirected to stderr for the duration and the result
    object is written afterwards. Redirecting is what makes this possible without
    rewriting the ~80 print() calls the text mode is built from.

    Argument parsing happens *inside* that machinery, not before it. A missing
    observation number, an unknown option or two conflicting criteria are exactly
    the cases a web front end most needs a readable answer for, and they are also
    the cases argparse would otherwise exit on before any JSON existed.
    """
    if not wants_json(sys.argv):
        # Pre-process sys.argv to handle unquoted project names
        sys.argv = preprocess_argv_for_project_name(sys.argv)
        return execute_search(parse_arguments())

    sink = {}
    status = 0
    message = None
    reason = "early_exit"
    try:
        with contextlib.redirect_stdout(sys.stderr):
            sys.argv = preprocess_argv_for_project_name(sys.argv)
            status = execute_search(parse_arguments(), sink) or 0
    except UsageError as error:
        # Argparse's own status is 2, the same as an unreachable API. The status
        # string is what tells the two apart, which is the point of --json.
        status, message, reason = 2, error.message, "usage"
    except InputError as error:
        status, message, reason = 1, error.message, "bad_input"
    except SystemExit as exit_error:
        status = exit_error.code or 0
    except KeyboardInterrupt:
        status, reason = 130, "interrupted"
    except ApiError as error:
        status, message, reason = API_FAILURE_EXIT_CODE, str(error), "api_error"

    if "result" not in sink:
        # An exit path that never built a result: bad input, or a refusal. None of
        # them ran a search to completion, so none of them claim to be complete.
        fallback = {
            0: "no_match",
            1: "error",
            API_FAILURE_EXIT_CODE: "incomplete",
            130: "cancelled",
        }.get(status, "error")
        if reason in ("usage", "bad_input"):
            fallback = "error"
        sink["result"] = build_json_result(
            SearchOutcome(
                status=fallback,
                stop_reason=reason,
                message=message,
                complete=False,
                exit_code=status,
            ),
            {},
        )

    print(json.dumps(sink["result"], indent=2))
    return status


def _emit(sink, outcome, locations):
    """Record a finished outcome for the JSON writer, when one is listening."""
    if sink is not None:
        sink["result"] = build_json_result(outcome, locations)
    return STATUS_EXIT_CODES.get(outcome.status, 1)


def execute_search(args, sink=None):
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
    genus = args.genus
    family = args.family
    username = args.user
    obs_input = args.observation_number
    digits_off = args.digits
    verbose = args.verbose
    show_progress = not args.no_progress
    assume_yes = args.yes
    auto = args.auto
    as_json = args.json
    notices = []

    start_time = time.time()

    def confirm(prompt, default_yes=False):
        """Ask for confirmation, honouring --yes without touching stdin."""
        return get_user_confirmation(prompt, default_yes, assume_yes=assume_yes)

    if digits_off < 0:
        print("Error: --digits must be 0 or greater.")
        raise InputError("--digits must be 0 or greater.")
    if digits_off == 0:
        print("Note: --digits 0 only checks the original observation number.")

    # Every clue - one here, possibly several under --auto - is verified through
    # the same resolver, so there is one verification code path rather than two.
    resolved = resolve_criteria(args, strict=not auto)

    if resolved.unusable and not auto:
        # A normal search has exactly one criterion, so an unresolvable one is
        # fatal, exactly as before. resolve_criteria() has already explained why;
        # an ambiguous name additionally gets the ready-to-paste re-run command.
        clue = resolved.unusable[0]
        if clue.ambiguity is not None:
            example_id = next(
                (
                    taxon.get("id")
                    for taxon in clue.ambiguity.candidates
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
        # Record the reason rather than only printing it, so --json reports the
        # same explanation a person reads on the terminal.
        return _emit(
            sink,
            SearchOutcome(
                status="error",
                stop_reason="bad_input",
                message=f"{clue.kind} '{clue.value}': {clue.reason}",
                unusable=tuple(resolved.unusable),
                complete=False,
                exit_code=1,
            ),
            {},
        )

    project_id_param = resolved.project_id_param
    project_metadata = resolved.project_metadata
    criterion = resolved.criteria[0] if resolved.criteria else None
    search_mode = criterion.kind if criterion else None
    search_term = criterion.value if criterion else None
    target_taxon = criterion.taxon if criterion else None
    target_taxon_id = criterion.taxon_id if criterion else None
    taxon_display = criterion.label if search_mode == "taxon_id" else None

    # Parse URL if provided
    obs_number = parse_inat_url(obs_input)

    if verbose:
        print(f"Input: {obs_input}")
        if obs_input != obs_number:
            print(f"Extracted observation number: {obs_number}")

    if not obs_number.isdigit():
        print("Error: Observation number must contain only digits")
        print("Input provided: " + obs_input)
        raise InputError(
            f"Observation number must contain only digits (got '{obs_input}')."
        )

    # Observation IDs have no leading zeroes; normalising keeps candidate counting
    # exact and avoids generating IDs the API would never accept.
    normalized = obs_number.lstrip("0") or "0"
    if normalized != obs_number:
        print(f"Note: reading observation number {obs_number} as {normalized}.")
        obs_number = normalized

    # Check for Mushroom Observer numbers (5 digits or less)
    if len(obs_number) <= 5:
        short_note = (
            f"The observation number {obs_number} is very short (5 digits or less); "
            "it might be a Mushroom Observer observation rather than an "
            f"iNaturalist one - https://mushroomobserver.org/{obs_number}"
        )
        print(
            f"Note: The observation number {obs_number} is very short (5 digits or less)."
        )
        print("This might be a Mushroom Observer observation rather than iNaturalist.")
        print(f"Consider checking: https://mushroomobserver.org/{obs_number}")

        if auto:
            # In auto mode this is a hint, not a gate. Stopping the whole search
            # to ask about it would make the mode useless to a caller that cannot
            # answer, and the note is carried through to the result either way.
            notices.append(short_note)
        elif not confirm("Continue with iNaturalist search anyway? (y/n): "):
            print("Exiting search.")
            return 0

    if auto:
        outcome = run_auto_mode(
            obs_number,
            resolved,
            digits_off,
            resume_token=args.auto_resume,
            assume_yes=assume_yes,
            as_json=as_json,
            show_progress=show_progress,
            verbose=verbose,
        )
        outcome = outcome._replace(notices=tuple(notices) + tuple(outcome.notices))
        reported = list(outcome.matches)
        if outcome.original is not None:
            reported.append(
                ScoredMatch(outcome.original, [], [], None)
            )
        locations = resolve_observation_locations(
            [match.observation for match in reported]
        )
        print_outcome(outcome, locations, obs_number)
        print(f"\nTotal time: {timedelta(seconds=int(time.time() - start_time))}")
        return _emit(sink, outcome, locations)

    def emit_recorded():
        """Hand the recorded outcome to the JSON writer, when one is listening."""
        if sink is not None and recorded:
            sink["result"] = build_json_result(recorded[0], recorded[1])

    def print_summary(
        found,
        interrupted=False,
        unchecked=0,
        message_callback=print,
        original=None,
        declined=False,
    ):
        """Print the deduplicated match list, search state, and elapsed time.

        ``original`` is the observation the supplied number really points at, when
        one came back, so a JSON consumer can show what that number references
        whether or not it matched the search criteria. ``declined`` marks the exits
        where the user stopped before the variations were checked: those results
        are real, but the search did not run to a conclusion and may not claim to
        have exhausted anything.
        """
        # Defensive API-result deduplication: an ID should only be reported once.
        # This also collapses the original observation if a candidate returned it.
        deduplicated = list({match.get("id"): match for match in found}.values())
        # The supplied observation is reported in its own field even when it did
        # not match, so its location has to be resolved alongside the matches.
        location_labels = resolve_observation_locations(
            deduplicated + ([original] if original is not None else []),
            message_callback=message_callback,
        )
        if interrupted:
            json_status, json_reason = "cancelled", "interrupted"
        elif unchecked:
            json_status, json_reason = "incomplete", "failures"
        elif deduplicated:
            json_status, json_reason = "match_found", "exhausted"
        else:
            json_status, json_reason = "no_match", "exhausted"
        if declined and json_reason == "exhausted":
            # Nothing but the number as supplied was ever checked, so the
            # unsearched variations must not be reported as conclusively absent.
            json_reason = "declined"
        recorded[:] = [
            SearchOutcome(
                status=json_status,
                stop_reason=json_reason,
                matches=tuple(
                    ScoredMatch(observation, [search_mode], [], None)
                    for observation in deduplicated
                ),
                notices=tuple(notices),
                original=original,
                unchecked=unchecked,
                criteria=tuple(resolved.criteria),
                complete=outcome_is_complete(json_status, json_reason),
                exit_code=STATUS_EXIT_CODES.get(json_status, 1),
            ),
            location_labels,
        ]

        if interrupted:
            print("\nSearch interrupted!")
        elif unchecked:
            print("\nSearch incomplete - results may be incomplete.")
        elif declined:
            print("\nSearch stopped at your request - the variations were not checked.")
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
        elif declined:
            print(
                "\nNo matches found. The variations were never checked, so a "
                "matching observation may still exist."
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

    # Holds the outcome the summary described, so --json can report the same
    # thing the human sees without a second pass over the results.
    recorded = []

    # First, check if the original observation number is correct
    if verbose:
        print(
            f"Checking if original observation number {obs_number} matches {search_mode} '{search_term}'..."
        )

    # Fetch the original number without criteria filters so a real observation is
    # not mistaken for a missing one. Project membership, when relevant, is a
    # separate question below.
    original_check_failed = False
    try:
        original_check = fetch_observations([obs_number])
    except ApiError as error:
        original_check = []
        original_check_failed = True
        print(f"Warning: could not check the original observation number - {error}")

    original_project_members = None
    if original_check and search_mode == "project":
        try:
            original_project_members = fetch_project_membership(
                [obs_number], project_id_param
            )
        except ApiError as error:
            original_check_failed = True
            print(
                "Warning: could not check project membership for the original "
                f"observation number - {error}"
            )

    original_match = None
    # What the supplied number actually references, whether or not it matched.
    # Reported in its own JSON field so a caller can show it either way.
    original_observation = original_check[0] if original_check else None

    if original_check:
        match_found = False
        obs = original_check[0]

        if search_mode == "project":
            if (
                original_project_members is not None
                and str(obs.get("id")) in original_project_members
            ):
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
                # Reaching here means the original lookup succeeded, so the only
                # thing left unresolved is the variations the user declined.
                print_summary(
                    [original_match],
                    original=original_observation,
                    declined=True,
                )
                emit_recorded()
                return 0
        elif search_mode == "project" and original_project_members is None:
            print(
                f"The original observation #{obs.get('id', obs_number)} exists, "
                "but its project membership could not be checked."
            )
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
                original=original_observation,
            )
            emit_recorded()
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
        print_summary(
            [original_match] if original_match else [],
            unchecked=1 if original_check_failed else 0,
            original=original_observation,
            declined=True,
        )
        emit_recorded()
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

    print_summary(
        matches,
        interrupted=interrupted,
        unchecked=unchecked,
        original=original_observation,
    )
    emit_recorded()

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
