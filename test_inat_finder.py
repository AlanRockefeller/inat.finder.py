import contextlib
import itertools
import math
import types
import unittest
from unittest.mock import Mock, patch

import inat_finder
from inat_finder import (
    ApiError,
    CandidatePlan,
    count_digit_variations,
    generate_digit_additions,
    generate_digit_removals,
    generate_digit_transpositions,
    generate_digit_variations,
    parse_inat_url,
    preprocess_argv_for_project_name,
    unique_by_integer_value,
)


class TestInatFinderFunctions(unittest.TestCase):
    def setUp(self):
        # The shared limiter carries state between calls; keep tests independent.
        inat_finder.RATE_LIMITER.reset()

    # Test methods for generate_digit_variations
    def test_gdv_no_change(self):
        self.assertEqual(generate_digit_variations("123", 0), ["123"])
        self.assertEqual(generate_digit_variations("123", -1), ["123"])

    def test_gdv_single_digit_off(self):
        # For "12", digits_off=1
        # Expected: 22, 32, ..., 92 (8 variations for first digit)
        #           10, 11, 13, ..., 19 (9 variations for second digit)
        # Total = 8 + 9 = 17; leading zero is not a valid observation ID.
        variations = generate_digit_variations("12", 1)
        self.assertEqual(len(variations), 17)
        self.assertNotIn("02", variations)
        self.assertIn("92", variations)  # Changed first digit
        self.assertIn("10", variations)  # Changed second digit
        self.assertIn("19", variations)  # Changed second digit
        self.assertNotIn("12", variations)  # Original number should not be present

        # For "7", digits_off=1
        # Expected: 0, 1, 2, 3, 4, 5, 6, 8, 9 (9 variations)
        variations_single = generate_digit_variations("7", 1)
        self.assertEqual(len(variations_single), 9)
        for i in range(10):
            if i == 7:
                self.assertNotIn(str(i), variations_single)
            else:
                self.assertIn(str(i), variations_single)

    def test_gdv_multiple_digits_off(self):
        # For "12", digits_off=2 includes one- and two-digit changes.
        # Pos 0 (was '1') can be any of 8 digits (2-9)
        # Pos 1 (was '2') can be any of 9 digits (0, 1, 3-9)
        # Total variations = 8 + 9 + (8 * 9) = 89
        variations = generate_digit_variations("12", 2)
        self.assertEqual(len(set(variations)), 89)
        self.assertEqual(len(variations), 89)

        # Specific checks:
        self.assertNotIn("00", variations)
        self.assertNotIn("01", variations)
        self.assertIn("20", variations)  # 1->2, 2->0
        self.assertIn("98", variations)  # 1->9, 2->8
        self.assertNotIn("12", variations)  # Original
        self.assertNotIn("02", variations)
        self.assertIn("10", variations)  # One digit changed from "12"

        # For "123", digits_off=2
        # Combinations of 2 positions to change: (0,1), (0,2), (1,2)
        # For (0,1) changing, '1' and '2' change, '3' stays: 9*9*1 = 81 variations (e.g., "003")
        # For (0,2) changing, '1' and '3' change, '2' stays: 9*9*1 = 81 variations (e.g., "020")
        # For (1,2) changing, '2' and '3' change, '1' stays: 9*9*1 = 81 variations (e.g., "100")
        # Changes involving the first position have eight choices, not nine.
        variations_123_2_off = generate_digit_variations("123", 2)
        self.assertEqual(len(variations_123_2_off), 251)
        self.assertNotIn("003", variations_123_2_off)
        self.assertNotIn("020", variations_123_2_off)
        self.assertIn("100", variations_123_2_off)  # 2->0, 3->0, 1 stays
        self.assertNotIn("123", variations_123_2_off)  # Original
        self.assertNotIn("023", variations_123_2_off)
        self.assertIn("120", variations_123_2_off)  # Only 1 digit changed

    def test_gdv_includes_fewer_changes_than_maximum(self):
        variations = generate_digit_variations("395286405", 3)
        self.assertIn("395286406", variations)

    def test_gdv_uniqueness(self):
        # Variations with different changed-position sets should remain unique.
        variations = generate_digit_variations(
            "111", 1
        )  # Should be "011", "211", ..., "101", "121", ...
        self.assertEqual(len(variations), len(set(variations)))
        variations_multi = generate_digit_variations("11", 2)
        self.assertEqual(len(variations_multi), len(set(variations_multi)))

    def test_gdv_empty_input(self):
        # Based on current logic, empty string for number_str:
        # digits_off = 0 -> [""]
        # digits_off = 1 -> range(len("")) is empty, loop doesn't run, returns []
        # digits_off = 2 (or more) -> combinations behavior with empty range?
        # itertools.combinations(range(0), 2) is empty. So loop won't run.
        self.assertEqual(generate_digit_variations("", 0), [""])
        self.assertEqual(generate_digit_variations("", 1), [])
        self.assertEqual(generate_digit_variations("", 2), [])

    # Test methods for generate_digit_additions
    def test_gda_all_additions(self):
        variations = generate_digit_additions("1")

        self.assertNotIn("01", variations)
        self.assertIn("10", variations)  # single suffix
        self.assertNotIn("001", variations)
        self.assertIn("100", variations)  # double suffix
        self.assertNotIn("010", variations)

        self.assertEqual(len(variations), len(set(variations)))
        self.assertTrue(
            all(len(value) == 1 or not value.startswith("0") for value in variations)
        )

    def test_gda_max_digits_respected(self):
        variations_one_digit = generate_digit_additions("1", max_added_digits=1)
        self.assertEqual(len(variations_one_digit), 18)
        self.assertNotIn("01", variations_one_digit)
        self.assertIn("10", variations_one_digit)
        self.assertNotIn("001", variations_one_digit)  # double prefix
        self.assertNotIn("100", variations_one_digit)  # double suffix
        self.assertNotIn("010", variations_one_digit)  # pre+suff

    def test_gda_inserts_one_missing_digit_in_the_middle(self):
        variations = generate_digit_additions("12345678", max_added_digits=1)

        self.assertIn("123495678", variations)
        self.assertNotIn("012345678", variations)
        self.assertIn("123456789", variations)
        # Adjacent insertion of an identical digit generates the same numeric ID.
        self.assertEqual(len(variations), 81)

    def test_variations_have_expected_three_digit_count(self):
        variations = generate_digit_variations("123", digits_off=1)
        self.assertEqual(len(variations), 26)
        self.assertTrue(all(not value.startswith("0") for value in variations))

    def test_integer_value_deduplication(self):
        self.assertEqual(
            unique_by_integer_value(["023", "23", "0024", "24", "23"]), ["23", "24"]
        )

    # Test methods for parse_inat_url
    def test_piu_valid_url(self):
        self.assertEqual(
            parse_inat_url("https://www.inaturalist.org/observations/12345"), "12345"
        )
        self.assertEqual(
            parse_inat_url("http://www.inaturalist.org/observations/67890"), "67890"
        )
        self.assertEqual(
            parse_inat_url("https://inaturalist.org/observations/123"), "123"
        )  # No www

    def test_piu_url_with_query_params(self):
        self.assertEqual(
            parse_inat_url(
                "https://www.inaturalist.org/observations/12345?param=value&another=true"
            ),
            "12345",
        )

    def test_piu_not_a_url(self):
        self.assertEqual(parse_inat_url("12345"), "12345")  # Should return itself

    def test_piu_scheme_less_url(self):
        self.assertEqual(
            parse_inat_url("www.inaturalist.org/observations/123456"), "123456"
        )

    def test_piu_invalid_url_format(self):
        # Different site - current behavior extracts if 'observations/\d+' is found
        self.assertEqual(
            parse_inat_url("https://www.example.com/observations/12345"), "12345"
        )
        # Incorrect iNat path
        self.assertEqual(
            parse_inat_url("https://www.inaturalist.org/obs/12345"),
            "https://www.inaturalist.org/obs/12345",
        )
        self.assertEqual(
            parse_inat_url("https://www.inaturalist.org/observations/"),
            "https://www.inaturalist.org/observations/",
        )

    def test_piu_url_no_number(self):
        self.assertEqual(
            parse_inat_url("https://www.inaturalist.org/observations/abc"),
            "https://www.inaturalist.org/observations/abc",
        )
        self.assertEqual(
            parse_inat_url("https://www.inaturalist.org/observations/"),
            "https://www.inaturalist.org/observations/",
        )

    def test_project_preprocess_preserves_five_digit_observation_id(self):
        argv = ["inat_finder.py", "--project", "my-slug", "12345"]
        self.assertEqual(
            preprocess_argv_for_project_name(argv),
            ["inat_finder.py", "--project", "my-slug", "12345"],
        )

    def test_project_preprocess_short_observation_and_trailing_flag(self):
        for observation_id in ("1", "12", "123", "1234"):
            with self.subTest(observation_id=observation_id):
                argv = [
                    "inat_finder.py",
                    "--project",
                    "Some",
                    "Project",
                    observation_id,
                    "--verbose",
                ]
                self.assertEqual(
                    preprocess_argv_for_project_name(argv),
                    [
                        "inat_finder.py",
                        "--project",
                        "Some Project",
                        observation_id,
                        "--verbose",
                    ],
                )

    def test_project_preprocess_quoted_name_and_observation_url(self):
        quoted = ["inat_finder.py", "--project", "Some Project", "42"]
        self.assertEqual(preprocess_argv_for_project_name(quoted), quoted)
        url = "www.inaturalist.org/observations/1234"
        argv = ["inat_finder.py", "--project", "Some", "Project", url, "--no-progress"]
        self.assertEqual(
            preprocess_argv_for_project_name(argv),
            ["inat_finder.py", "--project", "Some Project", url, "--no-progress"],
        )

    def test_project_preprocess_keeps_year_in_unquoted_project_name(self):
        argv = [
            "inat_finder.py",
            "--project",
            "Coastal",
            "and",
            "Marine",
            "Mycology",
            "2024",
            "12345",
        ]
        self.assertEqual(
            preprocess_argv_for_project_name(argv),
            [
                "inat_finder.py",
                "--project",
                "Coastal and Marine Mycology 2024",
                "12345",
            ],
        )

    # Test methods for generate_digit_removals
    def test_gdr_remove_one(self):
        variations = generate_digit_removals("123", max_removed_digits=1)
        self.assertEqual(len(variations), 3)
        self.assertCountEqual(variations, ["12", "13", "23"])

    def test_gdr_remove_up_to_two(self):
        variations = generate_digit_removals("1234", max_removed_digits=2)
        # remove 1: 123, 124, 134, 234 (4)
        # remove 2: 12, 13, 14, 23, 24, 34 (6)
        expected = ["123", "124", "134", "234", "12", "13", "14", "23", "24", "34"]
        self.assertEqual(len(variations), 10)
        self.assertCountEqual(variations, expected)

    def test_gdr_uniqueness(self):
        variations = generate_digit_removals("112", max_removed_digits=1)
        self.assertEqual(len(variations), 2)  # "11", "12"
        self.assertCountEqual(variations, ["11", "12"])

    def test_gdr_empty_and_short(self):
        self.assertEqual(generate_digit_removals("", 2), [])
        self.assertEqual(generate_digit_removals("1", 2), [])
        self.assertEqual(generate_digit_removals("1", 1), [])
        self.assertCountEqual(generate_digit_removals("12", 2), ["1", "2"])

    def test_family_match_uses_observation_ancestor_ids(self):
        observation = {
            "taxon": {
                "id": 1234,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestor_ids": [1, 2, 60773, 999],
            }
        }

        self.assertTrue(
            inat_finder.check_observation_family(observation, "Amanitaceae", 60773)
        )
        self.assertFalse(
            inat_finder.check_observation_family(observation, "Russulaceae", 48797)
        )

    def test_family_match_accepts_family_as_observation_taxon(self):
        observation = {
            "taxon": {
                "id": 60773,
                "name": "Amanitaceae",
                "rank": "family",
                "ancestor_ids": [1, 2],
            }
        }

        self.assertTrue(
            inat_finder.check_observation_family(observation, "Amanitaceae", 60773)
        )

    def test_taxon_matching_direct_ancestor_and_malformed_inputs(self):
        direct = {"taxon": {"id": 47158, "name": "Amanita", "rank": "genus"}}
        ancestor = {
            "taxon": {"id": 1, "ancestor_ids": [47158], "name": "Other species"}
        }
        self.assertTrue(inat_finder.check_observation_genus(direct, "Amanita", 47158))
        self.assertTrue(inat_finder.check_observation_genus(ancestor, "Amanita", 47158))
        self.assertFalse(inat_finder.check_observation_genus({}, "Amanita", 47158))
        self.assertFalse(
            inat_finder.check_observation_genus({"taxon": []}, "Amanita", 47158)
        )
        self.assertFalse(inat_finder.check_observation_user([], "someone"))
        self.assertFalse(inat_finder.check_observation_user({"user": "bad"}, "someone"))

    def test_verified_taxon_id_disables_name_prefix_fallback(self):
        # "Amanita example" starts with the target genus name, but the verified ID does
        # not appear anywhere in the taxonomy, so the prefix heuristic stays off.
        observation = {
            "taxon": {"id": 99, "name": "Amanita example", "rank": "species"}
        }
        self.assertFalse(
            inat_finder.check_observation_genus(observation, "Amanita", 47158)
        )
        self.assertFalse(
            inat_finder.check_observation_genus(
                {
                    "taxon": {
                        "id": 99,
                        "name": "Amanita example",
                        "rank": "species",
                        "ancestors": [
                            {"id": 5, "name": "Amanitaceae", "rank": "family"}
                        ],
                    }
                },
                "Amanita",
                47158,
            )
        )
        self.assertTrue(inat_finder.check_observation_genus(observation, "Amanita"))

    def test_verified_taxon_id_beats_same_name_ancestor(self):
        # Taxon names are not globally unique, so an expanded ancestor that merely
        # shares the name and rank must not override a verified ID mismatch.
        observation = {
            "taxon": {
                "id": 99,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestors": [{"id": 47158, "name": "Amanita", "rank": "genus"}],
            }
        }
        self.assertFalse(
            inat_finder.check_observation_genus(observation, "Amanita", 12345)
        )
        # The same observation matches when the verified ID is the one it carries.
        self.assertTrue(
            inat_finder.check_observation_genus(observation, "Amanita", 47158)
        )

    def test_same_name_taxon_with_different_id_is_not_a_match(self):
        # "Prunella" is both a plant genus and a bird genus. Searching for the bird
        # genus must not match a plant observation with the same genus name.
        plant = {
            "taxon": {
                "id": 500,
                "name": "Prunella vulgaris",
                "rank": "species",
                "ancestor_ids": [47126, 999],
            }
        }
        bird_genus_id = 13094
        self.assertFalse(
            inat_finder.check_observation_genus(plant, "Prunella", bird_genus_id)
        )
        # The observation's own taxon carrying the shared name is also not enough.
        plant_genus = {
            "taxon": {
                "id": 999,
                "name": "Prunella",
                "rank": "genus",
                "ancestor_ids": [47126, 999],
            }
        }
        self.assertFalse(
            inat_finder.check_observation_genus(plant_genus, "Prunella", bird_genus_id)
        )
        self.assertTrue(
            inat_finder.check_observation_genus(plant_genus, "Prunella", 999)
        )

    def test_family_name_match_does_not_override_id_mismatch(self):
        observation = {
            "taxon": {
                "id": 7,
                "name": "Amanitaceae",
                "rank": "family",
                "ancestor_ids": [1, 7],
            }
        }
        self.assertFalse(
            inat_finder.check_observation_family(observation, "Amanitaceae", 60773)
        )

    def test_check_observation_family_taxon_id_is_optional(self):
        observation = {
            "taxon": {
                "id": 60773,
                "name": "Amanitaceae",
                "rank": "family",
            }
        }
        self.assertTrue(
            inat_finder.check_observation_family(observation, "Amanitaceae")
        )
        self.assertFalse(
            inat_finder.check_observation_family(observation, "Russulaceae")
        )

    def test_retry_delay_supports_http_date_retry_after(self):
        response = Mock(
            headers={
                "Retry-After": "Wed, 21 Oct 2026 07:28:30 GMT",
                "Date": "Wed, 21 Oct 2026 07:28:00 GMT",
            }
        )
        self.assertEqual(inat_finder._retry_delay(response, 0), 30.0)

        # A Retry-After date in the past never yields a negative delay.
        past = Mock(
            headers={
                "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT",
                "Date": "Wed, 21 Oct 2026 07:28:30 GMT",
            }
        )
        self.assertEqual(inat_finder._retry_delay(past, 0), 0.0)

        # No usable header at all falls back to exponential backoff.
        self.assertEqual(inat_finder._retry_delay(Mock(headers={}), 2), 4.0)

    def test_verify_user_404_is_clean_not_found(self):
        response = Mock(status_code=404)
        with (
            patch.object(inat_finder, "api_get", return_value=response),
            patch("builtins.print") as output,
        ):
            self.assertFalse(inat_finder.verify_user_exists("missing"))
        output.assert_not_called()

    def test_api_get_retries_retryable_status_and_honors_retry_after(self):
        retry_response = Mock(status_code=429, headers={"Retry-After": "3"})
        success_response = Mock(status_code=200, headers={})
        with (
            patch.object(
                inat_finder.SESSION,
                "get",
                side_effect=[retry_response, success_response],
            ) as session_get,
            patch.object(inat_finder.time, "sleep") as sleep,
        ):
            result = inat_finder.api_get("/observations")
        self.assertIs(result, success_response)
        self.assertEqual(session_get.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_find_taxon_falls_back_after_autocomplete_miss(self):
        autocomplete = Mock(status_code=200)
        autocomplete.raise_for_status.return_value = None
        autocomplete.json.return_value = {"results": []}
        fallback = Mock(status_code=200)
        fallback.raise_for_status.return_value = None
        fallback.json.return_value = {
            "results": [{"id": 47158, "name": "Amanita", "rank": "genus"}]
        }
        with patch.object(
            inat_finder, "api_get", side_effect=[autocomplete, fallback]
        ) as api_get:
            result = inat_finder.find_taxon("amanita", "genus")
        self.assertEqual(result["id"], 47158)
        self.assertEqual(api_get.call_args_list[0].kwargs["params"]["per_page"], 30)

    def test_project_slug_uses_direct_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [{"id": 7, "slug": "fungi-map", "title": "Fungi Map"}]
        }
        with patch.object(inat_finder, "api_get", return_value=response) as api_get:
            identifier, project = inat_finder.resolve_project_identifier("fungi-map")
        self.assertEqual(identifier, "7")
        self.assertEqual(project["slug"], "fungi-map")
        api_get.assert_called_once_with("/projects/fungi-map")

    def test_main_family_search(self):
        argv = [
            "inat_finder.py",
            "--family",
            "Amanitaceae",
            "123456789",
            "--no-progress",
        ]
        matching_observation = {
            "id": 123456788,
            "taxon": {
                "id": 1234,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestor_ids": [60773],
            },
            "user": {"login": "observer"},
            "place_ids": [1, 24, 999],
        }

        def fake_batch_check(variations, batch_size=None, **kwargs):
            """Deliver two single-observation batches through results_callback."""
            results = [matching_observation, matching_observation.copy()]
            results_callback = kwargs.get("results_callback")
            if results_callback:
                results_callback(results[:1])
                results_callback(results[1:])
            return inat_finder.BatchCheckResult(results, 0, 0)

        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(inat_finder, "fetch_observations", return_value=[]),
            patch.object(
                inat_finder,
                "find_taxon",
                return_value={"id": 60773, "name": "Amanitaceae", "rank": "family"},
            ) as find_taxon,
            patch.object(
                inat_finder,
                "batch_check_observations",
                side_effect=fake_batch_check,
            ),
            patch.object(
                inat_finder,
                "resolve_observation_locations",
                return_value={123456788: "Pike Co. MS US"},
            ),
            patch("builtins.print") as output,
        ):
            inat_finder.main()

        find_taxon.assert_called_once_with("Amanitaceae", "family")
        rendered_output = "\n".join(
            " ".join(str(arg) for arg in call.args) for call in output.call_args_list
        )
        self.assertIn("Found 1 potential matches", rendered_output)
        self.assertIn("Observation #123456788", rendered_output)
        self.assertIn("Location: Pike Co. MS US", rendered_output)

    def test_batch_check_owns_batching_and_leaves_pacing_to_api_get(self):
        first = Mock(status_code=200)
        first.json.return_value = {"results": [{"id": 1}, {"id": 2}]}
        second = Mock(status_code=200)
        second.json.return_value = {"results": [{"id": 3}]}
        results_callback = Mock()
        progress = Mock()
        with (
            patch.object(
                inat_finder, "api_get", side_effect=[first, second]
            ) as api_get,
            patch.object(inat_finder.time, "sleep") as sleep,
        ):
            result = inat_finder.batch_check_observations(
                ["1", "2", "3"],
                batch_size=2,
                results_callback=results_callback,
                progress_callback=progress,
            )
        # Each batch's results are handed to the callback as soon as they arrive.
        self.assertEqual(
            [call.args[0] for call in results_callback.call_args_list],
            [[{"id": 1}, {"id": 2}], [{"id": 3}]],
        )
        self.assertEqual(result.observations, [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual(result.unchecked, 0)
        self.assertEqual(result.failed_batches, 0)
        self.assertEqual(
            [call.args for call in progress.call_args_list], [(2, True), (1, True)]
        )
        self.assertEqual(api_get.call_count, 2)
        self.assertEqual(
            api_get.call_args_list[0].kwargs["params"]["fields"],
            inat_finder.OBSERVATION_FIELDS,
        )
        # Pacing now lives in api_get, so batching itself must not sleep again.
        sleep.assert_not_called()

    def test_yes_flag_proceeds_through_large_search_without_reading_stdin(self):
        argv = [
            "inat_finder.py",
            "--genus",
            "Amanita",
            "123456789",
            "--digits",
            "3",
            "--no-progress",
            "--yes",
        ]
        checked = []

        def fake_batch_check(variations, batch_size=None, **kwargs):
            checked.append(list(variations))
            return inat_finder.BatchCheckResult([], 0, 0)

        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(inat_finder, "fetch_observations", return_value=[]),
            patch.object(
                inat_finder,
                "find_taxon",
                return_value={"id": 47158, "name": "Amanita", "rank": "genus"},
            ),
            patch.object(
                inat_finder, "batch_check_observations", side_effect=fake_batch_check
            ),
            patch.object(inat_finder, "resolve_observation_locations", return_value={}),
            patch(
                "builtins.input",
                side_effect=AssertionError("--yes must never read stdin"),
            ),
            patch("builtins.print") as output,
        ):
            inat_finder.main()

        rendered_output = "\n".join(
            " ".join(str(arg) for arg in call.args) for call in output.call_args_list
        )
        # The search ran past the large-search confirmation instead of exiting.
        self.assertNotIn("Exiting search.", rendered_output)
        self.assertIn("Search complete!", rendered_output)
        self.assertEqual(len(checked), 1)
        self.assertGreater(len(checked[0]), inat_finder.LARGE_SEARCH_THRESHOLD)

    def test_large_search_without_yes_exits_when_stdin_is_closed(self):
        argv = [
            "inat_finder.py",
            "--genus",
            "Amanita",
            "123456789",
            "--digits",
            "3",
            "--no-progress",
        ]
        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(
                inat_finder,
                "find_taxon",
                return_value={"id": 47158, "name": "Amanita", "rank": "genus"},
            ),
            patch.object(
                inat_finder, "fetch_observations", return_value=[]
            ) as fetch_observations,
            patch.object(
                inat_finder,
                "batch_check_observations",
                return_value=inat_finder.BatchCheckResult([], 0, 0),
            ) as batch_check,
            patch("builtins.input", side_effect=EOFError),
            patch("builtins.print") as output,
        ):
            inat_finder.main()

        rendered_output = "\n".join(
            " ".join(str(arg) for arg in call.args) for call in output.call_args_list
        )
        self.assertIn("Exiting search.", rendered_output)
        # Only the original-number check ran; the large search was declined.
        fetch_observations.assert_called_once()
        batch_check.assert_not_called()

    def test_keyboard_interrupt_during_search_reports_partial_matches(self):
        argv = ["inat_finder.py", "--user", "observer", "123456789"]
        matching_observation = {
            "id": 123456788,
            "taxon": {"id": 1234, "name": "Amanita muscaria", "rank": "species"},
            "user": {"login": "observer"},
        }

        def fake_batch_check(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([matching_observation])
            raise KeyboardInterrupt

        pbar = Mock()
        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(inat_finder, "fetch_observations", return_value=[]),
            patch.object(inat_finder, "verify_user_exists", return_value=True),
            patch.object(inat_finder, "tqdm", return_value=pbar),
            patch.object(
                inat_finder, "batch_check_observations", side_effect=fake_batch_check
            ),
            patch.object(
                inat_finder,
                "resolve_observation_locations",
                return_value={123456788: "Pike Co. MS US"},
            ),
            patch("builtins.print") as output,
            self.assertRaises(SystemExit) as exit_context,
        ):
            inat_finder.main()

        self.assertEqual(exit_context.exception.code, 130)
        pbar.close.assert_called_once_with()
        rendered_output = "\n".join(
            " ".join(str(arg) for arg in call.args) for call in output.call_args_list
        )
        self.assertIn("Search interrupted!", rendered_output)
        self.assertIn("partial results", rendered_output)
        self.assertIn("Observation #123456788", rendered_output)

    def test_progress_bar_is_closed_when_the_search_raises(self):
        argv = ["inat_finder.py", "--user", "observer", "123456789"]

        def fake_batch_check(variations, batch_size=None, **kwargs):
            raise RuntimeError("boom")

        pbar = Mock()
        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(inat_finder, "fetch_observations", return_value=[]),
            patch.object(inat_finder, "verify_user_exists", return_value=True),
            patch.object(inat_finder, "tqdm", return_value=pbar),
            patch.object(
                inat_finder, "batch_check_observations", side_effect=fake_batch_check
            ),
            patch("builtins.print"),
            self.assertRaises(RuntimeError),
        ):
            inat_finder.main()

        pbar.close.assert_called_once_with()

    def test_digits_zero_does_not_recheck_the_original_number(self):
        argv = [
            "inat_finder.py",
            "--genus",
            "Amanita",
            "123456789",
            "--digits",
            "0",
            "--no-progress",
            "--yes",
        ]
        matching_observation = {
            "id": 123456789,
            "taxon": {
                "id": 47158,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestor_ids": [47158],
            },
            "user": {"login": "observer"},
        }
        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(
                inat_finder,
                "find_taxon",
                return_value={"id": 47158, "name": "Amanita", "rank": "genus"},
            ),
            patch.object(
                inat_finder,
                "fetch_observations",
                return_value=[matching_observation],
            ) as fetch_observations,
            patch.object(inat_finder, "batch_check_observations") as batch_check,
            patch.object(
                inat_finder,
                "resolve_observation_locations",
                return_value={123456789: "Pike Co. MS US"},
            ),
            patch("builtins.input", side_effect=AssertionError("--yes reads no stdin")),
            patch("builtins.print") as output,
        ):
            inat_finder.main()

        # Only the original-number lookup happened - no redundant second request.
        fetch_observations.assert_called_once_with(["123456789"], project_id=None)
        batch_check.assert_not_called()
        rendered_output = "\n".join(
            " ".join(str(arg) for arg in call.args) for call in output.call_args_list
        )
        self.assertIn("already been checked", rendered_output)
        self.assertIn("Found 1 potential matches", rendered_output)
        self.assertIn("Observation #123456789", rendered_output)

    def test_format_place_label_prefers_most_specific_admin_place(self):
        places = [
            {
                "id": 1,
                "name": "United States",
                "display_name": "United States",
                "admin_level": 0,
            },
            {
                "id": 24,
                "name": "Mississippi",
                "display_name": "Mississippi, US",
                "admin_level": 10,
            },
            {
                "id": 999,
                "name": "Pike",
                "display_name": "Pike County, MS, US",
                "admin_level": 20,
            },
            {
                "id": 1000,
                "name": "Custom region",
                "display_name": "Custom region",
                "admin_level": None,
            },
        ]
        self.assertEqual(inat_finder.format_place_label(places), "Pike Co. MS US")
        self.assertEqual(inat_finder.format_place_label([]), "Unknown location")

    def test_resolve_observation_locations_uses_place_ids(self):
        observations = [
            {"id": 10, "place_ids": [1, 24, 999]},
            {"id": 11, "place_ids": []},
        ]
        places = {
            "1": {"id": 1, "display_name": "United States", "admin_level": 0},
            "24": {"id": 24, "display_name": "Mississippi, US", "admin_level": 10},
            "999": {
                "id": 999,
                "display_name": "Pike County, MS, US",
                "admin_level": 20,
            },
        }
        with patch.object(inat_finder, "fetch_places", return_value=places) as fetch:
            labels = inat_finder.resolve_observation_locations(observations)
        fetch.assert_called_once_with([1, 24, 999], message_callback=print)
        self.assertEqual(labels[10], "Pike Co. MS US")
        self.assertEqual(labels[11], "Unknown location")

    def test_fetch_places_requests_required_fields(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "id": 999,
                    "name": "Pike",
                    "admin_level": 20,
                    "display_name": "Pike County, MS, US",
                }
            ]
        }
        with patch.object(inat_finder, "api_get", return_value=response) as api_get:
            places = inat_finder.fetch_places([999, 999])
        self.assertIn("999", places)
        api_get.assert_called_once_with(
            "/places/999",
            params={
                "fields": inat_finder.PLACE_FIELDS,
                "per_page": inat_finder.BATCH_SIZE,
            },
        )


