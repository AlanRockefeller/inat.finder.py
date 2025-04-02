# Changelog
All notable changes to the inat.finder.py project will be documented in this file.

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
