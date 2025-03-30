#!/usr/bin/env python3
"""
iNaturalist Observation Finder

Version 1.2 - By Alan Rockefeller - March 29, 2025

This script helps find the correct iNaturalist observation number when there are mistyped digits.
It works by systematically changing digits of the provided observation number and checking if
any of those variations match the specified genus in the iNaturalist database.

If the observation number has fewer than 9 digits, it will also try adding up to two digits
at the beginning and end of the number, including combinations of adding digits to both ends.

For very short numbers (5 digits or less), it will suggest that the number might be a
Mushroom Observer observation number instead.

The script can also parse observation numbers directly from iNaturalist URLs.

Usage:
    python inat.finder.py <genus> <observation_number_or_url> [options]

Arguments:
    genus                   The genus name to match (e.g., "Galerina")
    observation_number_or_url  The potentially mistyped iNaturalist observation number
                               or a complete iNaturalist URL

Options:
    --digits N      Number of digits that might be wrong (default: 1)
    --verbose           Print detailed information about each attempt
    --no-progress       Hide the progress bar (progress bar is shown by default)
"""

import argparse
import requests
import time
import sys
from tqdm import tqdm
from datetime import timedelta
import textwrap

def parse_arguments():
    """Parse command line arguments."""
    # Create a formatted description from the module docstring
    description = textwrap.dedent(__doc__)
    
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter  # Use this to preserve formatting
    )
    
    """
    Parses command-line arguments for the iNaturalist observation finder.
    
    This function sets up an argument parser to accept a required genus name and a 
    potentially mistyped observation number (or URL). It also supports options to specify 
    the number of digits that might be incorrect (default: 1), enable verbose output, and 
    disable the progress bar. If no arguments are provided, the help message is printed and 
    the program exits.
    
    Returns:
        argparse.Namespace: An object with attributes corresponding to the parsed arguments.
    """
    parser.add_argument("genus", help="The genus name to match (e.g., 'Amanita')")
    parser.add_argument("observation_number", help="The potentially mistyped iNaturalist observation number or URL")
    parser.add_argument("--digits", type=int, default=1,
                        help="Number of digits that might be wrong (default: 1)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print detailed information about each attempt")
    parser.add_argument("--no-progress", action="store_true",
                        help="Hide the progress bar (progress bar is shown by default)")

    # If no arguments were provided, print help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    return parser.parse_args()

def generate_digit_variations(number_str, digits_off=1):
    """
    Generate variations of an observation number by altering specified digits.
    
    This function produces unique variations of the input observation number by replacing a given
    number of digits. For a single-digit change, it iterates over each digit position and substitutes
    it with every other possible digit. For multiple-digit changes, it recursively generates all combinations.
    If the 'digits_off' parameter is non-positive, the function returns the original number.
     
    Args:
        number_str (str): The original observation number.
        digits_off (int, optional): The number of digits to change. Defaults to 1.
     
    Returns:
        List[str]: A list of unique observation number variations.
    """
    if digits_off <= 0:
        return [number_str]  # No variations if digits_off is 0 or negative

    # For single digit variation, use direct approach
    if digits_off == 1:
        variations = []
        for pos in range(len(number_str)):
            for digit in range(10):
                if int(number_str[pos]) == digit:
                    continue  # Skip if it's the same digit

                # Replace digit at position pos
                new_number = number_str[:pos] + str(digit) + number_str[pos+1:]
                variations.append(new_number)
        return variations

    # For multiple digits off, use recursive approach
    variations = []
    for pos in range(len(number_str)):
        for digit in range(10):
            if int(number_str[pos]) == digit:
                continue  # Skip if it's the same digit

            # Replace digit at position pos
            new_number = number_str[:pos] + str(digit) + number_str[pos+1:]

            # Recursively generate variations with one fewer digit off
            sub_variations = generate_digit_variations(new_number, digits_off - 1)
            variations.extend(sub_variations)

    # Remove duplicates that might occur in recursive generation
    return list(set(variations))

def generate_digit_additions(number_str, max_added_digits=2):
    """
    Generate observation number variations by appending leading and/or trailing digits.
    
    This function returns all combinations where digit sequences (of length 1 up to max_added_digits)
    are added as a prefix, a suffix, or both to the original number string.
    
    Args:
        number_str: The original observation number as a string.
        max_added_digits: Maximum number of digits to add (default is 2).
    
    Returns:
        A list of strings containing all generated variations.
    """
    variations = []

    # Add single digits at the beginning only (0-9)
    for digit in range(10):
        variations.append(str(digit) + number_str)
        
    # Add single digits at the end only (0-9)
    for digit in range(10):
        variations.append(number_str + str(digit))
        
    # If max_added_digits is 2, add two digits
    if max_added_digits >= 2:
        # Add two digits at the beginning only
        for first_digit in range(10):
            for second_digit in range(10):
                variations.append(str(first_digit) + str(second_digit) + number_str)
                
        # Add two digits at the end only
        for first_digit in range(10):
            for second_digit in range(10):
                variations.append(number_str + str(first_digit) + str(second_digit))
                
        # Add one digit at beginning and one at end
        for prefix in range(10):
            for suffix in range(10):
                variations.append(str(prefix) + number_str + str(suffix))
    
    return variations

