#!/usr/bin/env python3
"""
iNaturalist Observation Finder

Version 1.1 - By Alan Rockefeller - March 29, 2025

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

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
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
    Generate all possible variations of the number with specified number of digits changed.

    Args:
        number_str: The original observation number as a string
        digits: How many digits might be wrong (1 means one digit is wrong)

    Returns:
        List of all possible variations
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
    Generate variations by adding up to max_added_digits at the beginning and end.
    Also handles combinations of adding to both beginning and end simultaneously.
    
    Args:
        number_str: The original observation number as a string
        max_added_digits: Maximum number of digits to add (1 or 2)
        
    Returns:
        List of all possible variations with added digits
    """
    variations = []
    
    # Add digits at the beginning only
    for prefix_len in range(1, max_added_digits + 1):
        for prefix in range(10**(prefix_len-1), 10**prefix_len):
            variations.append(str(prefix) + number_str)
    
    # Add digits at the end only
    for suffix_len in range(1, max_added_digits + 1):
        for suffix in range(10**(suffix_len-1), 10**suffix_len):
            variations.append(number_str + str(suffix))
    
    # Add digits at both beginning and end (for missing digits on both sides)
    for prefix_len in range(1, max_added_digits + 1):
        for suffix_len in range(1, max_added_digits + 1):
            for prefix in range(10**(prefix_len-1), 10**prefix_len):
                for suffix in range(10**(suffix_len-1), 10**suffix_len):
                    variations.append(str(prefix) + number_str + str(suffix))
    
    return variations

def parse_inat_url(url_or_number):
    """
    Extract the observation number from an iNaturalist URL or return the number if it's already a number.
    
    Args:
        url_or_number: Either an iNaturalist URL or an observation number
        
    Returns:
        The extracted observation number as a string
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
    """Check multiple observation IDs in batches to minimize API calls."""
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
    """Check if an observation has the target genus."""
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
        print(f"This might be a Mushroom Observer observation rather than iNaturalist.")
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
        print(f"Observation number has fewer than 9 digits. Will also try adding digits...")
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
