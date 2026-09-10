import contextlib
import io
import itertools
import json
import math
import os
import subprocess
import sys
import threading
import types
import typing
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import inat_finder
from inat_finder import (
    ApiError,
    AutoStage,
    CandidatePlan,
    build_candidate_plan,
    build_resume_token,
    count_digit_variations,
    generate_digit_additions,
    generate_digit_removals,
    generate_digit_transpositions,
    generate_digit_variations,
    parse_inat_url,
    parse_resume_token,
    preprocess_argv_for_project_name,
    rank_matches,
    restore_seen_ids,
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

    def test_eager_digit_variations_refuses_pathological_materialization(self):
        with (
            patch.object(inat_finder, "iter_digit_variations") as iterator,
            self.assertRaisesRegex(
                ValueError, r"899999999 candidates.*iter_digit_variations"
            ),
        ):
            generate_digit_variations("123456789", 9)
        iterator.assert_not_called()

    def test_large_digit_variations_remain_streamable(self):
        streamed = list(
            itertools.islice(inat_finder.iter_digit_variations("123456789", 9), 3)
        )
        self.assertEqual(streamed, ["223456789", "323456789", "423456789"])

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
        direct = {"taxon": {"id": 48419, "name": "Amanita", "rank": "genus"}}
        ancestor = {
            "taxon": {"id": 1, "ancestor_ids": [48419], "name": "Other species"}
        }
        self.assertTrue(inat_finder.check_observation_genus(direct, "Amanita", 48419))
        self.assertTrue(inat_finder.check_observation_genus(ancestor, "Amanita", 48419))
        self.assertFalse(inat_finder.check_observation_genus({}, "Amanita", 48419))
        self.assertFalse(
            inat_finder.check_observation_genus({"taxon": []}, "Amanita", 48419)
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
            inat_finder.check_observation_genus(observation, "Amanita", 48419)
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
                48419,
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
                "ancestors": [{"id": 48419, "name": "Amanita", "rank": "genus"}],
            }
        }
        self.assertFalse(
            inat_finder.check_observation_genus(observation, "Amanita", 12345)
        )
        # The same observation matches when the verified ID is the one it carries.
        self.assertTrue(
            inat_finder.check_observation_genus(observation, "Amanita", 48419)
        )

    def test_same_name_taxon_with_different_id_is_not_a_match(self):
        # "Prunella" is both a plant genus and a bird genus. Searching for the bird
        # genus must not match a plant observation with the same genus name.
        plant = {
            "taxon": {
                "id": 500,
                "name": "Prunella vulgaris",
                "rank": "species",
                "ancestor_ids": [PLANTAE_ID, 999],
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
                "ancestor_ids": [PLANTAE_ID, 999],
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
            "results": [{"id": 48419, "name": "Amanita", "rank": "genus"}]
        }
        with patch.object(
            inat_finder, "api_get", side_effect=[autocomplete, fallback]
        ) as api_get:
            result = inat_finder.find_taxon("amanita", "genus")
        self.assertEqual(result["id"], 48419)
        self.assertEqual(api_get.call_args_list[0].kwargs["params"]["per_page"], 30)

    def test_find_taxon_returns_one_exact_match_after_both_lookups(self):
        taxon = {"id": 48419, "name": "Amanita", "rank": "genus"}
        with patch.object(
            inat_finder,
            "api_get_json",
            side_effect=[{"results": [taxon]}, {"results": []}],
        ) as api_get_json:
            result = inat_finder.find_taxon("amanita", "genus")
        self.assertIs(result, taxon)
        self.assertEqual(api_get_json.call_count, 2)

    def test_find_taxon_deduplicates_same_id_across_lookup_methods(self):
        autocomplete_taxon = {"id": 48419, "name": "Amanita", "rank": "genus"}
        search_taxon = {"id": "48419", "name": "Amanita", "rank": "genus"}
        with patch.object(
            inat_finder,
            "api_get_json",
            side_effect=[
                {"results": [autocomplete_taxon]},
                {"results": [search_taxon]},
            ],
        ):
            result = inat_finder.find_taxon("Amanita", "genus")
        self.assertIs(result, autocomplete_taxon)

    def test_find_taxon_rejects_distinct_exact_homonyms(self):
        first = {"id": 10, "name": "Duplicata", "rank": "genus"}
        second = {"id": 20, "name": "Duplicata", "rank": "genus"}
        with (
            patch.object(
                inat_finder,
                "api_get_json",
                side_effect=[{"results": [first]}, {"results": [second]}],
            ),
            self.assertRaises(inat_finder.TaxonAmbiguityError) as raised,
        ):
            inat_finder.find_taxon("Duplicata", "genus")
        self.assertEqual(
            [taxon["id"] for taxon in raised.exception.candidates], [10, 20]
        )

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
                return_value={"id": 48419, "name": "Amanita", "rank": "genus"},
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
                return_value={"id": 48419, "name": "Amanita", "rank": "genus"},
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
                "id": 48419,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestor_ids": [48419],
            },
            "user": {"login": "observer"},
        }
        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(
                inat_finder,
                "find_taxon",
                return_value={"id": 48419, "name": "Amanita", "rank": "genus"},
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
        fetch_observations.assert_called_once_with(["123456789"])
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


# Real iNaturalist taxon IDs, so fixtures stay recognisable to a maintainer who
# looks them up: 48419 is the genus Amanita, 48715 the species Amanita muscaria,
# 118249 the family Amanitaceae, 47170 the kingdom Fungi, 47126 the kingdom Plantae.
AMANITA_ID = 48419
AMANITA_MUSCARIA_ID = 48715
AMANITACEAE_ID = 118249
FUNGI_ID = 47170
PLANTAE_ID = 47126


def _observation(
    obs_id,
    login="observer",
    taxon_id=AMANITA_MUSCARIA_ID,
    ancestor_ids=(FUNGI_ID, AMANITACEAE_ID, AMANITA_ID),
):
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
        self.printed = printed
        return status, "\n".join(printed)

    def run_main_json(self, argv, patches=None, expect_locations=True):
        """Run main() with --json; return (status, parsed result, narration).

        The JSON object is the last thing written, after every human line, so it
        can be picked out of the captured output without the test having to model
        the stdout/stderr split that the real command line uses.
        """
        argv = list(argv)
        if "--json" not in argv:
            argv.append("--json")
        status, output = self.run_main(argv, patches, expect_locations)
        payload = next(
            (chunk for chunk in reversed(self.printed) if chunk.startswith("{")), None
        )
        self.assertIsNotNone(payload, f"no JSON object was printed:\n{output}")
        return status, json.loads(payload), output


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

    def test_permanent_failure_followed_by_success_does_not_abort(self):
        def per_batch(ids, call_number):
            if ids == ["1"]:
                return ApiError("offline")
            return [{"id": int(ids[0])}]

        fake, calls = self._api_get_json(per_batch)
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                ["1", "2", "3", "4", "5"], batch_size=1
            )
        self.assertEqual(len(calls), 6)
        self.assertEqual(result.unchecked, 1)
        self.assertEqual(result.failed_batches, 1)
        self.assertEqual(
            [observation["id"] for observation in result.observations], [2, 3, 4, 5]
        )

    def test_total_outage_stops_without_consuming_large_lazy_search(self):
        fake, calls = self._api_get_json(lambda ids, n: ApiError("offline"))
        candidate_count = 100_000
        yielded = []

        def candidates():
            for value in range(1, candidate_count + 1):
                yielded.append(value)
                yield str(value)

        progress = Mock()
        messages = []
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                candidates(),
                batch_size=2,
                total=candidate_count,
                progress_callback=progress,
                message_callback=messages.append,
            )

        attempted_candidates = inat_finder.MAX_CONSECUTIVE_FAILED_BATCHES * 2
        expected_requests = inat_finder.MAX_CONSECUTIVE_FAILED_BATCHES * (
            inat_finder.BATCH_RETRY_ROUNDS + 1
        )
        self.assertEqual(len(calls), expected_requests)
        self.assertEqual(len(yielded), attempted_candidates)
        self.assertEqual(result.unchecked, candidate_count)
        self.assertEqual(
            result.failed_batches, inat_finder.MAX_CONSECUTIVE_FAILED_BATCHES
        )
        progress.assert_called_once_with(candidate_count, False)
        self.assertTrue(any("Stopping after" in message for message in messages))

    def test_match_before_fail_fast_is_retained_and_progress_only_counts_success(self):
        match = _observation(1)

        def per_batch(ids, call_number):
            if ids == ["1"]:
                return [match]
            return ApiError("offline")

        fake, calls = self._api_get_json(per_batch)
        progress = Mock()
        total = 20
        with patch.object(inat_finder, "api_get_json", side_effect=fake):
            result = inat_finder.batch_check_observations(
                (str(value) for value in range(1, total + 1)),
                batch_size=1,
                total=total,
                progress_callback=progress,
                message_callback=lambda message: None,
            )
        self.assertEqual(result.observations, [match])
        self.assertEqual(result.unchecked, total - 1)
        self.assertEqual(
            len(calls),
            1
            + inat_finder.MAX_CONSECUTIVE_FAILED_BATCHES
            * (inat_finder.BATCH_RETRY_ROUNDS + 1),
        )
        self.assertEqual(
            [call.args for call in progress.call_args_list],
            [(1, True), (total - 1, False)],
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
                return_value={"id": 48419, "name": "Amanita", "rank": "genus"}
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


class TestNormalModeOriginalProjectCheck(MainRunnerMixin, unittest.TestCase):
    """The supplied ID is resolved before its project membership is evaluated."""

    def _run_project(self, fetch):
        return self.run_main_json(
            [
                "inat_finder.py",
                "--project",
                "fungi-map",
                "123456789",
                "--digits",
                "0",
                "--no-progress",
                "--yes",
            ],
            {
                "resolve_project_identifier": Mock(return_value=("42", PROJECT)),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )

    def test_nonmember_is_fetched_unfiltered_and_reported_as_existing(self):
        calls = []

        def fetch(ids, project_id=None, batch_size=None):
            calls.append((list(ids), project_id))
            return [] if project_id else [_observation(123456789)]

        status, result, output = self._run_project(fetch)

        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [(["123456789"], None), (["123456789"], "42")],
        )
        self.assertIn("exists but does not match project 'fungi-map'", output)
        self.assertNotIn("does not exist", output)
        self.assertEqual(result["original"]["id"], 123456789)
        self.assertEqual(result["matches"], [])

    def test_member_is_recognized_by_the_separate_membership_lookup(self):
        calls = []

        def fetch(ids, project_id=None, batch_size=None):
            calls.append((list(ids), project_id))
            return [_observation(123456789)]

        status, result, output = self._run_project(fetch)

        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [(["123456789"], None), (["123456789"], "42")],
        )
        self.assertIn("is in project 'Fungi Map'", output)
        self.assertEqual([match["id"] for match in result["matches"]], [123456789])

    def test_nonexistent_observation_does_not_trigger_membership_lookup(self):
        calls = []

        def fetch(ids, project_id=None, batch_size=None):
            calls.append((list(ids), project_id))
            return []

        status, result, _output = self._run_project(fetch)

        self.assertEqual(status, 0)
        self.assertEqual(calls, [(["123456789"], None)])
        self.assertIsNone(result["original"])

    def test_membership_failure_preserves_observation_without_claiming_nonmembership(self):
        def fetch(ids, project_id=None, batch_size=None):
            if project_id:
                raise ApiError("probe down")
            return [_observation(123456789)]

        status, result, output = self._run_project(fetch)

        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertIn("exists, but its project membership could not be checked", output)
        self.assertNotIn("does not match project", output)
        self.assertEqual(result["original"]["id"], 123456789)


