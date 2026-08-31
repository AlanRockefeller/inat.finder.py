# inat.finder.py

**Version:** 1.7.5
**Author:** Alan Rockefeller
**Release Date:** August 30, 2026

## Overview

inat_finder.py is a command-line tool for finding the correct iNaturalist observation when you have a mistyped observation number. The script works by systematically changing digits in the provided observation number and checking if any of those variations match the specified genus, family, username, or project in the iNaturalist database.

Since you probably are using this code because you have a DNA barcode which does not go to the correct iNaturalist observation (for example it shows a plant or a bird), you probably know the genus or family. Alternatively, you can search by the observer's username or an iNaturalist project.

This tool is particularly useful for sequence validators, researchers and iNaturalist power users who need to find specific observations but have encountered typos in their reference numbers.

A Windows .exe is available [here](https://github.com/AlanRockefeller/inat.finder.py/releases)

## Features

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
```

### Required Arguments

- Either:
  - `--genus <genus>`: The genus name to match (e.g., "Amanita")
  - `--family <family>`: The family name to match (e.g., "Amanitaceae")
  - `--taxon-id <id>`: The iNaturalist taxon ID to match (e.g., 48419). Matches the taxon itself and every descendant of it, at any rank.
  - `--user <username>`: The iNaturalist username to match (e.g., "alan_rockefeller")
  - `--project <project>`: The iNaturalist project to search within (ID, slug, URL, or title)
- `observation_number_or_url`: The potentially mistyped iNaturalist observation number or a complete iNaturalist URL

### Options

- `--digits N`: Maximum number of digits that might be wrong (default: 1)
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