def _observation(obs_id, login="observer", taxon_id=47158, ancestor_ids=(47158,)):
    """Build a minimal observation payload shaped like the iNaturalist API's."""
    return {
        "id": obs_id,
        "taxon": {
            "id": taxon_id,
            "name": "Amanita muscaria",
            "rank": "species",
            "ancestor_ids": list(ancestor_ids),
        },
        "user": {"login": login},
        "place_ids": [],
    }


class MainRunnerMixin:
    """Helpers for driving main() with the network fully mocked out."""

    def setUp(self):
        inat_finder.RATE_LIMITER.reset()

    def run_main(self, argv, patches=None, expect_locations=True):
        """Run main(); return (exit_status, printed_output)."""
        printed = []

        def record(*args, **kwargs):
            printed.append(" ".join(str(arg) for arg in args))

        patches = dict(patches or {})
        if expect_locations and "resolve_observation_locations" not in patches:
            patches["resolve_observation_locations"] = Mock(return_value={})

        status = 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(inat_finder.sys, "argv", list(argv)))
            stack.enter_context(patch("builtins.print", side_effect=record))
            for name, replacement in patches.items():
                stack.enter_context(patch.object(inat_finder, name, replacement))
            try:
                inat_finder.main()
            except SystemExit as exit_error:
                status = exit_error.code or 0
        return status, "\n".join(printed)