def parse_inat_url(url_or_number):
    """
    Extracts the observation number from an iNaturalist URL.
    
    If the input is a URL containing an observation number in the expected format,
    the function extracts and returns that observation number as a string.
    If the input does not match the URL pattern or is already an observation number,
    the original string is returned unchanged.
    
    Args:
        url_or_number: A string representing either an iNaturalist URL or an observation number.
    
    Returns:
        A string containing the extracted observation number, or the original input if no valid
        observation number is found.
    """
    import re

    # Check if it's a URL
    if url_or_number.startswith(('http://', 'https://')):
        # Use regex to extract the observation number from the URL
        match = re.search(r'observations/(\d+)', url_or_number)
        if match:
            return match.group(1)
        else:
            # If no match found, return the original string
            return url_or_number
    else:
        # If it's not a URL, assume it's already an observation number
        return url_or_number

def batch_check_observations(variations, batch_size=30):
    """
    Check multiple observation IDs by querying the iNaturalist API in batches.
    
    This function divides the provided observation ID variations into smaller batches to limit the number of API calls. For each batch, it concatenates the IDs into a comma-separated string and sends a GET request to the iNaturalist observations endpoint. The JSON response is parsed to extract observation details, which are aggregated into a list. If a network error occurs during a batch request, an error message is printed and processing continues with the next batch. A one-second delay is applied after each API call to comply with rate limiting.
    
    Args:
        variations: A list of observation ID strings to be verified.
        batch_size: Maximum number of observation IDs to include in each API request (default is 30).
    
    Returns:
        A list of observation data dictionaries obtained from the API responses.
    """
    all_results = []

    # Process in batches
    for i in range(0, len(variations), batch_size):
        batch = variations[i:i+batch_size]
        ids_string = ",".join(batch)

        url = f"https://api.inaturalist.org/v1/observations?id={ids_string}&per_page={batch_size}"

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()

            # Extract observations from the batch
            all_results.extend(data.get('results', []))

        except requests.RequestException as e:
            print(f"Error fetching batch: {e}")

        # Rate limiting - no more than 1 request per second
        time.sleep(1)

    return all_results

def check_observation_genus(observation, target_genus):
    """
    Determine if an observation belongs to the specified genus.
    
    This function inspects the taxonomic data within an observation to verify whether
    its taxon or any of its ancestors is classified as the given genus. It performs a
    case-insensitive match by comparing the target genus against the 'name' field of
    the genus-level taxon or the first part of a multi-word taxon name.
    
    Args:
        observation: A dictionary containing observation details with taxonomic information.
        target_genus: The genus name to match (case-insensitive).
    
    Returns:
        True if the observation's taxonomy includes the target genus; otherwise, False.
    """
    if observation and 'taxon' in observation:
        taxon = observation['taxon']
        if taxon and 'ancestry' in taxon:
            # Check genus information from the taxonomy
            for ancestor in taxon['ancestors'] if 'ancestors' in taxon else []:
                if ancestor.get('rank') == 'genus' and ancestor.get('name', '').lower() == target_genus.lower():
                    return True

            # Alternative check from the taxon itself
            if taxon.get('rank') == 'genus' and taxon.get('name', '').lower() == target_genus.lower():
                return True

            # Check if the taxon name contains the genus (sometimes the full name has genus+species)
            name = taxon.get('name', '')
            if ' ' in name and name.split(' ')[0].lower() == target_genus.lower():
                return True

    return False