class TestNormalModeJsonReportsTheSuppliedObservation(
    MainRunnerMixin, unittest.TestCase
):
    """A normal-mode result says what the number points at, and when it stopped."""

    def _patches(self, original=None, batch=None):
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
                return_value={123456789: "Pike Co. MS US"}
            ),
        }

    def test_declining_to_keep_searching_is_not_an_exhausted_search(self):
        """The variations were never checked, so nothing may claim completeness."""
        with patch("builtins.input", return_value="n"):
            status, result, output = self.run_main_json(
                ["inat_finder.py", "--user", "observer", "123456789", "--no-progress"],
                self._patches(),
            )
        self.assertEqual(status, 0)
        self.assertIn("Exiting search.", output)
        self.assertEqual(result["status"], "match_found")
        self.assertEqual(result["stop_reason"], "declined")
        self.assertFalse(result["complete"])
        self.assertEqual([match["id"] for match in result["matches"]], [123456789])
        self.assertEqual(result["original"]["id"], 123456789)

    def test_declining_a_large_search_is_not_an_exhausted_search(self):
        # Three digits off a nine-digit number is far past the large-search
        # threshold, so the second prompt is the one that stops the run.
        with patch("builtins.input", side_effect=["y", "n"]) as prompt:
            status, result, output = self.run_main_json(
                [
                    "inat_finder.py",
                    "--user",
                    "observer",
                    "123456789",
                    "--no-progress",
                    "--digits",
                    "3",
                ],
                self._patches(),
            )
        self.assertEqual(status, 0)
        # Prompts are written by input(), not print(), so the call log is what
        # says which of the two exits this run took.
        self.assertIn("This is a large search", prompt.call_args_list[1].args[0])
        self.assertIn("Exiting search.", output)
        self.assertEqual(result["status"], "match_found")
        self.assertEqual(result["stop_reason"], "declined")
        self.assertFalse(result["complete"])
        self.assertEqual(result["original"]["id"], 123456789)

    def test_declining_a_large_search_without_a_match_still_reports_the_number(self):
        """Nothing matched, but nothing was searched either - say both."""
        with patch("builtins.input", return_value="n"):
            status, result, output = self.run_main_json(
                [
                    "inat_finder.py",
                    "--user",
                    "observer",
                    "123456789",
                    "--no-progress",
                    "--digits",
                    "3",
                ],
                self._patches(
                    original=_observation(123456789, login="someone_else")
                ),
            )
        self.assertEqual(status, 0)
        self.assertIn("Search stopped at your request", output)
        self.assertNotIn("Search complete!", output)
        # The stock "why did this find nothing" advice would be a non sequitur.
        self.assertNotIn("The observation may have more than one digit mistyped", output)
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["stop_reason"], "declined")
        self.assertFalse(result["complete"])
        self.assertEqual(result["original"]["id"], 123456789)

    def test_continuing_to_the_end_still_reports_an_exhausted_search(self):
        status, result, _ = self.run_main_json(
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
        self.assertEqual(result["status"], "match_found")
        self.assertEqual(result["stop_reason"], "exhausted")
        self.assertTrue(result["complete"])
        self.assertEqual(result["original"]["id"], 123456789)

    def test_a_nonmatching_supplied_number_is_still_reported(self):
        """It matched nothing, but the caller still needs to see what it is."""
        status, result, output = self.run_main_json(
            [
                "inat_finder.py",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            self._patches(original=_observation(123456789, login="someone_else")),
        )
        self.assertEqual(status, 0)
        self.assertIn("exists but does not match", output)
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["original"]["id"], 123456789)
        self.assertEqual(result["original"]["user"], "someone_else")
        self.assertEqual(result["original"]["location"], "Pike Co. MS US")

    def test_a_missing_supplied_number_reports_no_original(self):
        patches = self._patches()
        patches["fetch_observations"] = Mock(return_value=[])
        status, result, _ = self.run_main_json(
            [
                "inat_finder.py",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            patches,
        )
        self.assertEqual(status, 0)
        self.assertIsNone(result["original"])


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

    def test_second_taxon_lookup_failure_is_not_hidden_by_first_match(self):
        taxon = {"id": 48419, "name": "Amanita", "rank": "genus"}
        with (
            patch.object(
                inat_finder,
                "api_get_json",
                side_effect=[{"results": [taxon]}, ApiError("fallback offline")],
            ),
            self.assertRaises(ApiError),
        ):
            inat_finder.find_taxon("Amanita", "genus")

    def test_ambiguous_taxon_is_input_error_and_lists_candidate_ids(self):
        candidates = [
            {
                "id": 10,
                "name": "Duplicata",
                "rank": "genus",
                "iconic_taxon_name": "Plantae",
            },
            {"id": 20, "name": "Duplicata", "rank": "genus"},
        ]
        fetch_observations = Mock()
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--genus",
                "Duplicata",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(
                    side_effect=inat_finder.TaxonAmbiguityError(
                        "Duplicata", "genus", candidates
                    )
                ),
                "fetch_observations": fetch_observations,
            },
        )
        self.assertEqual(status, 1)
        self.assertIn("ambiguous", output)
        self.assertIn("ID: 10", output)
        self.assertIn("ID: 20", output)
        self.assertNotIn("could not reach", output)
        fetch_observations.assert_not_called()

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


