# inat.finder.py

**Version:** 1.0  
**Author:** Alan Rockefeller  
**Release Date:** March 28, 2025

## Overview

inat.finder.py is a command-line tool for finding the correct iNaturalist observation when you have a mistyped observation number. The script works by systematically changing digits in the provided observation number and checking if any of those variations match the specified genus in the iNaturalist database.

Since you probably are using this code because you have a DNA barcode which does not go to the correct iNaturalist observation (for example it shows a plant or a bird), you probably know the genus.

This tool is particularly useful for sequence validators, researchers and iNaturalist power users who need to find specific observations but have encountered typos in their reference numbers.

## Features

- Checks if the original observation number already matches the genus before searching for variations
- Generates all possible variations with a configurable number of digits that might be wrong (default: 1)
- Efficiently queries the iNaturalist API with batched requests to minimize API calls
- Respects rate limits by making no more than one API call per second
- Shows a progress bar with estimated completion time by default
- Provides optional verbose mode for detailed information about each attempt
- Works with any genus and observation number combination

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
chmod +x inat_finder.py  # Make the script executable
```

## Usage

```
python inat_finder.py <genus> <observation_number> [options]
```

### Required Arguments

- `genus`: The genus name to match (e.g., "Amanita")
- `observation_number`: The potentially mistyped iNaturalist observation number

### Options

- `--digits N`: Number of digits that might be wrong (default: 1)
- `--verbose`: Print detailed information about each attempt
- `--no-progress`: Hide the progress bar (progress bar is shown by default)

### Examples

Check for an Amanita observation with one digit off from 123456789:

```bash
python inat_finder.py Amanita 123456789
```

Look for a Russula observation with up to 2 digits wrong in the number:

```bash
python inat_finder.py Russula 123456789 --digits-off 2
```

Get detailed information about each observation being checked:

```bash
python inat_finder.py Boletus 123456789 --verbose
```

## How It Works

1. The script first checks if the original observation number already matches the specified genus
2. If not, it generates all possible variations of the number with the specified number of digits changed
3. It batches these variations to efficiently query the iNaturalist API (30 IDs per request)
4. For each observation found, it checks if the genus matches what you're looking for
5. It presents all matching observations, including direct links to view them on iNaturalist.org

## Performance Considerations

The number of variations grows exponentially with the number of digits that might be wrong:

- For a 9-digit number with 1 digit off: 81 variations
- For a 9-digit number with 2 digits off: ~3,240 variations
- For a 9-digit number with 3 digits off: ~84,240 variations

Be cautious when setting high values for `--digits-off` as it can result in very long execution times and many API calls.

## Contributing

Contributions to inat.finder.py are welcome! Please feel free to submit pull requests on Github or contact Alan Rockefeller with suggestions for improvements.

If you encounter any bugs or have feature requests, please open an issue on the [GitHub repository](https://github.com/AlanRockefeller/inat.finder.py/issues).

## License

This project is available under the GNU Public License 3.0. See the LICENSE file for more details.

## Acknowledgments

- Thanks to the iNaturalist team for providing the API that makes this tool possible
- Special thanks to all naturalists who contribute their observations to iNaturalist
- Thanks to Mycota Lab, OMDL and Harte Singer for sequencing so many fungi