class TestFailedBatchesAreNotFalseNegatives(MainRunnerMixin, unittest.TestCase):
    """Issue 1: a failed API batch must never look like a clean 'no matches'."""

    def _api_get_json(self, per_batch):
        """Return a fake api_get_json that maps each batch's ids to a result."""
        calls = []

        def fake(path, allow_missing=False, **kwargs):
            ids = kwargs["params"]["id"].split(",")
            calls.append(ids)
            outcome = per_batch(ids, len(calls))
            if isinstance(outcome, ApiError):
                raise outcome
            return {"results": outcome}

        return fake, calls

    def test_all_batches_succeed(self):
        fake, calls = self._api_get_json(lambda ids, n: [{"id": int(ids[0])}])
        progress = Mock()
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                ["1", "2", "3", "4"], batch_size=2, progress_callback=progress
            )
        self.assertEqual(result.unchecked, 0)
        self.assertEqual(result.failed_batches, 0)
        self.assertEqual(len(result.observations), 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.args[1] for call in progress.call_args_list))

    def test_intermediate_batch_failing_permanently_is_reported_unchecked(self):
        def per_batch(ids, call_number):
            if ids == ["3", "4"]:
                return ApiError("boom")
            return [{"id": int(ids[0])}]

        fake, calls = self._api_get_json(per_batch)
        progress = Mock()
        messages = []
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                ["1", "2", "3", "4", "5", "6"],
                batch_size=2,
                progress_callback=progress,
                message_callback=messages.append,
            )
        # Three batches, plus one retry round for the failing batch.
        self.assertEqual(len(calls), 4)
        self.assertEqual(result.unchecked, 2)
        self.assertEqual(result.failed_batches, 1)
        self.assertEqual(len(result.observations), 2)
        # The failed candidates are reported as not checked, never as progress.
        self.assertIn((2, False), [call.args for call in progress.call_args_list])
        self.assertEqual(
            sum(
                count
                for count, checked in (c.args for c in progress.call_args_list)
                if checked
            ),
            4,
        )
        self.assertTrue(any("Retrying" in message for message in messages))

    def test_failed_batch_recovers_on_retry(self):
        state = {"failed": False}

        def per_batch(ids, call_number):
            if ids == ["3"] and not state["failed"]:
                state["failed"] = True
                return ApiError("transient")
            return [{"id": int(ids[0])}]

        fake, calls = self._api_get_json(per_batch)
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(["1", "2", "3"], batch_size=1)
        self.assertEqual(result.unchecked, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(result.observations), 3)

    def test_results_are_not_accumulated_when_the_caller_streams_them(self):
        streamed = []
        fake, _calls = self._api_get_json(lambda ids, n: [{"id": int(ids[0])}])
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                ["1", "2"],
                batch_size=1,
                results_callback=streamed.extend,
                collect_results=False,
            )
        self.assertEqual(len(streamed), 2)
        self.assertEqual(result.observations, [])
        self.assertEqual(result.unchecked, 0)

    def test_total_outage_does_not_retain_every_failed_batch(self):
        fake, calls = self._api_get_json(lambda ids, n: ApiError("offline"))
        candidate_count = (inat_finder.MAX_RETRY_BATCHES + 20) * 2
        candidates = (str(value) for value in range(1, candidate_count + 1))
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                candidates, batch_size=2, message_callback=lambda message: None
            )
        # Every candidate is still counted as unchecked...
        self.assertEqual(result.unchecked, candidate_count)
        # ...but only a bounded number of batches were kept and retried.
        self.assertEqual(
            len(calls),
            candidate_count // 2 + inat_finder.MAX_RETRY_BATCHES,
        )

    def test_batch_with_the_only_match_failing_reports_incomplete_search(self):
        match = _observation(123456788)

        def fake_batch(variations, batch_size=None, **kwargs):
            # The batch that would have contained the match never completes.
            kwargs["results_callback"]([])
            kwargs["progress_callback"](10, True)
            kwargs["progress_callback"](5, False)
            return inat_finder.BatchCheckResult([], 5, 1)

        status, output = self.run_main(
            ["inat_finder.py", "--user", "observer", "123456789", "--no-progress"],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(return_value=[]),
                "batch_check_observations": Mock(side_effect=fake_batch),
            },
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertNotIn("Search complete!", output)
        self.assertIn("Search incomplete", output)
        self.assertIn("5 candidate(s) could not be checked", output)
        self.assertIn("may still exist", output)
        self.assertNotIn(str(match["id"]), output)

    def test_partial_matches_are_shown_with_a_failed_batch(self):
        match = _observation(123456788)

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([match])
            return inat_finder.BatchCheckResult([match], 200, 1)

        status, output = self.run_main(
            ["inat_finder.py", "--user", "observer", "123456789", "--no-progress"],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(return_value=[]),
                "batch_check_observations": Mock(side_effect=fake_batch),
                "resolve_observation_locations": Mock(
                    return_value={123456788: "Pike Co. MS US"}
                ),
            },
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertIn("Search incomplete", output)
        self.assertIn("Found 1 potential matches (partial results)", output)
        self.assertIn("Observation #123456788", output)
        self.assertNotIn("Search complete!", output)

    def test_interrupt_after_completed_batches_still_exits_130(self):
        match = _observation(123456788)

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([match])
            kwargs["progress_callback"](200, True)
            raise KeyboardInterrupt

        status, output = self.run_main(
            ["inat_finder.py", "--user", "observer", "123456789", "--no-progress"],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(return_value=[]),
                "batch_check_observations": Mock(side_effect=fake_batch),
                "resolve_observation_locations": Mock(
                    return_value={123456788: "Pike Co. MS US"}
                ),
            },
        )
        self.assertEqual(status, 130)
        self.assertIn("Search interrupted!", output)
        self.assertIn("partial results", output)
        self.assertIn("Observation #123456788", output)

    def test_failed_original_check_makes_the_search_incomplete(self):
        status, output = self.run_main(
            ["inat_finder.py", "--user", "observer", "123456789", "--no-progress"],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=ApiError("offline")),
                "batch_check_observations": Mock(
                    return_value=inat_finder.BatchCheckResult([], 0, 0)
                ),
            },
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertIn("could not check the original observation number", output)
        self.assertIn("Search incomplete", output)