class TestTaxonIdArgumentParsing(unittest.TestCase):
    """--taxon-id is a first-class, mutually exclusive search criterion."""

    def _parse(self, argv):
        with patch.object(inat_finder.sys, "argv", list(argv)):
            return inat_finder.parse_arguments()

    def test_taxon_id_parses(self):
        args = self._parse(["inat_finder.py", "--taxon-id", "48419", "123456789"])
        self.assertEqual(args.taxon_id, "48419")
        self.assertIsNone(args.genus)
        self.assertIsNone(args.family)
        self.assertIsNone(args.user)
        self.assertIsNone(args.project)
        self.assertEqual(args.observation_number, "123456789")

    def _assert_conflicts_with(self, *conflicting):
        """Assert argparse rejects the flag combination, and pin the exit status.

        Conflicting criteria are a command-line syntax error, so argparse handles
        them and exits 2 with a usage message on stderr. That is argparse's own
        status, not the script's API-failure status: the script's own bad-input
        checks (an invalid --taxon-id value, for instance) exit 1. Pinned here so a
        future change to the parser cannot silently move it.
        """
        argv = ["inat_finder.py", "--taxon-id", "48419", *conflicting, "123456789"]
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            self._parse(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_taxon_id_is_mutually_exclusive_with_genus(self):
        self._assert_conflicts_with("--genus", "Amanita")

    def test_taxon_id_is_mutually_exclusive_with_family(self):
        self._assert_conflicts_with("--family", "Amanitaceae")

    def test_taxon_id_is_mutually_exclusive_with_user(self):
        self._assert_conflicts_with("--user", "observer")

    def test_taxon_id_is_mutually_exclusive_with_project(self):
        self._assert_conflicts_with("--project", "fungi-map")

    def test_valid_taxon_ids_are_accepted(self):
        self.assertEqual(inat_finder.parse_taxon_id_argument("48419"), 48419)
        self.assertEqual(inat_finder.parse_taxon_id_argument(" 48419 "), 48419)
        self.assertEqual(inat_finder.parse_taxon_id_argument("1"), 1)

    def test_invalid_taxon_ids_are_rejected(self):
        for value in ("0", "-1", "-48419", "abc", "", "  ", "48419.0", "4e5", "47 158"):
            with self.subTest(value=value):
                self.assertIsNone(inat_finder.parse_taxon_id_argument(value))


class TestTaxonIdInputValidation(MainRunnerMixin, unittest.TestCase):
    """Bad --taxon-id values exit 1 without ever contacting iNaturalist."""

    def _run(self, value):
        api_get = Mock(side_effect=AssertionError("no request for invalid input"))
        return self.run_main(
            ["inat_finder.py", "--taxon-id", value, "123456789", "--no-progress"],
            {"api_get": api_get, "api_get_json": api_get},
        )

    def test_zero_is_rejected(self):
        status, output = self._run("0")
        self.assertEqual(status, 1)
        self.assertIn("--taxon-id must be a positive iNaturalist taxon ID", output)

    def test_negative_is_rejected(self):
        status, output = self._run("-5")
        self.assertEqual(status, 1)
        self.assertIn("--taxon-id must be a positive iNaturalist taxon ID", output)

    def test_non_numeric_is_rejected(self):
        status, output = self._run("Amanita")
        self.assertEqual(status, 1)
        self.assertIn("--taxon-id must be a positive iNaturalist taxon ID", output)

    def test_malformed_is_rejected(self):
        status, output = self._run("48419,60773")
        self.assertEqual(status, 1)
        self.assertIn("--taxon-id must be a positive iNaturalist taxon ID", output)


class TestFindTaxonById(unittest.TestCase):
    """Taxon-ID lookups separate 'not there' from 'could not look it up'."""

    def setUp(self):
        inat_finder.RATE_LIMITER.reset()

    def _response(self, status_code, payload=None, unreadable=False):
        response = Mock(status_code=status_code)
        if unreadable:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = payload
        return response

    def test_valid_taxon_resolves(self):
        taxon = {
            "id": 48419,
            "name": "Amanita",
            "rank": "genus",
            "preferred_common_name": "fly agarics",
            "iconic_taxon_name": "Fungi",
        }
        with patch.object(
            inat_finder,
            "api_get",
            return_value=self._response(200, {"results": [taxon]}),
        ):
            self.assertEqual(inat_finder.find_taxon_by_id(48419), taxon)

    def test_missing_taxon_returns_none(self):
        with patch.object(inat_finder, "api_get", return_value=self._response(404)):
            self.assertIsNone(inat_finder.find_taxon_by_id(999999999999))

    def test_empty_results_return_none(self):
        with patch.object(
            inat_finder, "api_get", return_value=self._response(200, {"results": []})
        ):
            self.assertIsNone(inat_finder.find_taxon_by_id(999999999999))

    def test_api_failure_propagates(self):
        with (
            patch.object(inat_finder, "api_get", side_effect=ApiError("503")),
            self.assertRaises(ApiError),
        ):
            inat_finder.find_taxon_by_id(48419)

    def test_unreadable_body_is_not_not_found(self):
        with (
            patch.object(
                inat_finder,
                "api_get",
                return_value=self._response(200, unreadable=True),
            ),
            self.assertRaises(ApiError),
        ):
            inat_finder.find_taxon_by_id(48419)

    def test_malformed_payloads_are_not_not_found(self):
        for payload in ({}, {"results": None}, {"results": {"id": 48419}}, []):
            with (
                self.subTest(payload=payload),
                patch.object(
                    inat_finder,
                    "api_get",
                    return_value=self._response(200, payload),
                ),
                self.assertRaises(ApiError),
            ):
                inat_finder.find_taxon_by_id(48419)

    def test_results_for_a_different_taxon_are_not_not_found(self):
        with (
            patch.object(
                inat_finder,
                "api_get",
                return_value=self._response(
                    200, {"results": [{"id": 99, "name": "X"}]}
                ),
            ),
            self.assertRaises(ApiError),
        ):
            inat_finder.find_taxon_by_id(48419)


class TestTaxonIdMatching(unittest.TestCase):
    """--taxon-id matching is strictly ancestry/ID based."""

    def test_exact_taxon_id_matches(self):
        observation = _observation(
            1, taxon_id=AMANITA_ID, ancestor_ids=(FUNGI_ID, AMANITA_ID)
        )
        self.assertTrue(inat_finder.check_observation_taxon_id(observation, AMANITA_ID))

    def test_descendant_matches_through_ancestor_ids(self):
        # Amanita muscaria sits under the genus Amanita.
        observation = _observation(
            2,
            taxon_id=AMANITA_MUSCARIA_ID,
            ancestor_ids=(FUNGI_ID, AMANITACEAE_ID, AMANITA_ID),
        )
        self.assertTrue(inat_finder.check_observation_taxon_id(observation, AMANITA_ID))

    def test_descendant_matches_through_ancestors_records(self):
        observation = {
            "id": 3,
            "taxon": {
                "id": 48715,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestors": [{"id": 48419, "name": "Amanita", "rank": "genus"}],
            },
        }
        self.assertTrue(inat_finder.check_observation_taxon_id(observation, 48419))

    def test_unrelated_observation_does_not_match(self):
        # A plant, nowhere near the genus Amanita.
        observation = _observation(4, taxon_id=52786, ancestor_ids=(PLANTAE_ID, 52785))
        self.assertFalse(
            inat_finder.check_observation_taxon_id(observation, AMANITA_ID)
        )

    def test_identical_name_cannot_override_an_id_mismatch(self):
        # A hypothetical plant genus sharing the fungal genus's name. IDs 998-1000
        # are deliberately fictional; only the name collision matters here.
        observation = {
            "id": 5,
            "taxon": {
                "id": 999,
                "name": "Amanita",
                "rank": "genus",
                "ancestor_ids": [PLANTAE_ID, 998],
            },
        }
        self.assertFalse(
            inat_finder.check_observation_taxon_id(observation, AMANITA_ID)
        )
        # The species name-prefix heuristic must not rescue it either.
        species = {
            "id": 6,
            "taxon": {
                "id": 1000,
                "name": "Amanita muscaria",
                "rank": "species",
                "ancestor_ids": [PLANTAE_ID, 999],
            },
        }
        self.assertFalse(inat_finder.check_observation_taxon_id(species, 48419))

    def test_missing_or_empty_taxa_do_not_match(self):
        self.assertFalse(inat_finder.check_observation_taxon_id({}, 48419))
        self.assertFalse(inat_finder.check_observation_taxon_id({"id": 1}, 48419))
        self.assertFalse(
            inat_finder.check_observation_taxon_id({"id": 1, "taxon": None}, 48419)
        )


AMANITA_TAXON = {
    "id": AMANITA_ID,
    "name": "Amanita",
    "rank": "genus",
    "preferred_common_name": "fly agarics",
    "iconic_taxon_name": "Fungi",
}


class TestTaxonIdSearch(MainRunnerMixin, unittest.TestCase):
    """End-to-end --taxon-id behaviour through main()."""

    def _patches(self, original=None, batch=None, **extra):
        patches = {
            "find_taxon_by_id": Mock(return_value=dict(AMANITA_TAXON)),
            "fetch_observations": Mock(
                return_value=[original] if original is not None else []
            ),
            "batch_check_observations": Mock(
                side_effect=batch
                or (lambda *a, **k: inat_finder.BatchCheckResult([], 0, 0))
            ),
            "resolve_observation_locations": Mock(return_value={}),
        }
        patches.update(extra)
        return patches

    def _argv(self, *extra):
        return [
            "inat_finder.py",
            "--taxon-id",
            "48419",
            "123456789",
            "--no-progress",
            "--yes",
            *extra,
        ]

    def test_valid_taxon_is_verified_and_described(self):
        status, output = self.run_main(self._argv(), self._patches())
        self.assertEqual(status, 0)
        self.assertIn("Verifying taxon ID 48419 exists on iNaturalist...", output)
        self.assertIn("Taxon ID 48419 verified: Amanita (genus)", output)
        self.assertIn("Common name: fly agarics", output)
        self.assertIn("Iconic taxon: Fungi", output)
        self.assertIn(
            "Looking for iNaturalist observations belonging to Amanita "
            "(taxon ID 48419)",
            output,
        )

    def test_missing_taxon_is_input_error(self):
        missing = Mock(status_code=404)
        with patch.object(inat_finder, "api_get", return_value=missing):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--taxon-id",
                    "999999999999",
                    "123456789",
                    "--no-progress",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("Error: Taxon ID 999999999999 not found on iNaturalist.", output)
        self.assertNotIn("could not reach", output)

    def test_taxon_lookup_api_failure_is_operational(self):
        with patch.object(inat_finder, "api_get", side_effect=ApiError("timeout")):
            status, output = self.run_main(
                ["inat_finder.py", "--taxon-id", "48419", "123456789", "--no-progress"]
            )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertNotIn("not found on iNaturalist", output)
        self.assertIn("could not reach the iNaturalist API", output)

    def test_malformed_lookup_response_is_not_not_found(self):
        garbage = Mock(status_code=200)
        garbage.json.return_value = {"results": "nope"}
        with patch.object(inat_finder, "api_get", return_value=garbage):
            status, output = self.run_main(
                ["inat_finder.py", "--taxon-id", "48419", "123456789", "--no-progress"]
            )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertNotIn("not found on iNaturalist", output)

    def test_matching_original_observation_is_recognised(self):
        original = _observation(123456789, taxon_id=48715, ancestor_ids=(47170, 48419))
        with patch("builtins.input", return_value="n"):
            status, output = self.run_main(
                [
                    "inat_finder.py",
                    "--taxon-id",
                    "48419",
                    "123456789",
                    "--no-progress",
                ],
                self._patches(original=original),
            )
        self.assertEqual(status, 0)
        self.assertIn(
            "already belongs to Amanita (taxon ID 48419).",
            output,
        )
        self.assertIn("Found 1 potential matches", output)

    def test_nonmatching_original_observation_continues_searching(self):
        original = _observation(123456789, taxon_id=52786, ancestor_ids=(47126,))
        batch_check = Mock(
            side_effect=lambda *a, **k: inat_finder.BatchCheckResult([], 0, 0)
        )
        status, output = self.run_main(
            self._argv(),
            self._patches(original=original, batch_check_observations=batch_check),
        )
        self.assertEqual(status, 0)
        self.assertIn(
            "does not belong to taxon ID 48419 (Amanita).",
            output,
        )
        batch_check.assert_called_once()
        self.assertIn("2. The taxon ID might be incorrect", output)

    def test_matching_original_appears_exactly_once_when_search_continues(self):
        original = _observation(123456789, taxon_id=48715, ancestor_ids=(48419,))

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([dict(original)])
            return inat_finder.BatchCheckResult([dict(original)], 0, 0)

        status, output = self.run_main(
            self._argv(), self._patches(original=original, batch=fake_batch)
        )
        self.assertEqual(status, 0)
        self.assertIn("Found 1 potential matches", output)
        self.assertEqual(output.count("Observation #123456789"), 1)

    def test_matching_variation_is_reported(self):
        alternate = _observation(
            123456788, login="someone", taxon_id=48715, ancestor_ids=(47170, 48419)
        )
        unrelated = _observation(
            123456787, login="other", taxon_id=52786, ancestor_ids=(47126,)
        )

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([alternate, unrelated])
            return inat_finder.BatchCheckResult([], 0, 0)

        status, output = self.run_main(
            self._argv(),
            self._patches(
                batch=fake_batch,
                resolve_observation_locations=Mock(
                    return_value={123456788: "Pike Co. MS US"}
                ),
            ),
        )
        self.assertEqual(status, 0)
        self.assertIn("Found 1 potential matches", output)
        self.assertIn("Observation #123456788 - Amanita muscaria", output)
        self.assertIn("Created by: someone", output)
        self.assertIn("Location: Pike Co. MS US", output)
        self.assertIn("URL: https://www.inaturalist.org/observations/123456788", output)
        self.assertNotIn("123456787", output)

    def test_no_matches_uses_taxon_id_wording(self):
        status, output = self.run_main(self._argv(), self._patches())
        self.assertEqual(status, 0)
        self.assertIn("No matches found. Consider these possibilities:", output)
        self.assertIn("2. The taxon ID might be incorrect", output)
        self.assertNotIn("genus name might be incorrect", output)
        self.assertNotIn("family name might be incorrect", output)
        self.assertNotIn("username might be incorrect", output)
        self.assertNotIn("project might be incorrect", output)

    def test_incomplete_search_still_reports_partial_results(self):
        alternate = _observation(123456788, taxon_id=48715, ancestor_ids=(48419,))

        def fake_batch(variations, batch_size=None, **kwargs):
            kwargs["results_callback"]([alternate])
            return inat_finder.BatchCheckResult([], 17, 1)

        status, output = self.run_main(self._argv(), self._patches(batch=fake_batch))
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertIn("Search incomplete - results may be incomplete.", output)
        self.assertIn("17 candidate(s) could not be checked", output)
        self.assertIn("Found 1 potential matches (partial results)", output)


HOMONYM_CANDIDATES = [
    {"id": 10, "name": "Duplicata", "rank": "genus", "iconic_taxon_name": "Plantae"},
    {"id": 20, "name": "Duplicata", "rank": "genus", "iconic_taxon_name": "Fungi"},
]


class TestTaxonAmbiguityPointsAtTaxonId(MainRunnerMixin, unittest.TestCase):
    """A homonym must be resolvable with the IDs the error prints."""

    def _run(self, *extra):
        fetch_observations = Mock()
        return self.run_main(
            [
                "inat_finder.py",
                "--genus",
                "Duplicata",
                "123456789",
                "--no-progress",
                *extra,
            ],
            {
                "find_taxon": Mock(
                    side_effect=inat_finder.TaxonAmbiguityError(
                        "Duplicata", "genus", HOMONYM_CANDIDATES
                    )
                ),
                "fetch_observations": fetch_observations,
            },
        )

    def test_candidate_ids_are_listed(self):
        status, output = self._run()
        self.assertEqual(status, 1)
        self.assertIn("ID: 10; scientific name: Duplicata; rank: genus", output)
        self.assertIn("ID: 20; scientific name: Duplicata; rank: genus", output)
        self.assertIn("iconic taxon: Plantae", output)
        self.assertIn("iconic taxon: Fungi", output)

    def test_message_tells_the_user_to_use_taxon_id(self):
        status, output = self._run()
        self.assertEqual(status, 1)
        self.assertIn("Re-run the search using the desired taxon ID", output)
        self.assertIn("--taxon-id", output)
        self.assertNotIn(
            "Please use a taxon name and rank that resolve to one iNaturalist", output
        )

    def test_suggested_command_uses_the_supplied_observation_number(self):
        status, output = self._run()
        self.assertEqual(status, 1)
        self.assertIn("python inat_finder.py --taxon-id 10 123456789", output)

    def test_suggested_command_keeps_a_non_default_digits(self):
        status, output = self._run("--digits", "2")
        self.assertEqual(status, 1)
        self.assertIn(
            "python inat_finder.py --taxon-id 10 123456789 --digits 2", output
        )

    def test_suggested_command_works_from_a_url(self):
        fetch_observations = Mock()
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--genus",
                "Duplicata",
                "https://www.inaturalist.org/observations/123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(
                    side_effect=inat_finder.TaxonAmbiguityError(
                        "Duplicata", "genus", HOMONYM_CANDIDATES
                    )
                ),
                "fetch_observations": fetch_observations,
            },
        )
        self.assertEqual(status, 1)
        self.assertIn("--taxon-id 10 123456789", output)


