# inat.finder.py

**Version:** 1.8.1
**Author:** Alan Rockefeller
**Release Date:** September 10, 2026

## Use it online - no installation required

You can run this tool in your browser at **[dikarya.us/finder](https://dikarya.us/finder)** - no Python, no downloads, no setup.

The instructions below are for those who prefer to run the command-line version locally or understand how it works.

## Overview

inat_finder.py is a command-line tool for finding the correct iNaturalist observation when you have a mistyped observation number. The script works by systematically changing digits in the provided observation number and checking if any of those variations match the specified genus, family, username, or project in the iNaturalist database.

Since you probably are using this code because you have a DNA barcode which does not go to the correct iNaturalist observation (for example it shows a plant or a bird), you probably know the genus or family. Alternatively, you can search by the observer's username or an iNaturalist project.

This tool is particularly useful for sequence validators, researchers and iNaturalist power users who need to find specific observations but have encountered typos in their reference numbers.

A Windows .exe is available [here](https://github.com/AlanRockefeller/inat.finder.py/releases)

![iNaturalist observation finder screenshot](iNaturalist%20observation%20finder%20screenshot.png)

## Features

- **Auto mode (`--auto`)** - one command for a foray: supply whichever clues you have, and the tool works through the common failure modes until something matches
- Search by genus name, family name, iNaturalist taxon ID, iNaturalist username, or iNaturalist project
- Resolve homonyms with `--taxon-id`: when two taxa share a name and rank, the tool lists their IDs and you re-run with the one you want
- Verifies that the specified genus, family, or username exists before searching
- Checks if the original observation number already matches the search criterion before searching for variations
- Generates all possible variations with a configurable number of digits that might be wrong (default: 1) - _Now more robust for multiple digits off!_
- Supports parsing observation numbers directly from iNaturalist URLs
- For short numbers (<9 digits), tries inserting one or two missing digits at any position, including internal positions
- Tries swapping adjacent digits to catch transposition typos (123456789 -> 123465789)
- For long numbers (>5 digits), tries removing one or two digits.
- Suggests checking Mushroom Observer for very short numbers (≤5 digits)
- Can discover observations with missing digits from both beginning and end simultaneously
- Efficiently queries the iNaturalist API with batched requests of 200 to minimize API calls
- Respects rate limits by making no more than one API call per second, across every kind of request
- Shows a progress bar with estimated completion time (ETA now more accurate)
- Retries rate-limited and transient API failures with backoff, honouring `Retry-After`
- Reports matches as each batch of results comes back, rather than only at the end
- Counts the search space before generating it, asks for confirmation above 5,000 candidates, and refuses searches that could never finish
- Streams candidates instead of building them all in memory, so a large `--digits` cannot exhaust RAM
- Never reports a failed API batch as "no matches": failed batches are retried, and an incomplete search says so and exits nonzero
- Stops cleanly on Ctrl+C, printing the matches found so far
- Provides optional verbose mode for detailed information about each attempt
- Works with genus, family, username, or project criteria
- `--json` prints one machine-readable result object, for use from a script or a web front end
- Includes a comprehensive unit test suite for maintainability.

## Installation

### Prerequisites

- Python 3.6 or higher
- Required Python packages:
  - `requests`
  - `tqdm`

### Install Dependencies

```bash
pip install requests tqdm
```

### Download the Script

```bash
git clone https://github.com/AlanRockefeller/inat.finder.py.git
cd inat.finder.py
# The script is now named inat_finder.py
chmod +x inat_finder.py  # Make the script executable
```

Or just copy the code from Github and paste it into a file named `inat_finder.py`.

## Usage

```
python inat_finder.py (--genus NAME | --family NAME | --taxon-id ID | --user USER | --project PROJECT) OBSERVATION [options]
python inat_finder.py --auto [--genus NAME] [--user USER] [...] OBSERVATION [options]
```

### Required Arguments

- Either:
  - `--genus <genus>`: The genus name to match (e.g., "Amanita")
  - `--family <family>`: The family name to match (e.g., "Amanitaceae")
  - `--taxon-id <id>`: The iNaturalist taxon ID to match (e.g., 48419). Matches the taxon itself and every descendant of it, at any rank.
  - `--user <username>`: The iNaturalist username to match (e.g., "alan_rockefeller")
  - `--project <project>`: The iNaturalist project to search within (ID, slug, URL, or title)
- `observation_number_or_url`: The potentially mistyped iNaturalist observation number or a complete iNaturalist URL

With `--auto` the search criteria stop being mutually exclusive and stop being
required: pass any combination of them, or none.

### Options

- `--auto`: Work through the common failure modes in order, widening the search until something matches. See [Auto mode](#auto-mode).
- `--digits N`: Maximum number of digits that might be wrong (default: 1). With `--auto` it caps how wide the ladder may go, and defaults to 3.
- `--auto-resume TOKEN`: Continue an `--auto` search from where a previous run stopped, using the token that run printed.
- `--json`: Print one machine-readable JSON object on stdout, with all human narration on stderr. See [JSON output](#json-output).
- `--verbose`: Print detailed information about each attempt
- `--no-progress`: Hide the progress bar (progress bar is shown by default)
- `--yes`, `-y`: Assume "yes" at every confirmation prompt. The tool never reads stdin, which makes it safe to run from scripts and CI where a large search would otherwise be declined automatically.

### Examples

Check for an Amanita observation with one digit off from 123456789:

```bash
python inat_finder.py --genus Amanita 123456789
```

Search more broadly for an observation in the family Amanitaceae:

```bash
python inat_finder.py --family Amanitaceae 12345678
```

Search by an explicit iNaturalist taxon ID, which matches that taxon and all of its descendants:

```bash
python inat_finder.py --taxon-id 48419 123456789
```

`--taxon-id` is the option to reach for when:

- multiple taxa share the same name (the same genus name can exist in more than one kingdom), and the tool refuses to guess between them;
- you already know the iNaturalist taxon ID and want to skip the name lookup;
- you want to search an arbitrary rank - order, tribe, section, subspecies - that `--genus` and `--family` cannot express.

Search for observations by a specific user with one digit off from 123456789:

```bash
python inat_finder.py --user maractwin 123456789
```

Use a full iNaturalist URL instead of just an observation number:

```bash
python inat_finder.py --genus Cystoderma https://www.inaturalist.org/observations/187067126
```

Search within a specific project (by ID, slug, or title):

```bash
python inat_finder.py --project "Coastal and Marine Mycology 2024" 123456789
```

Look for a Russula observation with up to 2 digits wrong in the number:

```bash
python inat_finder.py --genus Russula 123456789 --digits 2
```

Get detailed information about each observation being checked:

```bash
python inat_finder.py --genus Boletus 123456789 --verbose
```

## Auto mode

Auto mode exists for foray and DNA-barcoding work, where a sequence points at an
iNaturalist number that does not resolve and you are left guessing which knob to
turn. Instead of re-running the tool by hand with a wider `--digits`, then again
without the genus, `--auto` works through the hypotheses for you and stops at the
first stage that turns up an observation matching everything you told it.

```bash
python inat_finder.py --auto --genus Cystoderma 187067127
python inat_finder.py --auto --genus Amanita --user alan_rockefeller --project "Fungi Map" 187067127
python inat_finder.py --auto 187067126
```

**Any of the elements may be missing.** Pass the clues you have and leave out the
ones you don't. With no clues at all, auto mode simply tells you what the number
you gave actually points at, rather than listing every observation that happens to
exist nearby.

**A clue that turns out to be wrong does not stop the search.** On a foray the
mistaken element is as often the genus as the number, so a genus, family, taxon,
user or project that iNaturalist cannot resolve is reported, dropped, and left out
of scoring while the remaining clues carry on. Malformed input - `--taxon-id abc` -
is still an error, and a network failure is still a network failure: a clue is
never silently discarded because iNaturalist was unreachable.

**Results are ranked, not filtered.** An observation is reported if it matches at
least one clue, and the ones matching the most clues come first:

```
1. Observation #187067126 - Amanita muscaria   [2 of 3: genus, user; project: unknown]
```

If nothing matched every clue, the tool says so - a partial match usually means one
of the elements you supplied is the wrong one.

**One clue on its own is weak evidence.** iNaturalist hands out observation numbers
in the order things are uploaded, so the numbers either side of a mistyped one very
often belong to the same person, and frequently to the same species. A search with
only `--user` or only `--genus` will often find several neighbours that all match
perfectly. Auto mode lists all of them and warns you rather than picking one. Add a
second clue when you can.

### The ladder

| Stage | What it searches                                                                    |
| ----- | ----------------------------------------------------------------------------------- |
| 0     | the number exactly as supplied - it may be right, and something else wrong          |
| 1     | one substituted digit, plus adjacent swaps, plus the missing/extra-digit heuristics |
| 2     | two substituted digits not already tried                                            |
| 3     | three substituted digits not already tried                                          |

`--digits N` caps the ladder (default 3). No observation ID is ever requested twice
across the whole ladder, and each stage prints how many new candidates it holds -
the counts depend on the digits in your number, so they are printed rather than
documented.

When a stage turns up a full match, that whole stage is checked before the search
stops. You get every observation that matched equally well, instead of only the one
that happened to be looked at first. This is cheap: stage 1 of a nine-digit number
is 133 candidates, a single request.

Stages holding more than 5,000 candidates are the exception. Those still stop as
soon as a batch contains a full match, part-way through the stage, so a hit in the
first 200 candidates of stage 3 costs one request instead of several minutes. If
that answer looks wrong, the resume token carries on through the rest of the stage.

### Stopping and continuing

Whenever auto mode stops voluntarily - because it found a full match, or because
the next stage is large enough to want confirmation - it prints a resume token:

```
To keep searching from here, re-run with:
  --auto-resume v1:2:200:9f3c1a4e
```

Re-running with that token replays the already-tried candidates offline, with no
API calls, and carries on from exactly where the previous run stopped. The token is
bound to the observation number, the clues and the `--digits` cap, so a cursor can
never be replayed against a search it did not come from.

A search that stopped because _requests failed_ gets no token. Its failed batches
are already marked as tried, so a cursor would skip them for good; that run exits
`2` and should be retried from the beginning.

## JSON output

`--json` prints exactly one JSON object on stdout and sends every human line to
stderr. It works in both auto and normal mode.

```bash
python inat_finder.py --auto --genus Cystoderma 187067127 --json
```

**Every** invocation carrying `--json` writes exactly one JSON document to stdout,
including the ones a command-line syntax error would normally end - a missing
observation number, an unknown option, two conflicting criteria. There is no case
where a caller gets a bare exit status and nothing to show a user.

```json
{
  "version": 1,
  "status": "match_found",
  "complete": true,
  "exit_code": 0,
  "stop_reason": "full_match",
  "stage": 1,
  "attempted": 133,
  "stage_total": 133,
  "unchecked": 0,
  "clues": ["genus"],
  "matches": [
    {
      "id": 187067126,
      "taxon": "Cystoderma carcharias fallax",
      "user": "betweenthelyons",
      "location": "Fresno Co. US CA",
      "url": "https://www.inaturalist.org/observations/187067126",
      "score": 1,
      "matched": ["genus"],
      "unknown": [],
      "stage": 1
    }
  ],
  "original": { "id": 187067127, "taxon": "Rickenella fibula", "...": "..." },
  "unusable_clues": [],
  "notices": [],
  "stages": [{ "stage": 0, "total": 1, "attempted": 1, "unchecked": 0 }],
  "resume": { "stage": 2, "offset": 0, "token": "v1:2:0:35050284" },
  "message": null
}
```

`status` is one of:

| Status               | Exit | Meaning                                                                                |
| -------------------- | ---- | -------------------------------------------------------------------------------------- |
| `match_found`        | 0    | at least one observation matched at least one clue                                     |
| `no_match`           | 0    | the search finished and nothing matched                                                |
| `needs_confirmation` | 0    | the next stage is large; ask the user, then re-run with `--yes` and the `resume` token |
| `incomplete`         | 2    | some candidates could not be checked; `resume` is always `null`                        |
| `cancelled`          | 130  | interrupted with Ctrl+C; whatever was found first is still reported                    |
| `error`              | 1    | the search could not run; `stop_reason` says why - see below                           |
| `error`              | 2    | a command-line syntax error; `stop_reason` is `usage`                                  |

An `error` exiting 1 carries one of three reasons:

| `stop_reason` | Meaning                                                                                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_input`   | a value the tool rejected - a malformed `--taxon-id`, a non-numeric observation number, or, without `--auto`, a genus, family, taxon, user or project that does not exist |
| `bad_resume`  | the resume token is malformed, belongs to a different search, or points outside the ladder                                                                                |
| `too_large`   | a stage would need more candidates than the tool will ever check; lower `--digits`                                                                                        |

`exit_code` repeats the process's exit status, so a caller that only has stdout
never has to infer it. Note that `error` covers both statuses above: `stop_reason`
tells them apart, matching the long-standing convention that argparse's own errors
exit 2 while the script's validation exits 1. Either way `message` carries the same
explanation that was printed to stderr, and `unusable_clues` names the clue at
fault when there is one - so a page never has to scrape stderr to tell the user
what went wrong.

`complete` is **not** the same as "did not fail". It is true only when the search
actually ran to a conclusion, so `needs_confirmation`, a declined stage and a stage
too large to run are all `"complete": false` despite exiting 0 or 1. Use it to
decide whether there is more searching to do.

`original` is whatever the number you supplied actually points at, whether or not it
matched. `unknown` on a match lists clues that could not be checked - in practice
project membership, when that request failed while the observation itself came back.

### Calling it from a web front end

`needs_confirmation` is the hook for a "keep searching" button: the object carries
`estimated_candidates` and `estimated_seconds`, so the page can ask _"Deep search:
about 59,000 more possibilities. Continue?"_ and re-run with `--yes` and the resume
token when the user says yes. Auto mode never prompts when stdin is not a terminal,
so it cannot block waiting for an answer that will not come.

Build the command as an **argv array**, never as a shell string assembled from
genus, user or project values - a project title with a quote or a space in it is a
shell-parsing problem waiting to happen:

```python
subprocess.run(
    ["python", "inat_finder.py", "--auto", "--json",
     "--project", project_title, observation_number],
    capture_output=True, text=True,
)
```

**Merge resumed results, do not replace them.** A resumed run reports only what
_it_ found; the token carries a position, not the earlier run's matches. Keep the
matches you already have and merge the new ones by observation `id`, taking the
higher `score` when the same observation appears twice.

## How It Works

1. The script first verifies that the specified genus, family, taxon ID, or username exists on iNaturalist (API error messages are now more detailed).
2. If a URL is provided, the script extracts the observation number from it.
3. For very short numbers (5 digits or less), it suggests checking Mushroom Observer.
4. The script checks if the original observation number already matches the specified search criterion.
5. It counts the candidate search space - variations with between one and the specified maximum number of digits changed - before generating anything, and asks for confirmation (or refuses) if it is very large.
6. For short numbers (<9 digits), it also inserts one or two digits at every possible position, internal positions included.
7. For long numbers (>5 digits), it also generates variations with 1-2 digits removed. Adjacent digit swaps are added when `--digits 1` does not already cover them.
8. It streams these variations, in batches of 200 IDs per request, to the iNaturalist API. Batches whose request fails are retried, and any candidates that still could not be checked are reported as an incomplete search.
9. For each observation found, it checks if the selected genus, family, taxon ID, username, or project matches what you're looking for. Genus, family and `--taxon-id` searches all decide membership from the observation's taxonomic ancestry, never from a taxon name.
10. It presents all matching observations, including the creator username and direct links to view them on iNaturalist.org.
11. The progress bar's Estimated Time of Arrival (ETA) is now more accurate due to a refined calculation method.

## Performance Considerations

The number of variations grows exponentially with the number of digits that might be wrong. Candidates with a leading zero are discarded, and candidates that are the same numeric ID are counted only once, so the totals below are the unique IDs actually queried:

- For a 9-digit number with 1 digit off: 80 variations
- For a 9-digit number with up to 2 digits off: 2,924 variations
- For a 9-digit number with up to 3 digits off: 61,892 variations
- For a 9-digit number with up to 4 digits off: 847,754 variations

For shorter numbers, additional variations are generated by adding digits at any position:

- For an 8-digit number: 3,735 insertion candidates (81 of them with a single digit added)
- For a 7-digit number: 2,997 insertion candidates (72 of them with a single digit added)

Numbers longer than 5 digits also get up to 45 more candidates from removing one or two digits, plus up to 8 adjacent-digit swaps. Altogether, a 9-digit number with 1 digit off checks 133 unique IDs, and an 8-digit number with 1 digit off checks 3,849.

The candidate count is calculated before any candidate is built. Searches over 5,000 variations print a time estimate and ask for confirmation first; pass `--yes` to skip the prompt. Searches needing more than 1,000,000 API-checked candidates (for example `--digits 8` or `--digits 9` on a 9-digit number) are refused outright, because they could not finish in a reasonable time.

Be cautious when setting high values for `--digits` as it can result in very long execution times and many API calls.

## Exit Status

The same codes apply in auto mode, and `--json` reports them as a `status` string
as well - see the table under [JSON output](#json-output).

- `0` - the search finished, whether or not matches were found
- `1` - bad input, or the genus, family, taxon ID, user, or project does not exist
- `2` - the search could not be completed because iNaturalist could not be reached
- `130` - the search was interrupted with Ctrl+C

Command-line syntax errors caught by the argument parser - an unknown option, a missing observation number, or two conflicting search criteria such as `--genus` together with `--taxon-id` - also exit `2`, with a usage message on stderr. Values the script validates itself, such as an invalid `--taxon-id`, exit `1`. A caller that needs to distinguish a usage error from an unreachable API can check stderr, since the API failure message goes to stdout.

## Contributing

Contributions to inat.finder.py are welcome! Please feel free to submit pull requests on Github or contact Alan Rockefeller with suggestions for improvements.

If you encounter any bugs or have feature requests, please open an issue on the [GitHub repository](https://github.com/AlanRockefeller/inat.finder.py/issues).

## License

This project is available under the GNU Public License 3.0. See the LICENSE file for more details.

## Acknowledgments

- Thanks to the iNaturalist team for providing the API that makes this tool possible
- Special thanks to all naturalists who contribute their observations to iNaturalist
- Thanks to Mycota Lab, OMDL and Harte Singer for sequencing so many fungi
- Thanks to Alisha Millican, Elora, Ryan Peace and Scott Ostuni for suggesting new features