class TestCandidatePlanning(unittest.TestCase):
    """Issue 2 and 7: sizing, streaming, deduplication, and typo coverage."""

    def setUp(self):
        inat_finder.RATE_LIMITER.reset()

    def test_count_matches_generation_for_small_inputs(self):
        for number in ("7", "12", "123", "1023", "907"):
            for digits in range(5):
                with self.subTest(number=number, digits=digits):
                    generated = list(inat_finder.iter_digit_variations(number, digits))
                    expected = len(generated) if digits > 0 else 0
                    self.assertEqual(count_digit_variations(number, digits), expected)
                    if digits > 0:
                        # Generation itself is duplicate-free and leading-zero-free.
                        self.assertEqual(len(set(generated)), len(generated))
                        self.assertTrue(
                            all(not v.startswith("0") or len(v) == 1 for v in generated)
                        )

    def test_count_matches_closed_form_for_nine_digits(self):
        expected = sum(
            math.comb(8, k) * 9**k + math.comb(8, k - 1) * 8 * 9 ** (k - 1)
            for k in range(1, 5)
        )
        self.assertEqual(count_digit_variations("123456789", 4), expected)
        self.assertEqual(count_digit_variations("123456789", 0), 0)

    def test_plan_for_digits_zero_has_no_candidates(self):
        plan = CandidatePlan("123456789", 0)
        self.assertEqual(plan.total, 0)
        self.assertEqual(list(plan), [])

    def test_plan_for_one_digit_search_is_exact_and_unique(self):
        plan = CandidatePlan("1234567", 1)
        candidates = list(plan)
        self.assertEqual(len(candidates), plan.total)
        # No duplicate API checks and no leading-zero IDs.
        self.assertEqual(len({int(value) for value in candidates}), plan.total)
        self.assertTrue(all(inat_finder._is_valid_candidate(v) for v in candidates))
        # The original number is never re-checked as a candidate.
        self.assertNotIn("1234567", candidates)
        self.assertGreater(plan.replacement_count, 0)
        self.assertTrue(plan.additions and plan.removals and plan.transpositions)

    def test_transpositions_are_not_duplicated_by_two_digit_replacements(self):
        one_digit = CandidatePlan("123456789", 1)
        self.assertIn("123465789", list(one_digit))
        two_digit = CandidatePlan("123456789", 2)
        # With two-digit replacements every adjacent swap is already covered.
        self.assertEqual(two_digit.transpositions, [])
        swaps = [c for c in itertools.islice(two_digit, 200000) if c == "123465789"]
        self.assertEqual(len(swaps), 1)

    def test_plan_candidates_are_deduplicated_across_mutation_methods(self):
        # "11" -> removing a digit and replacing one can both produce "1".
        plan = CandidatePlan("112", 1)
        candidates = list(plan)
        self.assertEqual(len(candidates), plan.total)
        self.assertEqual(len({int(value) for value in candidates}), plan.total)
        self.assertNotIn("112", candidates)

    def test_large_plan_is_not_materialized(self):
        plan = CandidatePlan("123456789", 4, add_digits=False, remove_digits=False)
        self.assertGreater(plan.total, 800000)
        # Nothing large was built: only the bounded extra classes are stored.
        self.assertEqual(plan.extras, [])
        self.assertIsInstance(iter(plan), types.GeneratorType)
        self.assertEqual(len(list(itertools.islice(plan, 5))), 5)

    def test_extra_candidate_classes_stay_small(self):
        plan = CandidatePlan("12345678", 1)
        self.assertLess(len(plan.extras), 20000)

    def test_two_missing_digits_can_be_internal(self):
        variations = generate_digit_additions("1234", max_added_digits=2)
        # '9' omitted after "1" and '8' omitted after "3": 1 9 2 3 8 4
        self.assertIn("192384", variations)
        # Both at the ends still work, as before.
        self.assertIn("911234", variations)
        self.assertIn("123499", variations)
        self.assertEqual(len(variations), len(set(variations)))
        self.assertTrue(all(not v.startswith("0") for v in variations))

    def test_adjacent_transpositions(self):
        self.assertIn("123465789", generate_digit_transpositions("123456789"))
        self.assertEqual(generate_digit_transpositions("111"), [])
        # A swap must never create a leading zero.
        self.assertNotIn("012", generate_digit_transpositions("102"))
        self.assertEqual(generate_digit_transpositions("102"), ["120"])