class TestExistingCriteriaStillWork(MainRunnerMixin, unittest.TestCase):
    """Adding --taxon-id must not disturb the other search criteria."""

    def _run(self, argv, patches):
        base = {
            "fetch_observations": Mock(return_value=[]),
            "batch_check_observations": Mock(
                return_value=inat_finder.BatchCheckResult([], 0, 0)
            ),
            "resolve_observation_locations": Mock(return_value={}),
        }
        base.update(patches)
        return self.run_main(argv + ["--no-progress", "--yes"], base)

    def test_genus_still_reports_genus_wording(self):
        status, output = self._run(
            ["inat_finder.py", "--genus", "Amanita", "123456789"],
            {
                "find_taxon": Mock(
                    return_value={"id": 48419, "name": "Amanita", "rank": "genus"}
                )
            },
        )
        self.assertEqual(status, 0)
        self.assertIn(
            "Looking for iNaturalist observations with genus 'Amanita'", output
        )
        self.assertIn("2. The genus name might be incorrect", output)

    def test_family_still_reports_family_wording(self):
        status, output = self._run(
            ["inat_finder.py", "--family", "Amanitaceae", "123456789"],
            {
                "find_taxon": Mock(
                    return_value={
                        "id": 118249,
                        "name": "Amanitaceae",
                        "rank": "family",
                    }
                )
            },
        )
        self.assertEqual(status, 0)
        self.assertIn(
            "Looking for iNaturalist observations in family 'Amanitaceae'", output
        )
        self.assertIn("2. The family name might be incorrect", output)

    def test_user_still_reports_user_wording(self):
        status, output = self._run(
            ["inat_finder.py", "--user", "observer", "123456789"],
            {"verify_user_exists": Mock(return_value=True)},
        )
        self.assertEqual(status, 0)
        self.assertIn(
            "Looking for iNaturalist observations created by user 'observer'", output
        )
        self.assertIn("2. The username might be incorrect", output)

    def test_project_still_reports_project_wording(self):
        status, output = self._run(
            ["inat_finder.py", "--project", "fungi-map", "123456789"],
            {
                "resolve_project_identifier": Mock(
                    return_value=(
                        "42",
                        {"id": 42, "title": "Fungi Map", "slug": "fungi-map"},
                    )
                )
            },
        )
        self.assertEqual(status, 0)
        self.assertIn(
            "Looking for iNaturalist observations in project 'Fungi Map'", output
        )
        self.assertIn("2. The project might be incorrect", output)


GENUS_TAXON = {"id": AMANITA_ID, "name": "Amanita", "rank": "genus"}
FAMILY_TAXON = {"id": AMANITACEAE_ID, "name": "Amanitaceae", "rank": "family"}
PROJECT = {"id": 42, "title": "Fungi Map", "slug": "fungi-map"}


class AutoModeMixin(MainRunnerMixin):
    """Drive --auto searches against a fake iNaturalist that returns fixed IDs."""

    def fetcher(self, present, calls=None, project_members=None, fail=None):
        """Build a fetch_observations stand-in.

        ``present`` are the observation IDs that exist. ``project_members``, when
        given, are the IDs a project-filtered request returns. ``fail`` is called
        with (ids, project_id) and may raise to simulate a failing request.
        """
        calls = [] if calls is None else calls

        def fetch(ids, project_id=None, batch_size=None):
            ids = [str(value) for value in ids]
            calls.append({"ids": ids, "project_id": project_id})
            if fail is not None:
                fail(ids, project_id)
            if project_id is not None:
                members = present if project_members is None else project_members
                return [_observation(int(i)) for i in ids if int(i) in members]
            return [_observation(int(i)) for i in ids if int(i) in present]

        return fetch, calls

    def requested_ids(self, calls, project_id=None):
        """Every observation ID asked for, in order, optionally for one filter."""
        return [
            value
            for call in calls
            if project_id is None or call["project_id"] == project_id
            for value in call["ids"]
        ]


