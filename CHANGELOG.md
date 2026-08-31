# Changelog

All notable changes to the inat.finder.py project will be documented in this file.

## [1.8.0] - 2026-08-30

### Fixed

- Never report a failed API batch as "no matches". Batches whose request fails after `api_get()` retries are retried again in an extra round; candidates that still could not be checked are counted, the summary says "Search incomplete - results may be incomplete" instead of "Search complete!", and the process exits with status 2. Matches from successful batches are still shown.
- Distinguish "not found" from an API or network failure when verifying a user, genus, family, or project. Transient failures now raise `ApiError`, which is reported once as a connection problem with exit status 2, instead of printing "Username 'x' not found on iNaturalist." Genuine 404 and empty-result responses still produce the friendly not-found messages.
- Include the original observation in the final results when it already matches and the user chooses to keep searching. Previously the summary could announce a match and then end with "No matches found." The match list is deduplicated by observation ID, so the original is reported exactly once even if a candidate returns it again.
- Stop a matching taxon name and rank from overriding a verified taxon-ID mismatch. Taxon names are not globally unique, so when a verified taxon ID is available the decision is made purely from the observation's `ancestor_ids` taxonomy path. Name-based heuristics remain only as a fallback when no verified ID exists.
- Reject observation numbers with leading zeroes by normalizing them, so candidate counts stay exact and no impossible IDs are generated.

### Changed

- Size the candidate search space before generating it. `CandidatePlan` counts replacement variations with a closed-form calculation and streams them lazily, so `--digits 8` or `--digits 9` no longer builds a multi-hundred-million-entry list. The confirmation prompt for searches over 5,000 candidates now happens before generation, and searches needing more than 1,000,000 API-checked candidates are refused with an explanation.
- Guarantee that every candidate is a distinct numeric observation ID, so no ID is requested twice and the progress total is exact. Adjacent-digit swaps are only added when `--digits` is 1, because a two-digit replacement search already contains them.
- Move API pacing into `api_get()`, so validation lookups, the original-observation check, project and place lookups, and the batched search all share one request-per-second baseline. Explicit `Retry-After` backoffs are counted as the pacing delay rather than sleeping twice.
- Distinguish checked candidates from failed ones in the progress bar: only checked candidates advance the bar, and unchecked candidates are shown as a separate count.
- Report an ambiguous unquoted `--project` title (one ending in a short number) with a note explaining how it was parsed and suggesting quotes.

### Added

- Insert two missing digits at arbitrary positions, not just at the ends, so internal omissions are found. An 8-digit number now generates 3,735 insertion candidates instead of 361.
- Add adjacent-digit transposition as a candidate class (123456789 -> 123465789).
- Fall back to the observation's `place_guess` when structured place resolution yields no standard place, instead of printing "Unknown location".
- Document the exit status codes: 0 finished, 1 bad input or unknown search term, 2 API failure, 130 interrupted.

## [1.7.5] - 2026-08-30

### Added

- Add `--yes` (`-y`) to assume "yes" at every confirmation prompt without reading stdin, so scripted and CI runs are no longer silently declined by the large-search prompt.
- Estimate the number of batches and the API time before searching, and ask for confirmation when a search exceeds 5,000 variations.
- Retry rate-limited and transient API failures with exponential backoff, honouring both the numeric and HTTP-date forms of `Retry-After`, and send a proper `User-Agent` identifying the tool and its repository.

### Changed

- Report matches incrementally as each batch of results arrives instead of only after every batch has completed, so verbose output no longer appears minutes late on long searches.
- Handle Ctrl+C during the search: the progress bar is closed, the matches found so far are printed as partial results, and the process exits with status 130.
- Centralize batching and rate limiting inside `batch_check_observations`, and route all search output through the progress bar so tqdm's display is not corrupted.
- Match genus and family through verified taxon IDs and observation ancestry. The genus name-prefix heuristic is now used only when no verified taxon ID is available; exact rank-and-name comparisons still apply.

### Fixed

- Exclude candidates with a leading zero and deduplicate candidates that resolve to the same numeric observation ID, so the reported variation counts match the IDs actually queried.
- Deduplicate the final match list by observation ID so an observation is never reported twice.
- Parse unquoted `--project` names correctly when followed by a short observation number, and accept scheme-less iNaturalist URLs.
- Treat a 404 from the users endpoint as a clean "not found" instead of printing an API error.
- Fall back from `/taxa/autocomplete` to `/taxa`, and from the direct project endpoint to a project search, when the first lookup returns nothing.
- URL-encode user and project identifiers before putting them into API paths.
- Skip the redundant second lookup of the original observation number when `--digits 0` leaves it as the only candidate.

## [1.7.4] - 2026-08-30

### Added

- Add `--family` as a search criterion, using iNaturalist taxon ancestry to match observations.

### Fixed

- Insert one missing digit at every position in observation numbers shorter than nine digits, instead of checking only the beginning and end. Thanks to Elora for the feedback!

## [1.7.3] - 2026-08-29

### Fixed

- Make `--digits N` search observations with one through N changed digits, matching its documented "up to N" behavior.

## [1.7.2] - 2026-06-04

### Fixed

- Fix project ID parsing and outer batch rate limiting

## [1.7.1] - 2026-01-21

### Fixed

- Fixed crash when API returns `None` for `taxon` or `user` fields.
- Improved robustness of `check_observation_genus` and `check_observation_user` against unexpected data types.
- Corrected inaccurate ETA calculation in the progress bar.
- Improved user input handling to robustly accept various yes/no responses.
- Added validation to prevent errors when no variations are generated.
- Fixed potential division by zero error in ETA calculation.
- Optimized rate limiting to skip unnecessary sleep after the final batch.

## [1.7] - 2026-01-21

### Added

- New `--project` argument to search within a specific iNaturalist project (ID, slug, URL, or title). (Thanks to Ryan Peace for the suggestion!)
- Robust project verification and disambiguation logic.
- Support for unquoted multi-word project titles in the command line.

## [1.6] - 2025-10-16

### Added

- For observation numbers with more than 5 digits, the script now also tries removing one or two digits to find a match. Thanks to Ryan Peace for the suggestion!

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
