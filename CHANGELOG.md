# Changelog

All notable changes to the inat.finder.py project will be documented in this file.

## [1.8.0] - 2026-09-10

### Added

- `--auto` works through the common failure modes on its own, so a foray collection whose number does not resolve no longer needs a dozen hand-written re-runs. Thanks to Stephen Russell for the suggestion.
- In auto mode the search criteria stop being mutually exclusive and stop being required: pass whichever of `--genus`, `--family`, `--taxon-id`, `--user` and `--project` you actually know, in any combination, or none at all. With no clues it reports what the number you gave really points at.
- A clue that iNaturalist cannot resolve no longer ends an auto search. An unknown genus, family, taxon, user or project is reported, dropped, and left out of scoring while the other clues carry on - on a foray the mistaken element is as often the genus as the number. Malformed input is still an error, and an unreachable API is still an unreachable API.
- Matches are ranked by how many clues they satisfied rather than filtered by all of them, and each one says which clues matched: `[2 of 3: genus, user; project: unknown]`. When nothing matched everything, the tool says so, because that usually means one of the supplied elements is the wrong one.
- The search now stops the moment a batch contains a full match, part-way through a stage instead of at the end of it. A hit in the first 200 candidates of the widest stage costs one request instead of seven minutes.
- Every voluntary stop prints an `--auto-resume` token that continues from exactly where the run left off, replaying the already-tried candidates offline with no API calls. The token is bound to the observation number, the clues and the `--digits` cap, so a cursor can never be replayed against a search it did not come from.
- `--json` prints one machine-readable result object on stdout with all human narration on stderr, in both auto and normal mode. Its `status` field (`match_found`, `no_match`, `needs_confirmation`, `incomplete`, `cancelled`, `error`) pairs with the documented exit codes, so a script or a web page never has to parse English. Command-line syntax errors are covered too: a missing observation number or two conflicting criteria produce a JSON object explaining the problem rather than a bare status 2.
- The JSON result carries `exit_code`, so a caller holding only stdout does not have to infer the process status, and `complete`, which is true only when the search really ran to a conclusion - a paused, declined or over-sized stage is explicitly not complete even though it is not a failure.
- A stage too large to run unattended reports `needs_confirmation` with a candidate count and time estimate instead of prompting, whenever stdin is not a terminal. A front end can show "about 59,000 more possibilities - continue?" and re-run with `--yes` and the resume token.
- With `--auto`, `--digits N` caps how wide the ladder may go and defaults to 3. Without it, `--digits` means exactly what it always did and still defaults to 1.

### Fixed

- A search that could not check every candidate is never handed a resume cursor, and never reports `needs_confirmation` or "no match" over the gap. The failed batch's IDs are already marked as tried, so a cursor would skip them for good; such a run says it is incomplete and exits `2`, as it always has.
- When `--project` is combined with another clue, a failed project-membership request no longer discards the genus, user and taxon evidence for the same batch. Those matches are kept with project membership marked unknown, the search is reported incomplete, and it exits `2`. This includes the check of the number as supplied, which a resumed search never repeats and whose unanswered questions would otherwise be skipped for good.
- A resume token is now checked for a stage and offset that can actually exist, not only for a valid signature. An edited or stale cursor pointing past the end of the ladder is refused instead of searching nothing and reporting a clean "no match".
- Ctrl+C keeps the matches already found in the stage that was running, rather than only those from stages that had finished.
- A stage too large to search is reported as an error rather than as "no matches found", since that stage was never searched.
- Under `--json`, a genus, family, taxon, user or project that does not exist in a normal (non-`--auto`) search now reports _why_ in the result object, instead of leaving the explanation on stderr and handing the caller an error with no message.
- A cancelled search reports the candidates its stage had really checked, rather than zero, so a progress display stays honest after Ctrl+C.

## [1.7.5] - 2026-09-01

### Added

- `--taxon-id ID` searches by an iNaturalist taxon ID instead of a name. It matches that taxon and everything below it, so you can search any rank - order, tribe, section, subspecies - and it is the way to pick between two taxa that share a name. Example: `python inat_finder.py --taxon-id 48419 123456789`
- `--yes` (`-y`) answers "yes" at every prompt without reading the keyboard, so the tool can run unattended from a script, a scheduled job, or a GUI.
- Search results now show each observation's location alongside its taxon, creator and link.
- When a genus or family name belongs to more than one taxon - Prunella is both a plant and a bird - the tool stops instead of guessing. It lists every candidate with its taxon ID, and prints the exact `--taxon-id` command to re-run with the one you want.
- Before starting, the tool reports how many variations it will check and roughly how long that will take. It asks for confirmation above 5,000 variations, and refuses searches too large to ever finish.
- Missing digits are now tried in the middle of a number, not only at the ends, and adjacent digits are swapped to catch a number typed out of order (123456789 -> 123465789).
- The exit codes are documented, so scripts and GUIs can act on them: `0` the search finished, `1` bad input or an unknown genus, family, taxon, user or project, `2` iNaturalist could not be reached, `130` cancelled with Ctrl+C. A command-line syntax error, such as two conflicting search criteria, also exits `2` but writes a usage message to stderr.

### Changed

- Matches are shown as each batch of results comes back, instead of only after the whole search has finished.
- Ctrl+C now stops cleanly: it prints the matches found so far and exits with status 130.
- Genus and family are matched through the verified taxon ID and the observation's place in the taxonomy rather than by name, so a same-named taxon in another kingdom is no longer a false match.
- The progress bar counts only the variations that were really checked, and shows any that could not be checked as a separate total.
- Very large searches no longer build their whole list of variations up front, so a high `--digits` cannot exhaust memory.
- Every request - name lookups, the first check of your number, and the search itself - now shares one request-per-second limit, and rate-limited or failed requests are retried automatically.

### Fixed

- A search whose requests failed is never reported as "no matches". The tool says the search was incomplete, still shows what it did find, and exits with status 2.
- A network or server problem while checking a genus, family, taxon, user or project is reported as a connection problem, not as "not found".
- If the observation number you supplied already matches and you choose to keep searching, it now appears in the final results - exactly once.
- A sustained outage stops the search early instead of grinding through every remaining batch.
- An unquoted `--project` title that ends in a number now explains how it was read and suggests quoting it.

## [1.7.4] - 2026-08-30

### Added

- `--family NAME` searches by family, alongside `--genus`, `--user` and `--project`. Matching follows the observation's place in the iNaturalist taxonomy.

### Fixed

- Missing digits are now tried at every position in numbers shorter than nine digits, not only at the beginning and end. Thanks to Elora for the feedback!

## [1.7.3] - 2026-08-29

### Fixed

- `--digits N` now searches numbers with one through N wrong digits, which is what "up to N" was always meant to do. Previously it only tried exactly N.

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