class TestAutoModeLadder(AutoModeMixin, unittest.TestCase):
    """--auto climbs from the number as given to progressively wider typos."""

    def test_hit_in_stage_one_never_reaches_stage_two(self):
        fetch, calls = self.fetcher({123456788})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Observation #123456788", output)
        self.assertIn("Stage 1", output)
        self.assertNotIn("Stage 2", output)
        # Stage 0 is one ID; stage 1 for a nine-digit number is a single batch.
        self.assertEqual(len(calls), 2)

    def test_empty_stage_one_escalates_to_stage_two(self):
        # 123456700 differs from 123456789 in two digits, so only stage 2 has it.
        fetch, _calls = self.fetcher({123456700})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Stage 2", output)
        self.assertIn("Observation #123456700", output)
        self.assertIn("Found at stage 2", output)

    def test_a_small_stage_is_finished_after_a_full_match(self):
        """A hit early in a cheap stage must not hide the rest of that stage.

        With one clue every hit is a full match, and iNaturalist numbers run in
        upload order, so the first hit is routinely a neighbour by the same person
        rather than the observation being looked for. The stage runs to the end and
        both candidates are reported.
        """
        stage_one = {int(value) for value in build_candidate_plan("123456789", 1)}
        stage_two = [
            value
            for value in build_candidate_plan("123456789", 2)
            if int(value) not in stage_one
        ]
        early = stage_two[0]
        # Deliberately in a later batch than `early`, which used to end the stage.
        late = stage_two[inat_finder.BATCH_SIZE + 5]
        fetch, calls = self.fetcher({int(early), int(late)})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn(f"Observation #{int(early)}", output)
        self.assertIn(f"Observation #{int(late)}", output)
        self.assertNotIn("because a full match was found", output)
        # Stage 0, stage 1, then every batch of stage 2 - not just the first.
        stage_two_batches = (
            len(stage_two) + inat_finder.BATCH_SIZE - 1
        ) // inat_finder.BATCH_SIZE
        self.assertEqual(len(calls), 2 + stage_two_batches)
        # And it still stops there rather than climbing to stage 3.
        self.assertNotIn("Stage 3", output)

    def test_full_match_stops_before_the_next_batch_in_a_large_stage(self):
        """Intra-stage stopping survives where finishing would cost minutes."""
        stage_two = {int(value) for value in build_candidate_plan("123456789", 2)}
        stage_three = [
            value
            for value in build_candidate_plan("123456789", 3)
            if int(value) not in stage_two
        ]
        self.assertGreater(len(stage_three), inat_finder.EARLY_STOP_MIN_CANDIDATES)
        early = stage_three[0]
        # A second observation shares the batch and must still be reported.
        companion = stage_three[1]
        fetch, calls = self.fetcher({int(early), int(companion)})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
                "--digits",
                "3",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        # Stage 0, all of stage 1 and 2, then exactly one batch of stage 3.
        def batches(count):
            return (count + inat_finder.BATCH_SIZE - 1) // inat_finder.BATCH_SIZE

        stage_one_total = build_candidate_plan("123456789", 1).total
        expected = (
            1
            + batches(stage_one_total)
            + batches(len(stage_two) - stage_one_total)
            + 1
        )
        self.assertEqual(len(calls), expected)
        self.assertLessEqual(len(calls[-1]["ids"]), inat_finder.BATCH_SIZE)
        self.assertIn(f"Observation #{int(early)}", output)
        self.assertIn(f"Observation #{int(companion)}", output)
        self.assertIn("because a full match was found", output)

    def test_no_observation_id_is_requested_twice(self):
        fetch, calls = self.fetcher(set())
        status, _output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--digits",
                "2",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        asked = self.requested_ids(calls)
        self.assertEqual(len(asked), len(set(asked)))

    def test_stage_one_fail_fast_leaves_stage_two_totals_exact(self):
        """The bug this accounting exists to avoid.

        Stage sizes are ``plan.total - len(seen_ids)``, not
        ``plan_k.total - plan_(k-1).total``. When stage 1 stops early after a run
        of permanently failed batches, its unyielded candidates were never marked
        as tried, so differencing the plan totals would declare a stage 2 far
        smaller than the one that actually runs - corrupting both the progress
        bar and the unchecked accounting that depends on that total.
        """
        number = "1870671"
        plan_one = build_candidate_plan(number, 1)
        plan_two = build_candidate_plan(number, 2)
        seen = set()
        stage_one = AutoStage(1, plan_one, seen)
        self.assertEqual(stage_one.total, plan_one.total)

        iterator = iter(stage_one)
        for _ in range(inat_finder.BATCH_SIZE):
            next(iterator)
        self.assertEqual(len(seen), inat_finder.BATCH_SIZE)

        stage_two = AutoStage(2, plan_two, seen)
        self.assertEqual(stage_two.total, plan_two.total - len(seen))
        self.assertGreater(stage_two.total, plan_two.total - plan_one.total)

        # The candidates stage 1 never reached are picked up by stage 2, and the
        # declared total is exactly what the stage goes on to yield.
        leftovers = {int(value) for value in plan_one} - set(seen)
        yielded = {int(value) for value in stage_two}
        self.assertTrue(leftovers)
        self.assertTrue(leftovers <= yielded)
        self.assertEqual(stage_two.total, len(yielded))

    def test_the_plans_nest_so_the_ladder_never_repeats_or_misses(self):
        """The invariant the whole ladder rests on.

        Stage k is plan k minus what earlier stages tried, which is only sound
        because the plans nest as sets. Checked across shapes that stress the
        deduplication: repeated digits (fewer swaps and removals survive), a
        leading one followed by zeros, and lengths that switch the insertion and
        removal classes on and off.
        """
        for number in ("123456789", "1234567", "1122334", "100000", "187067127"):
            with self.subTest(number=number):
                plans = [build_candidate_plan(number, k) for k in (1, 2, 3)]
                sets = [{int(value) for value in plan} for plan in plans]
                self.assertTrue(sets[0] <= sets[1] <= sets[2])
                for plan, candidates in zip(plans, sets):
                    self.assertEqual(plan.total, len(candidates))

                seen = set()
                for index, plan in zip((1, 2, 3), plans):
                    stage = AutoStage(index, plan, seen)
                    announced = stage.total
                    yielded = list(stage)
                    # What the stage announced is exactly what it goes on to
                    # check - the progress bar and the unchecked accounting both
                    # depend on that.
                    self.assertEqual(announced, len(yielded))
                    self.assertEqual(len(yielded), len(set(yielded)))
                # Every candidate, once, and nothing beyond the widest plan.
                self.assertEqual(seen, sets[2])

    def test_stage_labels_describe_what_is_really_searched(self):
        """Stage 1 is never just 'one digit off'."""
        label = inat_finder.auto_stage_label(1, build_candidate_plan("123456789", 1))
        self.assertIn("one substituted digit", label)
        self.assertIn("adjacent swaps", label)
        # Nine digits: removals apply, insertions do not.
        self.assertIn("extra digits", label)
        self.assertNotIn("missing", label)
        short = inat_finder.auto_stage_label(1, build_candidate_plan("1234567", 1))
        self.assertIn("missing or extra digits", short)
        self.assertEqual(
            inat_finder.auto_stage_label(2, build_candidate_plan("123456789", 2)),
            "two substituted digits and extra digits",
        )


class TestAutoModeRanking(AutoModeMixin, unittest.TestCase):
    """Equal scores are the normal case, so the tie-break has to mean something."""

    def test_equal_scores_are_ordered_by_stage_then_closeness(self):
        """With one clue every hit scores 1 of 1; ID order would be arbitrary.

        The three IDs here are chosen so that the answer differs under each rule:
        ascending ID would put the far one first, and closeness alone would put a
        stage-2 candidate ahead of a stage-1 one.
        """
        stage_one = [int(value) for value in build_candidate_plan("123456789", 1)]
        stage_two = [
            int(value)
            for value in build_candidate_plan("123456789", 2)
            if int(value) not in set(stage_one)
        ]
        near_stage_one = min(stage_one, key=lambda value: abs(value - 123456789))
        far_stage_one = max(stage_one, key=lambda value: abs(value - 123456789))
        near_stage_two = min(stage_two, key=lambda value: abs(value - 123456789))
        self.assertLess(
            abs(near_stage_two - 123456789), abs(far_stage_one - 123456789)
        )

        fetch, _calls = self.fetcher({near_stage_one, far_stage_one, near_stage_two})
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        found = [entry["id"] for entry in result["matches"]]
        # Stage 1 finishes, so both of its hits are here; stage 2 never runs.
        expected = sorted(
            [near_stage_one, far_stage_one], key=lambda value: abs(value - 123456789)
        )
        self.assertEqual(found, expected)
        self.assertNotIn(near_stage_two, found)

    def test_the_original_number_outranks_an_equally_scoring_neighbour(self):
        """Stage 0 is distance zero, so nothing can tie-break ahead of it."""
        neighbour = 123456788
        fetch, _calls = self.fetcher({123456789, neighbour})
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["matches"][0]["id"], 123456789)

    def test_rank_matches_without_an_origin_falls_back_to_id_order(self):
        matches = [
            inat_finder.ScoredMatch({"id": 30}, ["user"], [], 1),
            inat_finder.ScoredMatch({"id": 10}, ["user"], [], 1),
            inat_finder.ScoredMatch({"id": 20}, ["user", "genus"], [], 2),
        ]
        ranked = [match.observation["id"] for match in rank_matches(matches)]
        self.assertEqual(ranked, [20, 10, 30])

    def test_rank_matches_sorts_a_stageless_match_last_among_equals(self):
        """A non-auto search leaves stage None; math.inf must not raise or win."""
        matches = [
            inat_finder.ScoredMatch({"id": 10}, ["user"], [], None),
            inat_finder.ScoredMatch({"id": 30}, ["user"], [], 2),
            inat_finder.ScoredMatch({"id": 20}, ["user"], [], 1),
        ]
        ranked = [match.observation["id"] for match in rank_matches(matches)]
        self.assertEqual(ranked, [20, 30, 10])


class TestAutoModeSingleClueWarning(AutoModeMixin, unittest.TestCase):
    """One clue cannot separate neighbours, and the tool has to say so."""

    def test_several_full_matches_on_one_clue_are_called_a_ranking(self):
        stage_one = [int(value) for value in build_candidate_plan("123456789", 1)]
        fetch, _calls = self.fetcher(set(stage_one[:3]))
        status, result, output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(result["matches"]), 3)
        self.assertTrue(
            any("best guess rather than an answer" in notice
                for notice in result["notices"]),
            result["notices"],
        )
        self.assertIn("the order they were uploaded", output)

    def test_a_single_full_match_is_not_hedged(self):
        fetch, _calls = self.fetcher({123456788})
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["notices"], [])

    def test_two_clues_do_not_trigger_the_warning(self):
        stage_one = [int(value) for value in build_candidate_plan("123456789", 1)]
        fetch, _calls = self.fetcher(set(stage_one[:3]))
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(result["matches"]), 3)
        self.assertEqual(result["notices"], [])