class TestSearchSizeSafety(MainRunnerMixin, unittest.TestCase):
    """Issue 2: expensive searches are sized, confirmed, or refused up front."""

    def _taxon_patches(self, **extra):
        patches = {
            "find_taxon": Mock(
                return_value={"id": 47158, "name": "Amanita", "rank": "genus"}
            ),
            "fetch_observations": Mock(return_value=[]),
        }
        patches.update(extra)
        return patches

    def test_large_search_asks_before_generating_candidates(self):
        iter_variations = Mock(
            side_effect=AssertionError("candidates generated before confirmation")
        )
        batch_check = Mock()
        with patch("builtins.input", side_effect=EOFError) as prompt:
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--genus",
                    "Amanita",
                    "123456789",
                    "--digits",
                    "3",
                    "--no-progress",
                ],
                self._taxon_patches(
                    iter_digit_variations=iter_variations,
                    batch_check_observations=batch_check,
                ),
            )
        self.assertEqual(status, 0)
        self.assertIn("61937 total unique variations", output)
        # The confirmation prompt is asked before any candidate is generated.
        self.assertIn("This is a large search", prompt.call_args.args[0])
        self.assertIn("Exiting search.", output)
        iter_variations.assert_not_called()
        batch_check.assert_not_called()

    def test_impossible_search_is_refused_without_prompting(self):
        iter_variations = Mock(
            side_effect=AssertionError("candidates generated for a refused search")
        )
        batch_check = Mock()
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--genus",
                    "Amanita",
                    "123456789",
                    "--digits",
                    "9",
                    "--no-progress",
                    "--yes",
                ],
                self._taxon_patches(
                    iter_digit_variations=iter_variations,
                    batch_check_observations=batch_check,
                ),
            )
        self.assertEqual(status, 1)
        self.assertIn("which is far more than the limit", output)
        self.assertIn("smaller --digits", output)
        iter_variations.assert_not_called()
        batch_check.assert_not_called()

    def test_normal_one_digit_search_runs_without_confirmation(self):
        seen = []

        def fake_batch(variations, batch_size=None, **kwargs):
            seen.extend(variations)
            return inat_finder.BatchCheckResult([], 0, 0)

        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--genus",
                    "Amanita",
                    "123456789",
                    "--no-progress",
                ],
                self._taxon_patches(
                    batch_check_observations=Mock(side_effect=fake_batch)
                ),
            )
        self.assertEqual(status, 0)
        self.assertIn("Search complete!", output)
        self.assertNotIn("large search", output)
        expected = CandidatePlan("123456789", 1, add_digits=False).total
        self.assertEqual(len(seen), expected)
        self.assertEqual(len(set(seen)), expected)

    def test_digits_zero_reports_the_original_match_only(self):
        match = _observation(123456789)
        batch_check = Mock()
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--genus",
                "Amanita",
                "123456789",
                "--digits",
                "0",
                "--no-progress",
                "--yes",
            ],
            self._taxon_patches(
                fetch_observations=Mock(return_value=[match]),
                batch_check_observations=batch_check,
            ),
        )
        self.assertEqual(status, 0)
        self.assertIn("already been checked", output)
        self.assertIn("Found 1 potential matches", output)
        batch_check.assert_not_called()

    def test_progress_total_matches_the_planned_candidate_count(self):
        plan_total = CandidatePlan("123456789", 1, add_digits=False).total
        pbar = Mock()
        tqdm_factory = Mock(return_value=pbar)

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["progress_callback"](7, True)
            kwargs["progress_callback"](3, False)
            return inat_finder.BatchCheckResult([], 3, 1)

        status, _output = self.run_main(
            ["inat_finder.py", "--genus", "Amanita", "123456789"],
            self._taxon_patches(
                tqdm=tqdm_factory,
                batch_check_observations=Mock(side_effect=fake_batch),
            ),
        )
        self.assertEqual(tqdm_factory.call_args.kwargs["total"], plan_total)
        # Only checked candidates advance the bar; failures are shown separately.
        pbar.update.assert_called_once_with(7)
        pbar.set_postfix_str.assert_called_once_with("3 unchecked")
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)


