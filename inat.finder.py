#!/usr/bin/env python3
"""
iNaturalist Observation Finder

By Alan Rockefeller - March 28, 2025

This script helps find the correct iNaturalist observation number when there are mistyped digits.
It works by systematically changing digits of the provided observation number and checking if
any of those variations match the specified genus in the iNaturalist database.

Since you probably are using this code because you have a DNA barcode which does not go to the 
correct iNaturalist observation (for example it shows a plant or a bird), you probably know the 
genus.  

Usage:
    python inat.finder.py <genus> <observation_number> [options]

Arguments:
    genus               The genus name to match (e.g., "Galerina")
    observation_number  The potentially mistyped iNaturalist observation number

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
    parser.add_argument("observation_number", help="The potentially mistyped iNaturalist observation number")
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
    obs_number = args.observation_number
    digits_off = args.digits
    verbose = args.verbose
    show_progress = not args.no_progress
    
    if not obs_number.isdigit():
        print("Error: Observation number must contain only digits")
        sys.exit(1)
    
    # First, check if the original observation number is correct
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
    total_variations = len(variations)
    
    print(f"Generated {total_variations} possible variations to check")
    
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
    
    print(f"\nTotal time: {timedelta(seconds=int(time.time() - start_time))}")

if __name__ == "__main__":
    main()