class TestAutoModeClues(AutoModeMixin, unittest.TestCase):
    """Clues are clues: a wrong one is dropped, an outage is still an outage."""

    def test_one_bad_clue_does_not_stop_the_good_one(self):
        fetch, _calls = self.fetcher({123456788})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanitaa",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=None),
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Ignoring the genus clue", output)
        self.assertIn("was unusable", output)
        self.assertIn("Observation #123456788", output)

    def test_api_failure_while_resolving_a_clue_is_not_a_bad_clue(self):
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {"find_taxon": Mock(side_effect=ApiError("timeout"))},
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertIn("could not reach the iNaturalist API", output)
        self.assertNotIn("Ignoring the genus clue", output)

    def test_malformed_taxon_id_is_still_fatal_in_auto_mode(self):
        status, output = self.run_main(
            ["inat_finder.py", "--auto", "--taxon-id", "abc", "123456789"],
            {"find_taxon_by_id": Mock(side_effect=AssertionError("must not look up"))},
        )
        self.assertEqual(status, 1)
        self.assertIn("--taxon-id must be a positive", output)

    def test_partial_match_does_not_end_the_ladder(self):
        """A near miss is a reason to keep looking, not a reason to stop."""
        # 123456788 matches the genus only; 123456700 matches genus and user, and
        # lives two digits away, so it can only be found by widening to stage 2.
        def fetch(ids, project_id=None, batch_size=None):
            found = []
            for value in ids:
                if int(value) == 123456788:
                    found.append(_observation(123456788, login="someone_else"))
                elif int(value) == 123456700:
                    found.append(_observation(123456700, login="observer"))
            return found

        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--user",
                "observer",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Stage 2", output)
        # Best-first: the full match outranks the partial one.
        first = output.index("1. Observation #123456700")
        second = output.index("2. Observation #123456788")
        self.assertLess(first, second)
        self.assertIn("[2 of 2: genus, user]", output)
        self.assertIn("[1 of 2: genus]", output)

    def test_no_clues_reports_the_number_without_enumerating(self):
        fetch, calls = self.fetcher({123456789})
        status, output = self.run_main(
            ["inat_finder.py", "--auto", "123456789", "--no-progress"],
            {"fetch_observations": Mock(side_effect=fetch)},
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("Observation #123456789 exists", output)
        self.assertIn("No usable clue was supplied", output)


class TestAutoModeProjects(AutoModeMixin, unittest.TestCase):
    """Project membership is batch evidence, and may legitimately be unknown."""

    def test_project_alone_still_filters_server_side(self):
        fetch, calls = self.fetcher({123456788})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--project",
                "fungi-map",
                "123456789",
                "--no-progress",
            ],
            {
                "resolve_project_identifier": Mock(return_value=("42", PROJECT)),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Observation #123456788", output)
        # One request per search batch, every one of them carrying the project
        # filter. Stage 0 is deliberately unfiltered - it asks what the supplied
        # number is, which a membership filter would refuse to answer.
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0]["project_id"])
        self.assertEqual(calls[0]["ids"], ["123456789"])
        self.assertTrue(all(call["project_id"] == "42" for call in calls[1:]))

    def test_supplied_number_outside_the_project_is_not_called_nonexistent(self):
        """A non-member observation exists; only its membership is a 'no'."""
        fetch, calls = self.fetcher({123456789}, project_members=set())
        status, result, output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--project",
                "fungi-map",
                "123456789",
                "--no-progress",
                "--digits",
                "1",
                "--yes",
            ],
            {
                "resolve_project_identifier": Mock(return_value=("42", PROJECT)),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertNotIn("does not exist", output)
        self.assertIn("Observation #123456789 exists", output)
        self.assertIn("matched 0 of 1 clue(s)", output)
        # The supplied number is reported for what it points at, not dropped.
        self.assertIsNotNone(result["original"])
        self.assertEqual(result["original"]["id"], 123456789)
        self.assertEqual(result["matches"], [])
        # Stage 0: one unfiltered lookup, then one membership probe.
        self.assertIsNone(calls[0]["project_id"])
        self.assertEqual(calls[1], {"ids": ["123456789"], "project_id": "42"})

    def test_project_with_another_clue_probes_membership_separately(self):
        fetch, calls = self.fetcher({123456788}, project_members={123456788})
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--project",
                "fungi-map",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "resolve_project_identifier": Mock(return_value=("42", PROJECT)),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("[2 of 2: genus, project]", output)
        # The main request must not be filtered, or the genus clue could never
        # match anything outside the project.
        self.assertTrue(any(call["project_id"] is None for call in calls))
        self.assertTrue(any(call["project_id"] == "42" for call in calls))

    def test_failed_membership_probe_keeps_the_other_evidence(self):
        def fail(ids, project_id):
            if project_id is not None:
                raise ApiError("probe down")

        fetch, _calls = self.fetcher({123456788}, fail=fail)
        status, result, output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--project",
                "fungi-map",
                "123456789",
                "--no-progress",
                "--digits",
                "1",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "resolve_project_identifier": Mock(return_value=("42", PROJECT)),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertEqual(result["status"], "incomplete")
        # The genus evidence survives; only the project clue is unknown.
        self.assertEqual(
            [(m["id"], m["matched"], m["unknown"]) for m in result["matches"]],
            [(123456788, ["genus"], ["project"])],
        )
        # An incomplete search must never hand out a cursor that would skip the gap.
        self.assertIsNone(result["resume"])
        self.assertIn("unknown", output)

    def test_unresolvable_project_is_a_clue_in_auto_mode_only(self):
        fetch, _calls = self.fetcher({123456788})
        patches = {
            "find_taxon": Mock(return_value=GENUS_TAXON),
            "search_projects_by_query": Mock(return_value=[]),
            "api_get_json": Mock(return_value=None),
            "fetch_observations": Mock(side_effect=fetch),
        }
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--project",
                "no such project",
                "123456789",
                "--no-progress",
            ],
            patches,
        )
        self.assertEqual(status, 0)
        self.assertIn("Ignoring the project clue", output)
        self.assertIn("Observation #123456788", output)

        # Without --auto the same project is still a fatal input error.
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--project",
                "no such project",
                "123456789",
                "--no-progress",
            ],
            {
                "search_projects_by_query": Mock(return_value=[]),
                "api_get_json": Mock(return_value=None),
            },
        )
        self.assertEqual(status, 1)


class TestAutoModeResume(AutoModeMixin, unittest.TestCase):
    """A stop must be resumable, because the web front end stops all the time."""

    def test_token_round_trip(self):
        self.assertEqual(
            parse_resume_token(build_resume_token(2, 400, "abc12345")),
            (2, 400, "abc12345"),
        )

    def test_malformed_tokens_are_rejected(self):
        for value in ("", "2:400", "v1:2:400", "v9:2:400:abc", "vx:2:400:abc", 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_resume_token(value)

    def test_restore_replays_exactly_what_the_original_run_had_tried(self):
        number = "123456789"
        plan_one = build_candidate_plan(number, 1)
        plan_two = build_candidate_plan(number, 2)
        seen = set()
        stage_one = AutoStage(1, plan_one, seen)
        list(stage_one)
        stage_two = AutoStage(2, plan_two, seen)
        iterator = iter(stage_two)
        for _ in range(inat_finder.BATCH_SIZE):
            next(iterator)
        self.assertEqual(
            restore_seen_ids(number, 2, stage_two.plan_position), set(seen)
        )

    def test_resume_continues_without_repeating_a_single_id(self):
        stage_one = {int(value) for value in build_candidate_plan("123456789", 1)}
        early = next(
            value
            for value in build_candidate_plan("123456789", 2)
            if int(value) not in stage_one
        )
        fetch, first_calls = self.fetcher({int(early)})
        # --digits 3, because stage 2 is now finished rather than abandoned at the
        # hit: the cursor it hands back points at the start of stage 3, and a cap
        # of 2 would leave nothing for the resume to do.
        status, first_result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
                "--digits",
                "3",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(first_result["status"], "match_found")
        token = first_result["resume"]["token"]
        self.assertEqual(first_result["resume"]["stage"], 3)
        self.assertEqual(first_result["resume"]["offset"], 0)

        fetch_again, second_calls = self.fetcher(set())
        status, second_result, output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
                "--digits",
                "3",
                "--auto-resume",
                token,
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch_again),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Resuming at stage 3", output)
        self.assertEqual(second_result["status"], "no_match")
        # Stages 0 to 2 are replayed offline, not re-requested.
        self.assertNotIn("Stage 1", output)
        self.assertNotIn("Stage 2", output)
        first_ids = set(self.requested_ids(first_calls))
        second_ids = set(self.requested_ids(second_calls))
        self.assertTrue(second_ids)
        self.assertEqual(first_ids & second_ids, set())

    def test_a_cursor_outside_the_ladder_is_refused(self):
        """A fingerprint proves provenance, not that the coordinates can exist.

        The stage and offset are plain text in the token, so an edited or stale
        cursor could otherwise point at a stage the ladder does not have - which
        would quietly search nothing and report a clean "no match".
        """
        number = "123456789"
        fingerprint = inat_finder.search_fingerprint(
            number, [inat_finder._user_criterion("observer")], 2
        )
        oversized = build_candidate_plan(number, 2).total + 1
        cases = {
            "stage above the cap": build_resume_token(3, 0, fingerprint),
            "stage zero": build_resume_token(0, 0, fingerprint),
            "offset past the plan": build_resume_token(2, oversized, fingerprint),
        }
        for label, token in cases.items():
            with self.subTest(case=label):
                fetch, calls = self.fetcher(set())
                status, result, _output = self.run_main_json(
                    [
                        "inat_finder.py",
                        "--auto",
                        "--user",
                        "observer",
                        number,
                        "--no-progress",
                        "--digits",
                        "2",
                        "--auto-resume",
                        token,
                    ],
                    {
                        "verify_user_exists": Mock(return_value=True),
                        "fetch_observations": Mock(side_effect=fetch),
                    },
                )
                self.assertEqual(status, 1)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["stop_reason"], "bad_resume")
                self.assertFalse(result["complete"])
                self.assertEqual(calls, [])

    def test_a_token_from_another_search_is_refused(self):
        fetch, _calls = self.fetcher(set())
        good = inat_finder.search_fingerprint("123456789", [], 2)
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--auto-resume",
                build_resume_token(2, 0, good),
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("different search", result["message"])

    def test_auto_resume_without_auto_is_a_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, _output = self.run_main(
                [
                    "inat_finder.py",
                    "--genus",
                    "Amanita",
                    "123456789",
                    "--auto-resume",
                    "v1:2:0:abc12345",
                ]
            )
        self.assertEqual(status, 2)
        self.assertIn("only meaningful with --auto", stderr.getvalue())

    def test_no_cursor_is_issued_when_anything_went_unchecked(self):
        """Failure then match: report the match, but never a resume cursor.

        The failed batch's IDs are already marked as tried, so a cursor would skip
        them for good and turn a gap into a confident 'no match' later on.
        """
        # A seven-digit number gives stage 1 many batches, so one can fail
        # permanently while a later one still carries the match.
        candidates = [str(value) for value in build_candidate_plan("1234567", 1)]
        doomed = set(candidates[: inat_finder.BATCH_SIZE])
        match_id = int(candidates[inat_finder.BATCH_SIZE * 2 + 5])

        def fail(ids, project_id):
            if set(ids) & doomed:
                raise ApiError("offline")

        fetch, _calls = self.fetcher({match_id}, fail=fail)
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "1234567",
                "--no-progress",
                "--digits",
                "1",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual([m["id"] for m in result["matches"]], [match_id])
        self.assertIsNone(result["resume"])
        self.assertGreaterEqual(result["unchecked"], inat_finder.BATCH_SIZE)


