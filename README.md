# inat.finder.py

**Version:** 1.6
**Author:** Alan Rockefeller
**Release Date:** October 16, 2025

## Overview

inat_finder.py is a command-line tool for finding the correct iNaturalist observation when you have a mistyped observation number. The script works by systematically changing digits in the provided observation number and checking if any of those variations match the specified genus or username in the iNaturalist database.

Since you probably are using this code because you have a DNA barcode which does not go to the correct iNaturalist observation (for example it shows a plant or a bird), you probably know the genus. Alternatively, if you know the username of the observer, you can now search by that instead.

This tool is particularly useful for sequence validators, researchers and iNaturalist power users who need to find specific observations but have encountered typos in their reference numbers.

A Windows .exe is available [here](https://github.com/AlanRockefeller/inat.finder.py/releases)

## Features

- Search by either genus name or iNaturalist username
- Verifies that the specified genus or username exists before searching
- Checks if the original observation number already matches the genus or username before searching for variations
- Generates all possible variations with a configurable number of digits that might be wrong (default: 1) - *Now more robust for multiple digits off!*
- Supports parsing observation numbers directly from iNaturalist URLs
- For short numbers (<9 digits), tries adding up to two digits at beginning and/or end
- For long numbers (>5 digits), tries removing one or two digits.
- Suggests checking Mushroom Observer for very short numbers (≤5 digits)
- Can discover observations with missing digits from both beginning and end simultaneously
- Efficiently queries the iNaturalist API with batched requests of 200 to minimize API calls
- Respects rate limits by making no more than one API call per second
- Shows a progress bar with estimated completion time (ETA now more accurate)
- Provides optional verbose mode for detailed information about each attempt
- Works with any genus/username and observation number combination
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
python inat_finder.py (--genus <genus> | --user <username>) <observation_number_or_url> [options]
```

### Required Arguments

- Either:
  - `--genus <genus>`: The genus name to match (e.g., "Amanita")
  - `--user <username>`: The iNaturalist username to match (e.g., "alan_rockefeller") 
- `observation_number_or_url`: The potentially mistyped iNaturalist observation number or a complete iNaturalist URL

### Options

- `--digits N`: Number of digits that might be wrong (default: 1)
- `--verbose`: Print detailed information about each attempt
- `--no-progress`: Hide the progress bar (progress bar is shown by default)

### Examples

Check for an Amanita observation with one digit off from 123456789:

```bash
python inat_finder.py --genus Amanita 123456789
```

Search for observations by a specific user with one digit off from 123456789:

```bash
python inat_finder.py --user maractwin 123456789
```

Use a full iNaturalist URL instead of just an observation number:

```bash
python inat_finder.py --genus Cystoderma https://www.inaturalist.org/observations/187067126
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

1. The script first verifies that the specified genus or username exists on iNaturalist (API error messages are now more detailed).
2. If a URL is provided, the script extracts the observation number from it.
3. For very short numbers (5 digits or less), it suggests checking Mushroom Observer.
4. The script checks if the original observation number already matches the specified genus or username.
5. If not, it generates all possible variations of the number with the specified number of digits changed (generation logic for multiple differing digits is now corrected and robust).
6. For short numbers (<9 digits), it also generates variations with 1-2 digits added at the beginning and/or end (this logic has been refactored for clarity).
7. For long numbers (>5 digits), it also generates variations with 1-2 digits removed.
8. It batches these variations to efficiently query the iNaturalist API (200 IDs per request).
9. For each observation found, it checks if the genus or username matches what you're looking for.
10. It presents all matching observations, including the creator username and direct links to view them on iNaturalist.org.
11. The progress bar's Estimated Time of Arrival (ETA) is now more accurate due to a refined calculation method.

## Performance Considerations

The number of variations grows exponentially with the number of digits that might be wrong:

- For a 9-digit number with 1 digit off: 81 variations
- For a 9-digit number with 2 digits off: ~3,240 variations
- For a 9-digit number with 3 digits off: ~84,240 variations

For shorter numbers, additional variations are generated by adding digits:
- For an 8-digit number: 320 additional variations (1-2 digits added at either end or both ends)
- For a 7-digit number: 320 additional variations

Be cautious when setting high values for `--digits` as it can result in very long execution times and many API calls.

## Contributing

Contributions to inat.finder.py are welcome! Please feel free to submit pull requests on Github or contact Alan Rockefeller with suggestions for improvements.

If you encounter any bugs or have feature requests, please open an issue on the [GitHub repository](https://github.com/AlanRockefeller/inat.finder.py/issues).

## License

This project is available under the GNU Public License 3.0. See the LICENSE file for more details.

## Acknowledgments

- Thanks to the iNaturalist team for providing the API that makes this tool possible
- Special thanks to all naturalists who contribute their observations to iNaturalist
- Thanks to Mycota Lab, OMDL and Harte Singer for sequencing so many fungi
- Thanks to Alisha Millican and Scott Ostuni for suggesting new features

## Change Log

### Version 1.6 (October 16, 2025)
- For observation numbers with more than 5 digits, the script now also tries removing one or two digits to find a match.

### Version 1.5 (June 2, 2025)
- Corrected a significant bug in generating variations for multiple differing digits.
- Refactored digit addition logic for clarity.
- Improved progress bar ETA accuracy.
- Renamed script to `inat_finder.py` for better module handling.
- Enhanced API error messages.
- Added a comprehensive unit test suite.

### Version 1.4 (April 1, 2025)
- Added ability to search by username instead of genus with the new --user flag
- Modified command-line interface to require either --genus or --user flag
- Added validation to verify if the specified genus exists in iNaturalist taxonomy
- Added validation to verify if the specified username exists on iNaturalist
- Enhanced results display to include creator username for all matches
- Updated documentation to reflect new search capabilities

### Version 1.3 (March 29, 2025)
- Increased batch size to 200 observations per API request (maximum allowed)
- Improved overall execution speed by approximately 85%

### Version 1.2 (March 29, 2025)
- Added Windows executable
- Optimized digit addition algorithm

### Version 1.1 (March 29, 2025)
- Added support for parsing iNaturalist URLs
- Enhanced detection of observation numbers with missing digits at both ends
- Added automatic detection of Mushroom Observer numbers (≤5 digits)
- Improved verbosity control (only shows detailed messages in verbose mode)
- Fixed edge cases in digit addition logic

### Version 1.0 (March 28, 2025)
- Initial release