import unittest
from unittest.mock import patch

import inat_finder
from inat_finder import (
    generate_digit_additions,
    generate_digit_removals,
    generate_digit_variations,
    parse_inat_url,
    preprocess_argv_for_project_name,
)

class TestInatFinderFunctions(unittest.TestCase):
    # Test methods for generate_digit_variations
    def test_gdv_no_change(self):
        self.assertEqual(generate_digit_variations("123", 0), ["123"])
        self.assertEqual(generate_digit_variations("123", -1), ["123"])

    def test_gdv_single_digit_off(self):
        # For "12", digits_off=1
        # Expected: 02, 22, 32, ..., 92 (9 variations for first digit)
        #           10, 11, 13, ..., 19 (9 variations for second digit)
        # Total = 9 + 9 = 18
        variations = generate_digit_variations("12", 1)
        self.assertEqual(len(variations), 18)
        self.assertIn("02", variations) # Changed first digit
        self.assertIn("92", variations) # Changed first digit
        self.assertIn("10", variations) # Changed second digit
        self.assertIn("19", variations) # Changed second digit
        self.assertNotIn("12", variations) # Original number should not be present

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
        # Test case for digits_off > 1
        # For "12", digits_off=2.
        # Each position must change.
        # Pos 0 (was '1') can be any of 9 digits (0, 2-9)
        # Pos 1 (was '2') can be any of 9 digits (0, 1, 3-9)
        # Total variations = 9 * 9 = 81
        variations = generate_digit_variations("12", 2)
        self.assertEqual(len(set(variations)), 81) # Function returns a list from a set, so it's unique
        self.assertEqual(len(variations), 81) 
        
        # Specific checks:
        self.assertIn("00", variations) # 1->0, 2->0
        self.assertIn("01", variations) # 1->0, 2->1
        self.assertIn("20", variations) # 1->2, 2->0
        self.assertIn("98", variations) # 1->9, 2->8
        self.assertNotIn("12", variations) # Original
        self.assertNotIn("02", variations) # Only one digit changed from "12"
        self.assertNotIn("10", variations) # Only one digit changed from "12"

        # For "123", digits_off=2
        # Combinations of 2 positions to change: (0,1), (0,2), (1,2)
        # For (0,1) changing, '1' and '2' change, '3' stays: 9*9*1 = 81 variations (e.g., "003")
        # For (0,2) changing, '1' and '3' change, '2' stays: 9*9*1 = 81 variations (e.g., "020")
        # For (1,2) changing, '2' and '3' change, '1' stays: 9*9*1 = 81 variations (e.g., "100")
        # Total = 81 + 81 + 81 = 243
        variations_123_2_off = generate_digit_variations("123", 2)
        self.assertEqual(len(variations_123_2_off), 243)
        self.assertIn("003", variations_123_2_off) # 1->0, 2->0, 3 stays
        self.assertIn("020", variations_123_2_off) # 1->0, 3->0, 2 stays
        self.assertIn("100", variations_123_2_off) # 2->0, 3->0, 1 stays
        self.assertNotIn("123", variations_123_2_off) # Original
        self.assertNotIn("023", variations_123_2_off) # Only 1 digit changed
        self.assertNotIn("120", variations_123_2_off) # Only 1 digit changed

    def test_gdv_uniqueness(self):
        # The function uses a set internally, so uniqueness is expected.
        # This test is more of a confirmation.
        variations = generate_digit_variations("111", 1) # Should be "011", "211", ..., "101", "121", ...
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
        # Test with default max_added_digits=2
        number = "1"
        variations = generate_digit_additions(number)
        
        # Expected counts:
        # Single prefix: 10 (01, 11, ... 91)
        # Single suffix: 10 (10, 11, ... 19)
        # Double prefix: 100 (001, 011, ... 991)
        # Double suffix: 100 (100, 101, ... 199)
        # Single prefix + single suffix: 100 (010, 011, ... 919)
        # Total = 10 + 10 + 100 + 100 + 100 = 320
        
        # Note: "11" is generated by single prefix (1+1) and single suffix (1+1)
        # The function returns a list, so duplicates are possible if not handled.
        # The current implementation of generate_digit_additions can produce duplicates.
        # For example, if number_str is "1", "11" can be str(1)+number_str or number_str+str(1).
        # Let's check for presence and then for count using set for uniqueness.
        
        self.assertIn("01", variations) # single prefix
        self.assertIn("10", variations) # single suffix
        self.assertIn("001", variations) # double prefix
        self.assertIn("100", variations) # double suffix
        self.assertIn("010", variations) # pre+suff

        # Check counts of unique variations
        # Expected unique variations:
        # "1" -> 
        # Prefixes: 01, 11, ..., 91 (10)
        # Suffixes: 10, (11 already counted), 12, ..., 19 (9 new)
        # Total single additions = 10 + 9 = 19 (if "11" is generated once)
        # Or 20 if "11" generated twice and list is expected.
        # The function returns a list, so we test the list as is.
        # If "11" is an issue, the problem statement didn't ask to change gda for uniqueness.
        # The original gda:
        # variations.append(str(digit) + number_str)
        # variations.append(number_str + str(digit))
        # if number_str="1", digit=1: "11" added twice.
        
        # Let's count based on the implementation.
        # Number of variations should be 320.
        self.assertEqual(len(variations), 320)

        # Test uniqueness if that was a requirement (it's not explicitly for gda)
        # self.assertEqual(len(set(variations)), 320 - number of overlaps)
        # Overlaps for "1":
        # "11" (single prefix 1, single suffix 1)
        # "111" (double prefix 11, single prefix 1 + suffix 1, single prefix 11 + number)
        # This level of detail for gda uniqueness might be over-testing based on prompt.
        # The prompt: "Ensure the function returns the same set of variations as the original, just generated more cleanly."
        # The original did not guarantee uniqueness for gda.

    def test_gda_max_digits_respected(self):
        # This test assumes we can control max_added_digits,
        # but the current signature in main is generate_digit_additions(obs_number, 2)
        # The function itself has max_added_digits=2 as default.
        # To test this properly, we'd call with max_added_digits=1.
        
        # If we call generate_digit_additions("1", 1)
        # variations = []
        # single prefix "d1": 10
        # single suffix "1d": 10
        # if max_added_digits >= 2 is false. So total 20.
        variations_one_digit = generate_digit_additions("1", max_added_digits=1)
        self.assertEqual(len(variations_one_digit), 20)
        self.assertIn("01", variations_one_digit)
        self.assertIn("10", variations_one_digit)
        self.assertNotIn("001", variations_one_digit) # double prefix
        self.assertNotIn("100", variations_one_digit) # double suffix
        self.assertNotIn("010", variations_one_digit) # pre+suff

    # Test methods for parse_inat_url
    def test_piu_valid_url(self):
        self.assertEqual(parse_inat_url("https://www.inaturalist.org/observations/12345"), "12345")
        self.assertEqual(parse_inat_url("http://www.inaturalist.org/observations/67890"), "67890")
        self.assertEqual(parse_inat_url("https://inaturalist.org/observations/123"), "123") # No www

    def test_piu_url_with_query_params(self):
        self.assertEqual(parse_inat_url("https://www.inaturalist.org/observations/12345?param=value&another=true"), "12345")

    def test_piu_not_a_url(self):
        self.assertEqual(parse_inat_url("12345"), "12345") # Should return itself

    def test_piu_invalid_url_format(self):
        # Different site - current behavior extracts if 'observations/\d+' is found
        self.assertEqual(parse_inat_url("https://www.example.com/observations/12345"), "12345")
        # Incorrect iNat path
        self.assertEqual(parse_inat_url("https://www.inaturalist.org/obs/12345"), "https://www.inaturalist.org/obs/12345")
        self.assertEqual(parse_inat_url("https://www.inaturalist.org/observations/"), "https://www.inaturalist.org/observations/")

    def test_piu_url_no_number(self):
        self.assertEqual(parse_inat_url("https://www.inaturalist.org/observations/abc"), "https://www.inaturalist.org/observations/abc")
        self.assertEqual(parse_inat_url("https://www.inaturalist.org/observations/"), "https://www.inaturalist.org/observations/")

    def test_project_preprocess_preserves_five_digit_observation_id(self):
        argv = ["inat_finder.py", "--project", "my-slug", "12345"]
        self.assertEqual(
            preprocess_argv_for_project_name(argv),
            ["inat_finder.py", "--project", "my-slug", "12345"],
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
        self.assertEqual(len(variations), 2) # "11", "12"
        self.assertCountEqual(variations, ["11", "12"])

    def test_gdr_empty_and_short(self):
        self.assertEqual(generate_digit_removals("", 2), [])
        self.assertEqual(generate_digit_removals("1", 2), [])
        self.assertEqual(generate_digit_removals("1", 1), [])
        self.assertCountEqual(generate_digit_removals("12", 2), ["1", "2"])

    def test_main_sleeps_between_outer_variation_batches(self):
        argv = [
            "inat_finder.py",
            "--genus",
            "Amanita",
            "123456789",
            "--digits",
            "2",
            "--no-progress",
        ]
        with (
            patch.object(inat_finder.sys, "argv", argv),
            patch.object(inat_finder, "verify_genus_exists", return_value=True),
            patch.object(inat_finder, "batch_check_observations", return_value=[])
            as batch_check,
            patch.object(inat_finder.time, "sleep") as sleep,
            patch("builtins.print"),
        ):
            inat_finder.main()

        variation_batch_calls = batch_check.call_args_list[1:]
        self.assertGreater(len(variation_batch_calls), 1)
        self.assertEqual(sleep.call_count, len(variation_batch_calls) - 1)
        sleep.assert_any_call(1)


if __name__ == '__main__':
    unittest.main()