class TestAutoModeCompleteness(AutoModeMixin, unittest.TestCase):
    """Anything that leaves candidates unsearched must say it is not complete."""

    def test_stage_zero_membership_failure_makes_the_search_incomplete(self):
        """Stage 0 is never re-run on a resume, so its gaps cannot be deferred.

        Without this the original observation's project membership could go
        unanswered, the run could still pause at a later stage and hand out a
        cursor, and the resumed run would skip stage 0 entirely - losing the
        question for good.
        """
        def fail(ids, project_id):
            if project_id is not None:
                raise ApiError("probe down")

        fetch, _calls = self.fetcher({123456789}, fail=fail)
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--project",
                "fungi-map",
                "123456789",
                "--no-progress",
                "--digits",
                "1",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "resolve_project_identifier": Mock(return_value=("42", PROJECT)),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["resume"])
        # The genus evidence for the original observation is still reported.
        self.assertEqual(
            [(m["id"], m["matched"], m["unknown"]) for m in result["matches"]],
            [(123456789, ["genus"], ["project"])],
        )

    def test_needs_confirmation_is_not_complete(self):
        fetch, _calls = self.fetcher(set())
        _status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertFalse(result["complete"])

    def test_completeness_is_stated_for_every_way_a_search_can_end(self):
        complete = inat_finder.outcome_is_complete
        # Ran to a conclusion.
        self.assertTrue(complete("match_found", "full_match"))
        self.assertTrue(complete("no_match", "exhausted"))
        self.assertTrue(complete("no_match", "no_clues"))
        # Stopped with candidates deliberately left unsearched.
        self.assertFalse(complete("match_found", "declined"))
        self.assertFalse(complete("no_match", "declined"))
        self.assertFalse(complete("error", "too_large"))
        self.assertFalse(complete("needs_confirmation", "large_stage"))
        # Did not finish at all.
        self.assertFalse(complete("incomplete", "failures"))
        self.assertFalse(complete("cancelled", "interrupted"))
        self.assertFalse(complete("error", "usage"))

    def test_declining_a_large_stage_stops_and_offers_a_resume(self):
        fetch, _calls = self.fetcher(set())
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
                "stdin_is_interactive": Mock(return_value=True),
                "get_user_confirmation": Mock(return_value=False),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Stopped before the larger stage", output)
        self.assertIn("--auto-resume", output)

    def test_an_unsearchably_large_stage_is_an_error_not_a_no_match(self):
        """The stage was never searched, so 'nothing there' is not established."""
        fetch, _calls = self.fetcher(set())
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
                "--digits",
                "9",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
                "MAX_SEARCH_CANDIDATES": 200,
            },
        )
        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stop_reason"], "too_large")
        self.assertFalse(result["complete"])

    def test_a_finished_search_is_complete(self):
        fetch, _calls = self.fetcher(set())
        _status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--digits",
                "1",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(result["status"], "no_match")
        self.assertTrue(result["complete"])

    def test_interrupt_reports_what_the_stage_had_really_checked(self):
        """A cancelled search still has honest progress numbers.

        The counts come from the batch loop's own state rather than being reset
        to zero, so a page can say "searched 400 of 3,093" instead of "0".
        """
        state = {"batches": 0}
        completed = 2

        def fetch(ids, project_id=None, batch_size=None):
            if len(list(ids)) == 1:
                return []
            state["batches"] += 1
            if state["batches"] > completed:
                raise KeyboardInterrupt
            return []

        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "1234567",
                "--no-progress",
                "--digits",
                "1",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 130)
        self.assertEqual(result["status"], "cancelled")
        stage = result["stages"][-1]
        self.assertEqual(stage["stage"], 1)
        self.assertEqual(stage["attempted"], completed * inat_finder.BATCH_SIZE)
        self.assertEqual(stage["total"], build_candidate_plan("1234567", 1).total)

    def test_interrupt_keeps_matches_found_earlier_in_the_same_stage(self):
        """Ctrl+C reports what was found so far, including the running stage."""
        state = {"batches": 0}

        def fetch(ids, project_id=None, batch_size=None):
            ids = [str(value) for value in ids]
            if len(ids) == 1:
                return []
            state["batches"] += 1
            if state["batches"] == 1:
                # Matches the genus but not the user, so the ladder keeps going
                # instead of stopping on a full match before the interrupt lands.
                return [_observation(int(ids[0]), login="someone_else")]
            raise KeyboardInterrupt

        expected = int(next(iter(build_candidate_plan("1234567", 1))))
        status, result, output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "--user",
                "observer",
                "1234567",
                "--no-progress",
                "--digits",
                "1",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "verify_user_exists": Mock(return_value=True),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 130)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["complete"])
        self.assertEqual([m["id"] for m in result["matches"]], [expected])
        self.assertIn("Search interrupted", output)


class TestAutoModeConfirmation(AutoModeMixin, unittest.TestCase):
    """A stage too big to run unattended asks, or reports that it needs to."""

    def test_non_interactive_returns_needs_confirmation(self):
        fetch, _calls = self.fetcher(set())
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
                "get_user_confirmation": Mock(
                    side_effect=AssertionError("must not prompt")
                ),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["stage"], 3)
        self.assertGreater(result["estimated_candidates"], 5000)
        self.assertEqual(result["resume"]["stage"], 3)
        self.assertEqual(result["resume"]["offset"], 0)

    def test_yes_proceeds_through_the_large_stage(self):
        fetch, calls = self.fetcher(set())
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
                "--yes",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
                "get_user_confirmation": Mock(
                    side_effect=AssertionError("--yes must not prompt")
                ),
            },
        )
        self.assertEqual(status, 0)
        self.assertIn("Stage 3", output)
        self.assertGreater(len(calls), 100)

    def test_a_terminal_still_gets_a_prompt(self):
        fetch, _calls = self.fetcher(set())
        confirm = Mock(return_value=False)
        status, output = self.run_main(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
                "stdin_is_interactive": Mock(return_value=True),
                "get_user_confirmation": confirm,
            },
        )
        self.assertEqual(status, 0)
        confirm.assert_called_once()
        self.assertIn("large stage", confirm.call_args.args[0])
        self.assertIn("--auto-resume", output)


class TestAutoModeJson(AutoModeMixin, unittest.TestCase):
    """--json is an interface: statuses and exit codes are part of the contract."""

    def test_match_found_shape(self):
        fetch, _calls = self.fetcher({123456788})
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "match_found")
        self.assertTrue(result["complete"])
        self.assertEqual(result["version"], inat_finder.JSON_RESULT_VERSION)
        self.assertEqual(result["clues"], ["genus"])
        self.assertEqual(result["stop_reason"], "full_match")
        match = result["matches"][0]
        self.assertEqual(match["id"], 123456788)
        self.assertEqual(match["score"], 1)
        self.assertEqual(match["matched"], ["genus"])
        self.assertEqual(match["stage"], 1)
        self.assertEqual(
            match["url"], "https://www.inaturalist.org/observations/123456788"
        )
        self.assertEqual([entry["stage"] for entry in result["stages"]], [0, 1])

    def test_every_status_maps_to_its_documented_exit_code(self):
        for status_name, expected in inat_finder.STATUS_EXIT_CODES.items():
            with self.subTest(status=status_name):
                outcome = inat_finder.SearchOutcome(status=status_name)
                self.assertEqual(inat_finder._emit(None, outcome, {}), expected)

    def test_bad_input_still_produces_a_parsable_result(self):
        """Even the exit paths that predate --json must not print half an object."""
        status, result, _output = self.run_main_json(
            ["inat_finder.py", "--auto", "--genus", "Amanita", "not-a-number"],
            {"find_taxon": Mock(return_value=GENUS_TAXON)},
        )
        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "error")

    def test_json_works_for_a_normal_search_too(self):
        fetch, _calls = self.fetcher({123456788})
        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "match_found")
        self.assertEqual([m["id"] for m in result["matches"]], [123456788])
        # A normal search has no ladder, so it has no stage and nothing to resume.
        self.assertIsNone(result["stage"])
        self.assertIsNone(result["resume"])

    def test_interrupt_is_reported_as_cancelled(self):
        def fetch(ids, project_id=None, batch_size=None):
            if len(ids) > 1:
                raise KeyboardInterrupt
            return []

        status, result, _output = self.run_main_json(
            [
                "inat_finder.py",
                "--auto",
                "--genus",
                "Amanita",
                "123456789",
                "--no-progress",
            ],
            {
                "find_taxon": Mock(return_value=GENUS_TAXON),
                "fetch_observations": Mock(side_effect=fetch),
            },
        )
        self.assertEqual(status, 130)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["complete"])


