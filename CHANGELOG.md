# Changelog
All notable changes to the inat.finder.py project will be documented in this file.

## [1.6] - 2025-10-16
### Added
- For observation numbers with more than 5 digits, the script now also tries removing one or two digits to find a match.  Thanks to Ryan Peace for the suggestion!

## [1.5] - 2025-06-02
### Fixed
- Corrected a significant bug in `generate_digit_variations` for `digits_off > 1` which led to incorrect/incomplete results. The function now accurately generates all unique variations.

### Changed
- Refactored `generate_digit_additions` for improved clarity and conciseness using `itertools.product`.
- Improved Progress Bar ETA calculation in `main` function to use a moving average of recent batch processing times for better accuracy.
- Renamed script from `inat.finder.py` to `inat_finder.py` 
- Enhanced API error messages in `verify_genus_exists` and `verify_user_exists` to be more specific by including HTTP status codes when available.

### Added
- Added a comprehensive suite of unit tests (`test_inat_finder.py`) covering key functions: `generate_digit_variations`, `generate_digit_additions`, and `parse_inat_url`.

## [1.4] - 2025-04-01
### Added
- New --user argument to search by username instead of genus (Thanks to Scott Ostuni for the suggestion)
- Modified the script to require either --genus or --user flag (previous positional genus argument no longer supported)
- Added verification to check if the specified genus exists in iNaturalist taxonomy
- Added verification to check if the specified username exists on iNaturalist
- Enhanced results display to include the creator username for all matches

### Changed
- Command-line interface now requires --genus or --user flag instead of positional arguments
- Updated help text to explain the new command-line options
- Updated internal documentation to reflect new search capabilities

## [1.3] - 2025-03-29
### Enhanced
- Increased batch size from 30 to 200 observations per API request (the maximum allowed) to significantly reduce the number of API calls
- Improved overall execution speed by approximately 85% due to fewer API requests and connection delays

## [1.2] - 2025-03-29
### Fixed
- Included a Windows .exe
- Optimized digit addition algorithm to significantly reduce the number of variations generated
- Fixed help text formatting to properly display paragraph breaks in the documentation

## [1.1] - 2025-03-29
### Added
- Support for parsing observation numbers directly from iNaturalist URLs
- Feature to detect and suggest Mushroom Observer for very short numbers (≤5 digits)
- Capability to handle missing digits at both the beginning and end simultaneously (Thanks to Alisha Millican for the suggestion)
- For observation numbers with fewer than 9 digits, now tries adding up to two digits at the beginning and/or end
- More comprehensive documentation about the new features

### Changed
- Improved verbosity control - detailed information is only shown when using the --verbose flag
- Enhanced the digit addition logic to be more comprehensive
- Updated command-line help text to reflect new capabilities
- Made argument parsing more flexible to handle both numbers and URLs

## [1.0] - 2025-03-28
### Added
- Initial release of inat.finder.py
- Support for finding iNaturalist observations with mistyped digits
- Configurable number of digits that might be wrong
- Progress bar with estimated completion time
- Verbose mode for detailed output
- Efficient batched API requests
- Rate limiting to respect iNaturalist API guidelines