def main():
    """
    Executes the iNaturalist observation finder process.
    
    This function orchestrates the search for valid iNaturalist observations by:
    - Parsing command-line arguments and extracting an observation number from a URL or plain input.
    - Validating the observation number and warning the user if it appears too short (suggesting a possible Mushroom Observer observation).
    - Optionally confirming whether the original observation number already matches the target genus.
    - Generating possible variations by altering digits and appending additional ones for shorter numbers.
    - Checking these variations in batches via API calls and displaying progress.
    - Presenting a summary of potential matches and the overall search duration.
    
    Note: This function interacts with the user via input prompts and exits if critical validation fails.
    """
    args = parse_arguments()

    genus = args.genus
    obs_input = args.observation_number
    digits_off = args.digits
    verbose = args.verbose
    show_progress = not args.no_progress

    # Parse URL if provided
    obs_number = parse_inat_url(obs_input)

    if verbose:
        print(f"Input: {obs_input}")
        if obs_input != obs_number:
            print(f"Extracted observation number: {obs_number}")

    if not obs_number.isdigit():
        print("Error: Observation number must contain only digits")
        print("Input provided: " + obs_input)
        sys.exit(1)

    # Check for Mushroom Observer numbers (5 digits or less)
    if len(obs_number) <= 5:
        print(f"Note: The observation number {obs_number} is very short (5 digits or less).")
        print("This might be a Mushroom Observer observation rather than iNaturalist.")
        print(f"Consider checking: https://mushroomobserver.org/{obs_number}")

        user_input = input("Continue with iNaturalist search anyway? (y/n): ").strip().lower()
        if user_input != 'y':
            print("Exiting search.")
            return

    # First, check if the original observation number is correct
    if verbose:
        print(f"Checking if original observation number {obs_number} matches genus '{genus}'...")

    # Make a single API call to check the original number
    original_check = batch_check_observations([obs_number], 1)
    if original_check and check_observation_genus(original_check[0], genus):
        print(f"✓ Good news! The original observation number {obs_number} already matches genus {genus}.")
        print(f"  Taxon: {original_check[0].get('taxon', {}).get('name', 'Unknown taxon')}")
        print(f"  URL: https://www.inaturalist.org/observations/{obs_number}")

        user_input = input("Continue searching for other potential matches? (y/n): ").strip().lower()
        if user_input != 'y':
            print("Exiting search.")
            return

    print(f"Looking for iNaturalist observations with genus '{genus}' that might be {digits_off} digit(s) off from '{obs_number}'")

    # Generate all possible variations with specified digits changed
    variations = generate_digit_variations(obs_number, digits_off)

    # If the observation number has fewer than 9 digits, try adding digits
    additional_variations = []
    if len(obs_number) < 9:
        print("Observation number has fewer than 9 digits. Will also try adding digits...")
        additional_variations = generate_digit_additions(obs_number, 2)
        print(f"Generated {len(additional_variations)} additional variations by adding digits")
        variations.extend(additional_variations)

    total_variations = len(variations)
    print(f"Generated {total_variations} total possible variations to check")

    # Set up progress bar if requested
    pbar = None
    start_time = time.time()
    time_per_batch = None

    if show_progress:
        pbar = tqdm(total=total_variations, desc="Checking variations", unit="var")

    # Process in batches to minimize API calls
    batch_size = 30  # iNaturalist API typically limits batch size
    matches = []

    for i in range(0, len(variations), batch_size):
        batch = variations[i:i+batch_size]

        if verbose:
            print(f"\nChecking batch of {len(batch)} variations ({i+1}-{min(i+batch_size, total_variations)} of {total_variations})")
            print(f"Variations in this batch: {', '.join(batch)}")

        batch_start_time = time.time()
        results = batch_check_observations(batch, batch_size)
        batch_end_time = time.time()

        # Calculate time per batch after the first batch
        if i == 0:
            time_per_batch = batch_end_time - batch_start_time
            if show_progress:
                # Update ETA based on first batch timing
                total_expected_time = time_per_batch * (total_variations / batch_size)
                pbar.set_postfix({"ETA": str(timedelta(seconds=int(total_expected_time)))})

        for obs in results:
            obs_id = obs.get('id')
            if check_observation_genus(obs, genus):
                matches.append(obs)
                if verbose:
                    print(f"✓ Match found: Observation {obs_id} has genus {genus}")
                    if 'taxon' in obs and 'name' in obs['taxon']:
                        print(f"  Taxon: {obs['taxon']['name']}")
            elif verbose:
                print(f"✗ Observation {obs_id} does not match genus {genus}")

        # Update progress bar
        if show_progress:
            pbar.update(len(batch))

    if show_progress:
        pbar.close()

    # Print results
    print("\nSearch complete!")
    if matches:
        print(f"\nFound {len(matches)} potential matches:")
        for i, match in enumerate(matches, 1):
            obs_id = match.get('id')
            taxon_name = match.get('taxon', {}).get('name', 'Unknown taxon')
            print(f"{i}. Observation #{obs_id} - {taxon_name}")
            print(f"   URL: https://www.inaturalist.org/observations/{obs_id}")
    else:
        print("\nNo matches found. Consider these possibilities:")
        print("1. The observation may have more than one digit mistyped")
        print("2. The genus name might be incorrect")
        print("3. The observation might not exist or has been removed")
        if len(obs_number) <= 5:
            print("4. This might be a Mushroom Observer number: https://mushroomobserver.org/" + obs_number)

    print(f"\nTotal time: {timedelta(seconds=int(time.time() - start_time))}")

if __name__ == "__main__":
    main()