class TestAutoModeDoesNotDisturbNormalSearches(MainRunnerMixin, unittest.TestCase):
    """The old command line keeps its behaviour, including its exit codes."""

    def test_digits_still_defaults_to_one_without_auto(self):
        with patch.object(
            inat_finder.sys, "argv", ["inat_finder.py", "--genus", "A", "123456789"]
        ):
            self.assertEqual(inat_finder.parse_arguments().digits, 1)

    def test_digits_defaults_to_the_ladder_cap_with_auto(self):
        with patch.object(
            inat_finder.sys,
            "argv",
            ["inat_finder.py", "--auto", "--genus", "A", "123456789"],
        ):
            args = inat_finder.parse_arguments()
        self.assertEqual(args.digits, inat_finder.AUTO_DEFAULT_MAX_DIGITS)

    def test_explicit_digits_still_wins_in_both_modes(self):
        for argv in (
            ["inat_finder.py", "--genus", "A", "123456789", "--digits", "2"],
            ["inat_finder.py", "--auto", "--genus", "A", "123456789", "--digits", "2"],
        ):
            with self.subTest(argv=argv), patch.object(inat_finder.sys, "argv", argv):
                self.assertEqual(inat_finder.parse_arguments().digits, 2)

    def test_missing_criterion_is_still_a_usage_error(self):
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            patch.object(inat_finder.sys, "argv", ["inat_finder.py", "123456789"]),
            self.assertRaises(SystemExit) as raised,
        ):
            inat_finder.parse_arguments()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("is required", stderr.getvalue())

    def test_conflicting_criteria_are_still_a_usage_error(self):
        stderr = io.StringIO()
        argv = ["inat_finder.py", "--genus", "Amanita", "--user", "x", "123456789"]
        with (
            contextlib.redirect_stderr(stderr),
            patch.object(inat_finder.sys, "argv", argv),
            self.assertRaises(SystemExit) as raised,
        ):
            inat_finder.parse_arguments()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_auto_accepts_every_criterion_at_once(self):
        argv = [
            "inat_finder.py",
            "--auto",
            "--genus",
            "Amanita",
            "--family",
            "Amanitaceae",
            "--user",
            "observer",
            "123456789",
        ]
        with patch.object(inat_finder.sys, "argv", argv):
            args = inat_finder.parse_arguments()
        self.assertEqual(args.genus, "Amanita")
        self.assertEqual(args.family, "Amanitaceae")
        self.assertEqual(args.user, "observer")


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Bootstrap for the subprocess tests: point the script at a local stub and take
# the rate limiter out of the way, then hand over to main() exactly as the real
# console entry point does.
SUBPROCESS_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {repo!r}); import inat_finder; "
    "inat_finder.API_BASE_URL = {base!r}; "
    "inat_finder.RATE_LIMITER.min_interval = 0; "
    "inat_finder.main()"
)


class _StubHandler(BaseHTTPRequestHandler):
    """Answers just enough of the iNaturalist API to drive a real search."""

    # Shared by every request the stub server handles; set once per test class.
    present: typing.ClassVar[set] = set()

    def log_message(self, *args):
        """Silence BaseHTTPRequestHandler's default logging to stderr."""

    def _send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/taxa/autocomplete") or parsed.path.endswith("/taxa"):
            self._send({"results": [dict(GENUS_TAXON)]})
        elif parsed.path.endswith("/observations"):
            ids = [
                value
                for chunk in query.get("id", [])
                for value in chunk.split(",")
                if value
            ]
            self._send(
                {
                    "results": [
                        _observation(int(value))
                        for value in ids
                        if int(value) in type(self).present
                    ]
                }
            )
        else:
            self._send({"results": []})


class TestJsonSubprocessContract(unittest.TestCase):
    """stdout is exactly one JSON document for every invocation carrying --json.

    These run the real interpreter rather than patching print(), because the
    in-process helpers cannot model what a web front end actually reads. The
    failure this class exists to prevent is an argparse error - a missing
    observation number, an unknown option, two conflicting criteria - exiting
    before any JSON is written, which leaves the caller a bare status 2 and
    nothing to show a user.
    """

    server = None
    thread = None

    @classmethod
    def setUpClass(cls):
        _StubHandler.present = {123456788}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base_url = f"http://{host}:{port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def invoke(self, *args, base_url=None):
        """Run the script for real; return (exit status, parsed stdout, stderr)."""
        bootstrap = SUBPROCESS_BOOTSTRAP.format(
            repo=REPO_ROOT, base=base_url or self.base_url
        )
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap, *args],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
            check=False,
        )
        # The whole of stdout must parse. Anything else - a stray print, a
        # half-written object, an empty stream - fails here.
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"stdout was not one JSON document ({error})\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            )
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["version"], inat_finder.JSON_RESULT_VERSION)
        self.assertEqual(payload["exit_code"], proc.returncode)
        return proc.returncode, payload, proc.stderr

    def test_a_real_match_round_trips(self):
        status, payload, stderr = self.invoke(
            "--json", "--auto", "--genus", "Amanita", "123456789", "--no-progress"
        )
        self.assertEqual(status, 0)
        self.assertEqual(payload["status"], "match_found")
        self.assertTrue(payload["complete"])
        self.assertEqual([m["id"] for m in payload["matches"]], [123456788])
        # Narration goes to stderr, so stdout stays parsable.
        self.assertIn("Verifying genus", stderr)

    def test_needs_confirmation_round_trips(self):
        status, payload, _stderr = self.invoke(
            "--json", "--auto", "--genus", "Amanita", "999999999", "--no-progress"
        )
        self.assertEqual(status, 0)
        self.assertEqual(payload["status"], "needs_confirmation")
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["stage"], 3)
        self.assertGreater(payload["estimated_candidates"], 5000)
        self.assertEqual(payload["resume"]["offset"], 0)

    def test_missing_observation_number_is_json_not_a_bare_usage_error(self):
        status, payload, stderr = self.invoke("--json", "--genus", "Amanita")
        self.assertEqual(status, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stop_reason"], "usage")
        self.assertFalse(payload["complete"])
        self.assertIn("observation_number", payload["message"])
        # The human-facing usage message is still on stderr, unchanged.
        self.assertIn("usage:", stderr)

    def test_conflicting_criteria_are_json(self):
        status, payload, _stderr = self.invoke(
            "--json", "--genus", "Amanita", "--user", "observer", "123456789"
        )
        self.assertEqual(status, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stop_reason"], "usage")
        self.assertIn("not allowed with argument", payload["message"])

    def test_missing_criteria_are_json(self):
        status, payload, _stderr = self.invoke("--json", "123456789")
        self.assertEqual(status, 2)
        self.assertEqual(payload["stop_reason"], "usage")
        self.assertIn("is required", payload["message"])

    def test_unknown_option_is_json(self):
        status, payload, _stderr = self.invoke(
            "--json", "--genus", "Amanita", "123456789", "--nonsense"
        )
        self.assertEqual(status, 2)
        self.assertEqual(payload["stop_reason"], "usage")

    def test_auto_resume_without_auto_is_json(self):
        status, payload, _stderr = self.invoke(
            "--json",
            "--genus",
            "Amanita",
            "123456789",
            "--auto-resume",
            "v1:2:0:abc12345",
        )
        self.assertEqual(status, 2)
        self.assertEqual(payload["stop_reason"], "usage")
        self.assertIn("only meaningful with --auto", payload["message"])

    def test_malformed_taxon_id_is_json(self):
        status, payload, _stderr = self.invoke(
            "--json", "--auto", "--taxon-id", "abc", "123456789"
        )
        self.assertEqual(status, 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stop_reason"], "bad_input")
        self.assertFalse(payload["complete"])
        self.assertIn("--taxon-id", payload["message"])

    def test_normal_mode_clue_failure_explains_itself_in_json(self):
        """--json is documented for both modes, so both must say *why*.

        Without --auto an unknown genus is fatal, and the explanation used to
        exist only on stderr - leaving a page with an error it could not show.
        """
        status, payload, stderr = self.invoke(
            "--json", "--genus", "DefinitelyNotAGenus", "123456789", "--no-progress"
        )
        self.assertEqual(status, 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stop_reason"], "bad_input")
        self.assertFalse(payload["complete"])
        self.assertIn("DefinitelyNotAGenus", payload["message"])
        self.assertIn("not found", payload["message"])
        self.assertEqual(
            [clue["kind"] for clue in payload["unusable_clues"]], ["genus"]
        )
        self.assertIn("not found in iNaturalist taxonomy", stderr)

    def test_normal_mode_unknown_user_explains_itself_in_json(self):
        status, payload, _stderr = self.invoke(
            "--json", "--user", "nobody_at_all", "123456789", "--no-progress"
        )
        self.assertEqual(status, 1)
        self.assertEqual(payload["stop_reason"], "bad_input")
        self.assertIn("nobody_at_all", payload["message"])

    def test_a_non_numeric_observation_number_is_json(self):
        status, payload, _stderr = self.invoke(
            "--json", "--auto", "--genus", "Amanita", "not-a-number"
        )
        self.assertEqual(status, 1)
        self.assertEqual(payload["stop_reason"], "bad_input")
        self.assertIn("only digits", payload["message"])

    def test_invalid_resume_token_is_json(self):
        status, payload, _stderr = self.invoke(
            "--json", "--auto", "--genus", "Amanita", "123456789", "--auto-resume", "junk"
        )
        self.assertEqual(status, 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stop_reason"], "bad_resume")

    def test_unreachable_api_is_json_and_not_a_clean_no_match(self):
        # Port 1 refuses connections, so this is an outage rather than a result.
        status, payload, _stderr = self.invoke(
            "--json",
            "--auto",
            "--genus",
            "Amanita",
            "123456789",
            "--no-progress",
            base_url="http://127.0.0.1:1/v1",
        )
        self.assertEqual(status, inat_finder.API_FAILURE_EXIT_CODE)
        self.assertEqual(payload["status"], "incomplete")
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["matches"], [])

    def test_json_abbreviation_still_gets_a_json_usage_error(self):
        """argparse accepts --js, so the pre-parse scan must accept it too."""
        status, payload, _stderr = self.invoke("--js", "--genus", "Amanita")
        self.assertEqual(status, 2)
        self.assertEqual(payload["stop_reason"], "usage")


if __name__ == "__main__":
    unittest.main()