class TestOriginalMatchIsPreserved(MainRunnerMixin, unittest.TestCase):
    """Issue 3: the original observation belongs in the results exactly once."""

    def _patches(self, batch=None, original=None):
        return {
            "verify_user_exists": Mock(return_value=True),
            "fetch_observations": Mock(
                return_value=[original or _observation(123456789)]
            ),
            "batch_check_observations": Mock(
                side_effect=batch
                or (lambda *a, **k: inat_finder.BatchCheckResult([], 0, 0))
            ),
            "resolve_observation_locations": Mock(
                return_value={123456789: "Pike Co. MS US", 123456788: "Pike Co. MS US"}
            ),
        }

    def test_original_matches_and_user_stops(self):
        with patch("builtins.input", return_value="n"):
            status, output = self.run_main(
                ["inat_finder.py", "--user", "observer", "123456789", "--no-progress"],
                self._patches(),
            )
        self.assertEqual(status, 0)
        self.assertIn("Exiting search.", output)
        self.assertIn("Found 1 potential matches", output)
        self.assertIn("Observation #123456789", output)
        self.assertNotIn("No matches found", output)

    def test_original_matches_user_continues_and_nothing_else_matches(self):
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            self._patches(),
        )
        self.assertEqual(status, 0)
        self.assertIn("Search complete!", output)
        self.assertIn("Found 1 potential matches", output)
        self.assertIn("Observation #123456789", output)
        self.assertNotIn("No matches found", output)

    def test_original_plus_alternate_matches(self):
        alternate = _observation(123456788)

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([alternate])
            return inat_finder.BatchCheckResult([alternate], 0, 0)

        status, output = self.run_main(
            [
                "inat_finder.py",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            self._patches(batch=fake_batch),
        )
        self.assertEqual(status, 0)
        self.assertIn("Found 2 potential matches", output)
        self.assertIn("Observation #123456789", output)
        self.assertIn("Observation #123456788", output)

    def test_original_reported_once_when_a_candidate_returns_it_again(self):
        original = _observation(123456789)

        def fake_batch(variations, batch_size=None, **kwargs):
            # A padded candidate could resolve to the same observation ID.
            kwargs["results_callback"]([dict(original)])
            return inat_finder.BatchCheckResult([dict(original)], 0, 0)

        status, output = self.run_main(
            [
                "inat_finder.py",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            self._patches(batch=fake_batch, original=original),
        )
        self.assertEqual(status, 0)
        self.assertIn("Found 1 potential matches", output)
        self.assertEqual(output.count("Observation #123456789"), 1)


class TestLookupFailuresAreNotNotFound(MainRunnerMixin, unittest.TestCase):
    """Issue 4: outages must never be reported as 'not found'."""

    def test_user_not_found_is_friendly(self):
        response = Mock(status_code=404)
        with patch.object(inat_finder, "api_get", return_value=response):
            status, output = self.run_main(
                ["inat_finder.py", "--user", "nobody", "123456789", "--no-progress"]
            )
        self.assertEqual(status, 1)
        self.assertIn("Username 'nobody' not found on iNaturalist.", output)

    def test_user_lookup_api_failure_is_operational(self):
        with patch.object(
            inat_finder, "api_get", side_effect=ApiError("network error")
        ):
            status, output = self.run_main(
                ["inat_finder.py", "--user", "nobody", "123456789", "--no-progress"]
            )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertNotIn("not found", output)
        self.assertIn("could not reach the iNaturalist API", output)
        # The message appears once, not at every layer.
        self.assertEqual(output.count("could not reach the iNaturalist API"), 1)

    def test_verify_user_raises_on_server_error(self):
        with (
            patch.object(inat_finder, "api_get", return_value=Mock(status_code=500)),
            self.assertRaises(ApiError),
        ):
            inat_finder.verify_user_exists("someone")

    def test_taxon_not_found_is_friendly(self):
        empty = Mock(status_code=200)
        empty.json.return_value = {"results": []}
        with patch.object(inat_finder, "api_get", return_value=empty):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--genus",
                    "Nosuchgenus",
                    "123456789",
                    "--no-progress",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("not found in iNaturalist taxonomy", output)

    def test_taxon_lookup_api_failure_is_operational(self):
        with patch.object(inat_finder, "api_get", side_effect=ApiError("503")):
            status, output = self.run_main(
                ["inat_finder.py", "--genus", "Amanita", "123456789", "--no-progress"]
            )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertNotIn("not found in iNaturalist taxonomy", output)
        self.assertIn("could not reach the iNaturalist API", output)

    def test_find_taxon_propagates_api_errors(self):
        with (
            patch.object(inat_finder, "api_get", side_effect=ApiError("boom")),
            self.assertRaises(ApiError),
        ):
            inat_finder.find_taxon("Amanita", "genus")

    def test_project_not_found_is_friendly(self):
        missing = Mock(status_code=404)
        with patch.object(inat_finder, "api_get", return_value=missing):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--project",
                    "12345678",
                    "123456789",
                    "--no-progress",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("not found on iNaturalist", output)

    def test_project_lookup_api_failure_is_operational(self):
        with patch.object(inat_finder, "api_get", side_effect=ApiError("timeout")):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--project",
                    "fungi-map",
                    "123456789",
                    "--no-progress",
                ]
            )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertNotIn("not found", output)
        self.assertIn("could not reach the iNaturalist API", output)

    def test_project_search_propagates_api_errors(self):
        with (
            patch.object(inat_finder, "api_get", side_effect=ApiError("boom")),
            self.assertRaises(ApiError),
        ):
            inat_finder.search_projects_by_query("fungi")

    def test_api_get_raises_after_exhausting_retries(self):
        failing = Mock(status_code=503, headers={})
        with (
            patch.object(inat_finder.SESSION, "get", return_value=failing),
            patch.object(inat_finder.time, "sleep"),
            self.assertRaises(ApiError),
        ):
            inat_finder.api_get("/observations")

    def test_api_get_raises_on_network_error(self):
        with (
            patch.object(
                inat_finder.SESSION,
                "get",
                side_effect=inat_finder.requests.ConnectionError("down"),
            ),
            patch.object(inat_finder.time, "sleep"),
            self.assertRaises(ApiError),
        ):
            inat_finder.api_get("/observations")


class TestRequestPacing(unittest.TestCase):
    """Issue 6: one shared pacing layer for every iNaturalist request."""

    def setUp(self):
        inat_finder.RATE_LIMITER.reset()

    def test_consecutive_requests_are_paced(self):
        ok = Mock(status_code=200, headers={})
        with (
            patch.object(inat_finder.SESSION, "get", return_value=ok),
            patch.object(inat_finder.time, "sleep") as sleep,
        ):
            inat_finder.api_get("/taxa")
            self.assertEqual(sleep.call_count, 0)
            inat_finder.api_get("/observations")
        # The second request waits for the shared one-per-second baseline.
        self.assertEqual(sleep.call_count, 1)
        self.assertGreater(sleep.call_args.args[0], 0)
        self.assertLessEqual(sleep.call_args.args[0], inat_finder.RATE_LIMIT_DELAY)

    def test_backoff_and_pacing_do_not_sleep_twice(self):
        retry = Mock(status_code=429, headers={"Retry-After": "5"})
        ok = Mock(status_code=200, headers={})
        with (
            patch.object(inat_finder.SESSION, "get", side_effect=[retry, ok]),
            patch.object(inat_finder.time, "sleep") as sleep,
        ):
            inat_finder.api_get("/observations")
        sleep.assert_called_once_with(5.0)

    def test_pacing_can_be_disabled_for_tests(self):
        limiter = inat_finder.RateLimiter(min_interval=0)
        limiter.record_request()
        with patch.object(inat_finder.time, "sleep") as sleep:
            self.assertEqual(limiter.wait(), 0.0)
        sleep.assert_not_called()


class TestLocationFallback(unittest.TestCase):
    """Lower-priority cleanup: fall back to place_guess when needed."""

    def test_place_guess_is_used_when_places_cannot_be_resolved(self):
        observations = [
            {"id": 1, "place_ids": [999], "place_guess": "Whitinsville, MA, US"},
            {"id": 2, "place_ids": [], "place_guess": "  "},
        ]
        with patch.object(inat_finder, "fetch_places", return_value={}):
            labels = inat_finder.resolve_observation_locations(observations)
        self.assertEqual(labels[1], "Whitinsville, MA, US")
        self.assertEqual(labels[2], "Unknown location")

    def test_structured_places_still_win(self):
        observations = [{"id": 1, "place_ids": [24], "place_guess": "somewhere"}]
        places = {
            "24": {"id": 24, "display_name": "Mississippi, US", "admin_level": 10}
        }
        with patch.object(inat_finder, "fetch_places", return_value=places):
            labels = inat_finder.resolve_observation_locations(observations)
        self.assertEqual(labels[1], "Mississippi US")


if __name__ == "__main__":
    unittest.main()
