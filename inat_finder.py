#!/usr/bin/env python3
"""
iNaturalist Observation Finder

Version 1.7 - By Alan Rockefeller - January 21, 2026

This script helps find the correct iNaturalist observation number when there are mistyped digits.
It works by systematically changing digits of the provided observation number and checking if
any of those variations match the specified genus or username in the iNaturalist database.

If the observation number has fewer than 9 digits, it will also try adding up to two digits
at the beginning and end of the number, including combinations of adding digits to both ends.

For very short numbers (5 digits or less), it will suggest that the number might be a
Mushroom Observer observation number instead.

The script can also parse observation numbers directly from iNaturalist URLs.

Usage:
    python inat_finder.py (--genus <genus> | --user <username> | --project <project>) <observation_number_or_url> [options]

Arguments:
    --genus <genus>         The genus name to match (e.g., "Galerina")
    --user <username>       The iNaturalist username to match (e.g., "alan_rockefeller")
    --project <project>     The iNaturalist project to search within (ID, slug, URL, or title)
    observation_number_or_url  The potentially mistyped iNaturalist observation number
                               or a complete iNaturalist URL

Options:
    --digits N          Number of digits that might be wrong (default: 1)
    --verbose           Print detailed information about each attempt
    --no-progress       Hide the progress bar (progress bar is shown by default)
"""

import argparse
import itertools
import re
import sys
import textwrap
import time
from datetime import timedelta
import requests
from tqdm import tqdm

def parse_arguments():
    """
    Parses command-line arguments for the iNaturalist observation finder.

    This function sets up an argument parser to accept either a genus name or a username, and a
    potentially mistyped observation number (or URL). It also supports options to specify
    the number of digits that might be incorrect (default: 1), enable verbose output, and
    disable the progress bar. If no arguments are provided, the help message is printed and
    the program exits.

    Returns:
        argparse.Namespace: An object with attributes corresponding to the parsed arguments.
    """
    # Create a formatted description from the module docstring
    description = textwrap.dedent(__doc__)

    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter  # Use this to preserve formatting
    )
    search_group = parser.add_argument_group('search criteria (one required)')
    group = search_group.add_mutually_exclusive_group(required=True)
    group.add_argument("--genus", help="The genus name to match (e.g., 'Amanita')")
    group.add_argument("--user", help="The iNaturalist username to match")
    group.add_argument("--project", help="The iNaturalist project to search within (ID, slug, URL, or title)")
    
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

    args = parser.parse_args()
    
    return args

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

    # For multiple digits off, use itertools.combinations and itertools.product
    variations_set = set()
    n = len(number_str)
    
    for indices in itertools.combinations(range(n), digits_off):
        # For each combination of positions, generate all possible replacement digits
        # for those positions
        for new_digits_tuple in itertools.product(range(10), repeat=digits_off):
            temp_list = list(number_str)
            valid_replacement = True
            for i, index_to_change in enumerate(indices):
                # Ensure the new digit is different from the original digit at this position
                if int(number_str[index_to_change]) == new_digits_tuple[i]:
                    valid_replacement = False
                    break
                temp_list[index_to_change] = str(new_digits_tuple[i])
            
            if valid_replacement:
                variations_set.add("".join(temp_list))
                
    return list(variations_set)

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
    variations.extend([str(digit) + number_str for digit in range(10)])

    # Add single digits at the end only (0-9)
    variations.extend([number_str + str(digit) for digit in range(10)])

    # If max_added_digits is 2, add two digits
    if max_added_digits >= 2:
        # Add two digits at the beginning only
        for d1, d2 in itertools.product(range(10), repeat=2):
            variations.append(str(d1) + str(d2) + number_str)

        # Add two digits at the end only
        for d1, d2 in itertools.product(range(10), repeat=2):
            variations.append(number_str + str(d1) + str(d2))

        # Add one digit at beginning and one at end
        for p, s in itertools.product(range(10), repeat=2):
            variations.append(str(p) + number_str + str(s))
            
    return variations


def generate_digit_removals(number_str, max_removed_digits=2):
    """
    Generate observation number variations by removing digits.

    This function produces unique variations of the input observation number by removing a given
    number of digits from any position.

    Args:
        number_str (str): The original observation number.
        max_removed_digits (int, optional): The maximum number of digits to remove. Defaults to 2.

    Returns:
        List[str]: A list of unique observation number variations.
    """
    variations = set()
    n = len(number_str)
    
    if n == 0:
        return []

    # Determine how many digits to remove, from 1 to max_removed_digits
    for num_to_remove in range(1, min(max_removed_digits, n) + 1):
        # Find all combinations of indices to keep
        for indices_to_keep in itertools.combinations(range(n), n - num_to_remove):
            new_str = "".join(number_str[i] for i in indices_to_keep)
            if new_str: # Avoid adding empty strings if all digits are removed
                variations.add(new_str)
                
    return list(variations)


def verify_user_exists(username):
    """
    Verify if a username exists on iNaturalist.
    
    Makes an API call to check if the specified username exists in the iNaturalist database.
    
    Args:
        username: The username to verify.
        
    Returns:
        bool: True if the username exists, False otherwise.
    """
    try:
        url = f"https://api.inaturalist.org/v1/users/{username}"
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        # Check if any user matches the exact username
        if data.get('results'):
            for user in data['results']:
                if user.get('login', '').lower() == username.lower():
                    return True
        
        return False
    except requests.RequestException as e:
        if e.response is not None:
            print(f"Error verifying username: API request failed with status {e.response.status_code} - {e}. Check iNaturalist API status.")
        else:
            print(f"Error verifying username: Network error or invalid API endpoint - {e}. Check network connection.")
        return False

def verify_genus_exists(genus):
    """
    Verify if a genus exists in the iNaturalist taxonomy.
    
    Makes an API call to check if the specified genus exists in the iNaturalist taxonomy database.
    
    Args:
        genus: The genus name to verify.
        
    Returns:
        bool: True if the genus exists, False otherwise.
    """
    try:
        url = f"https://api.inaturalist.org/v1/taxa/autocomplete?q={genus}&rank=genus"
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        # Check if any taxon matches the exact genus name and is of rank genus
        if data.get('results'):
            for taxon in data['results']:
                if (
                    taxon.get('name', '').lower() == genus.lower() and 
                    taxon.get('rank', '') == 'genus'
                ):
                    return True
        
        return False
    except requests.RequestException as e:
        if e.response is not None:
            print(f"Error verifying genus: API request failed with status {e.response.status_code} - {e}. Check iNaturalist API status.")
        else:
            print(f"Error verifying genus: Network error or invalid API endpoint - {e}. Check network connection.")
        return False

def parse_project_slug_from_url(project_input):
    """
    Extracts the project slug from an iNaturalist project URL.
    
    Args:
        project_input: A string that might be a project URL.
        
    Returns:
        The extracted slug string if found, or None.
    """
    # Look for 'projects/' pattern only if the input looks URL-like or mentions iNaturalist
    if "projects/" in project_input and ("//" in project_input or "inaturalist.org" in project_input):
        match = re.search(r'projects/([^/?#]+)', project_input)
        if match:
            return match.group(1)
    return None

def search_projects_by_query(query):
    """
    Search for projects on iNaturalist by title or slug.
    
    Args:
        query: The search term (title or slug).
        
    Returns:
        A list of project dictionaries containing 'id', 'slug', and 'title'.
    """
    try:
        url = "https://api.inaturalist.org/v1/projects"
        params = {"q": query, "per_page": 10}
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        return response.json().get('results', [])
    except requests.RequestException as e:
        print(f"Error searching projects: {e}")
        return []

def resolve_project_identifier(project_input):
    """
    Resolves a project input string to a valid project ID/slug and metadata.
    
    Args:
        project_input: The input string (ID, slug, URL, or title).
        
    Returns:
        tuple: (project_id_or_slug, project_metadata_dict)
        
    Exits the program if ambiguous or not found.
    """
    # 1. Check if it's a numeric ID first
    if project_input.isdigit():
        try:
            url = f"https://api.inaturalist.org/v1/projects/{project_input}"
            response = requests.get(url, timeout=20)
            if response.status_code == 200:
                results = response.json().get('results', [])
                if results:
                    return project_input, results[0]
            
            # If we are here, the ID was not found or the request failed
            print(f"Error: Project ID '{project_input}' not found on iNaturalist.")
            sys.exit(1)
            
        except requests.RequestException as e:
            print(f"Error verifying project ID: {e}")
            sys.exit(1)

    # 2. Check if it's a URL
    slug_from_url = parse_project_slug_from_url(project_input)
    if slug_from_url:
        # Verify the extracted slug
        candidates = search_projects_by_query(slug_from_url)
        for p in candidates:
            # Case-insensitive comparison
            if p.get('slug', '').lower() == slug_from_url.lower():
                return str(p.get("id", slug_from_url)), p
        print(f"Error: Project URL slug '{slug_from_url}' not found.")
        sys.exit(1)

    # 3. Determine if it's likely a title or a slug
    # Conservative slug detection:
    # - If contains spaces -> Title
    # - If all digits -> ID (handled above)
    # - Else -> Treat as Slug candidate, but verify exactly. 
    #   If verification fails, fallback to title search.
    
    is_likely_slug = " " not in project_input
    
    candidates = search_projects_by_query(project_input)
    
    if not candidates:
        print(f"Error: Project '{project_input}' not found on iNaturalist.")
        sys.exit(1)

    # Try to find exact match
    project_input_lower = project_input.lower()
    exact_matches = [
        p
        for p in candidates
        if (is_likely_slug and p.get("slug", "").lower() == project_input_lower)
        or p.get("title", "").lower() == project_input_lower
    ]

    if len(exact_matches) == 1:
        p = exact_matches[0]
        # Prefer ID if available, else slug
        limit_param = str(p.get('id', p.get('slug')))
        return limit_param, p
        
    if len(exact_matches) > 1:
        # This shouldn't happen often for slugs, maybe for titles
        print(f"Found multiple exact matches for '{project_input}':")
        for p in exact_matches:
            print(f" - {p.get('title')} (ID: {p.get('id')}, Slug: {p.get('slug')})")
        print("Please use the specific ID or Slug.")
        sys.exit(1)
        
    # If no exact match, but we have candidates, show disambiguation
    print(f"No exact match found for '{project_input}', but found similar projects:")
    for p in candidates[:5]:
        print(f" - {p.get('title')} (ID: {p.get('id')}, Slug: {p.get('slug')})")
    print("\nPlease re-run with the specific Project ID or Slug.")
    sys.exit(1)

def unique_preserve_order(seq):
    """
    Deduplicates a list while preserving the original order.
    """
    seen = set()
    result = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def preprocess_argv_for_project_name(argv):
    """
    Pre-processes sys.argv to handle unquoted project names.
    
    Example: --project Coastal and Marine Mycology 2024 123456
    Becomes: --project "Coastal and Marine Mycology 2024" 123456
    
    Args:
        argv: List of command line arguments (usually sys.argv).
        
    Returns:
        Modified list of arguments.
    """
    if "--project" not in argv:
        return argv
        
    new_argv = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        new_argv.append(arg)
        i += 1
        
        if arg == "--project":
            # Start collecting tokens until we hit something that looks like an observation
            # or another flag
            project_tokens = []
            while i < len(argv):
                next_arg = argv[i]
                
                # Stop if it's the observation number/URL
                # Rule: contains "observations/" OR (digits >= 6)
                is_obs = "observations/" in next_arg or (next_arg.isdigit() and len(next_arg) >= 6)
                
                # Stop if it's a new flag
                is_flag = next_arg.startswith("-")
                
                if is_obs or is_flag:
                    break
                    
                project_tokens.append(next_arg)
                i += 1
            
            # If we collected multiple tokens, join them. 
            # If just one, it might be quoted or just a slug, effectively same result.
            if project_tokens:
                new_argv.append(" ".join(project_tokens))
    
    return new_argv

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

def batch_check_observations(variations, batch_size=200, project_id=None):
    """
    Check multiple observation IDs by querying the iNaturalist API in batches.

    This function divides the provided observation ID variations into smaller batches to limit the number of API calls. 
    For each batch, it concatenates the IDs into a comma-separated string and sends a GET request to the iNaturalist 
    observations endpoint. The JSON response is parsed to extract observation details, which are aggregated into a list. 
    If a network error occurs during a batch request, an error message is printed and processing continues with the next batch. 
    A one-second delay is applied after each API call to comply with rate limiting.

    Args:
        variations: A list of observation ID strings to be verified.
        batch_size: Maximum number of observation IDs to include in each API request (default is 200).
        project_id: Optional project ID or slug to filter by.

    Returns:
        A list of observation data dictionaries obtained from the API responses.
    """
    all_results = []

    # Process in batches
    for i in range(0, len(variations), batch_size):
        batch = variations[i:i+batch_size]
        ids_string = ",".join(batch)

        url = "https://api.inaturalist.org/v1/observations"
        params = {
            "id": ids_string,
            "per_page": batch_size
        }
        if project_id:
            params["project_id"] = project_id

        try:
            response = requests.get(url, params=params, timeout=20)
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
            for ancestor in taxon.get('ancestors', []):
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

def check_observation_user(observation, target_username):
    """
    Determine if an observation was created by the specified username.

    This function checks if the user who created the observation matches the
    provided username. It performs a case-insensitive comparison between the 
    target username and the observation's user login name.

    Args:
        observation: A dictionary containing observation details including user information.
        target_username: The username to match (case-insensitive).

    Returns:
        True if the observation was created by the target user; otherwise, False.
    """
    if observation and 'user' in observation:
        user = observation['user']
        if user and 'login' in user and user['login'].lower() == target_username.lower():
            return True
    
    return False

def main():
    """
    Executes the iNaturalist observation finder process.

    This function orchestrates the search for valid iNaturalist observations by:
    - Parsing command-line arguments and extracting an observation number from a URL or plain input.
    - Verifying that the specified genus, username, or project exists in iNaturalist.
    - Validating the observation number and warning the user if it appears too short (suggesting a possible Mushroom Observer observation).
    - Optionally confirming whether the original observation number already matches the criteria.
    - Generating possible variations by altering digits and appending additional ones for shorter numbers.
    - Checking these variations in batches via API calls and displaying progress.
    - Presenting a summary of potential matches and the overall search duration.

    Note: This function interacts with the user via input prompts and exits if critical validation fails.
    """
    # Pre-process sys.argv to handle unquoted project names
    sys.argv = preprocess_argv_for_project_name(sys.argv)
    
    batch_size = 200  # Define batch size as a constant

    args = parse_arguments()

    genus = args.genus
    username = args.user
    project_input = args.project
    obs_input = args.observation_number
    digits_off = args.digits
    verbose = args.verbose
    show_progress = not args.no_progress

    # Determine search mode
    if genus:
        search_mode = "genus"
        search_term = genus
    elif username:
        search_mode = "user"
        search_term = username
    else:
        search_mode = "project"
        search_term = project_input

    project_id_param = None
    project_metadata = None

    # Verify that the genus, user, or project exists before proceeding
    print(f"Verifying {search_mode} '{search_term}' exists on iNaturalist...")
    
    if search_mode == "genus":
        if not verify_genus_exists(genus):
            print(f"Error: Genus '{genus}' not found in iNaturalist taxonomy.")
            print("Please check the spelling or try a different genus name.")
            sys.exit(1)
        print(f"✓ Genus '{genus}' verified in iNaturalist taxonomy.")
        
    elif search_mode == "user":
        if not verify_user_exists(username):
            print(f"Error: Username '{username}' not found on iNaturalist.")
            print("Please check the spelling or try a different username.")
            sys.exit(1)
        print(f"✓ Username '{username}' verified on iNaturalist.")
        
    elif search_mode == "project":
        project_id_param, project_metadata = resolve_project_identifier(project_input)
        title = project_metadata.get('title', 'Unknown Project')
        pid = project_metadata.get('id')
        slug = project_metadata.get('slug')
        print(f"✓ Project verified: {title} (ID: {pid}, Slug: {slug})")
        if slug:
            print(f"  Project URL: https://www.inaturalist.org/projects/{slug}")

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
        print(f"Checking if original observation number {obs_number} matches {search_mode} '{search_term}'...")

    # Make a single API call to check the original number
    original_check = batch_check_observations([obs_number], 1, project_id=project_id_param)
    
    # In project mode, if we get results passing project_id param, they are matches.
    # In other modes, we need to check check_observation_* functions.
    
    if original_check:
        match_found = False
        obs = original_check[0]
        
        if search_mode == "project":
            match_found = True
            print(f"✓ Good news! The original observation number {obs_number} is in project '{project_metadata.get('title')}'.")
        elif search_mode == "genus" and check_observation_genus(obs, genus):
            match_found = True
            print(f"✓ Good news! The original observation number {obs_number} already matches genus {genus}.")
        elif search_mode == "user" and check_observation_user(obs, username):
            match_found = True
            print(f"✓ Good news! The original observation number {obs_number} was created by user {username}.")
        
        if match_found:
            print(f"  Taxon: {obs.get('taxon', {}).get('name', 'Unknown taxon')}")
            print(f"  Creator: {obs.get('user', {}).get('login', 'Unknown user')}")
            print(f"  URL: https://www.inaturalist.org/observations/{obs_number}")
            user_input = input("Continue searching for other potential matches? (y/n): ").strip().lower()
            if user_input != 'y':
                print("Exiting search.")
                return

    if search_mode == "genus":
        print(f"Looking for iNaturalist observations with genus '{genus}' that might be {digits_off} digit(s) off from '{obs_number}'")
    elif search_mode == "user":
        print(f"Looking for iNaturalist observations created by user '{username}' that might be {digits_off} digit(s) off from '{obs_number}'")
    else:
        print(f"Looking for iNaturalist observations in project '{project_metadata.get('title')}' that might be {digits_off} digit(s) off from '{obs_number}'")

    # Generate all possible variations with specified digits changed
    variations = generate_digit_variations(obs_number, digits_off)

    # If the observation number has fewer than 9 digits, try adding digits
    additional_variations = []
    if len(obs_number) < 9:
        print("Observation number has fewer than 9 digits. Will also try adding digits...")
        additional_variations = generate_digit_additions(obs_number, 2)
        print(f"Generated {len(additional_variations)} additional variations by adding digits")
        variations.extend(additional_variations)

    # If the observation number is long enough, try removing digits
    if len(obs_number) > 5:
        print("Observation number has more than 5 digits. Will also try removing up to 2 digits...")
        removal_variations = generate_digit_removals(obs_number, 2)
        print(f"Generated {len(removal_variations)} additional variations by removing digits")
        variations.extend(removal_variations)

    # Deduplicate variations while preserving order
    variations = unique_preserve_order(variations)
    total_variations = len(variations)
    print(f"Generated {total_variations} total unique variations to check")

    # Set up progress bar if requested
    pbar = None
    start_time = time.time()
    
    # For ETA calculation
    batch_times = []
    max_batch_times_to_average = 5 

    if show_progress:
        pbar = tqdm(total=total_variations, desc="Checking variations", unit="var")

    # Process in batches to minimize API calls
    matches = []

    for i in range(0, len(variations), batch_size):
        batch = variations[i:i+batch_size]
        
        if verbose:
            print(f"\nChecking batch of {len(batch)} variations ({i+1}-{min(i+batch_size, total_variations)} of {total_variations})")
            print(f"Variations in this batch: {', '.join(batch)}")

        batch_start_time = time.time()
        results = batch_check_observations(batch, batch_size, project_id=project_id_param)
        batch_end_time = time.time()

        current_batch_time = batch_end_time - batch_start_time
        batch_times.append(current_batch_time)
        if len(batch_times) > max_batch_times_to_average:
            batch_times.pop(0) # Keep only the last N times

        if show_progress and pbar:
            average_batch_time = sum(batch_times) / len(batch_times)
            if average_batch_time > 0: # Avoid division by zero if a batch was instant
                remaining_items = total_variations - pbar.n - len(batch) # pbar.n is updated after pbar.update()
                if remaining_items < 0:
                    remaining_items = 0 # Ensure non-negative
                
                remaining_batches = remaining_items / batch_size
                estimated_remaining_time = average_batch_time * remaining_batches
                pbar.set_postfix({"ETA": str(timedelta(seconds=int(estimated_remaining_time)))})

        for obs in results:
            obs_id = obs.get('id')
            match_found = False
            
            if search_mode == "project":
                # Server side filtering has already ensured membership
                match_found = True
                matches.append(obs)
                if verbose:
                    print(f"✓ Match found: Observation {obs_id} is in project")
            elif search_mode == "genus" and check_observation_genus(obs, genus):
                match_found = True
                matches.append(obs)
                if verbose:
                    print(f"✓ Match found: Observation {obs_id} has genus {genus}")
            elif search_mode == "user" and check_observation_user(obs, username):
                match_found = True
                matches.append(obs)
                if verbose:
                    print(f"✓ Match found: Observation {obs_id} was created by user {username}")
            
            taxon_name = obs.get("taxon", {}).get("name")
            if match_found and verbose and taxon_name:
                print(f"  Taxon: {taxon_name}")
                        
            if not match_found and verbose:
                if search_mode == "genus":
                    print(f"✗ Observation {obs_id} does not match genus {genus}")
                elif search_mode == "user":
                    print(f"✗ Observation {obs_id} was not created by user {username}")

        # Update progress bar
        if show_progress and pbar:
            pbar.update(len(batch))

    if show_progress and pbar:
        pbar.close()

    # Print results
    print("\nSearch complete!")
    if matches:
        print(f"\nFound {len(matches)} potential matches:")
        for i, match in enumerate(matches, 1):
            obs_id = match.get('id')
            taxon_name = match.get('taxon', {}).get('name', 'Unknown taxon')
            creator = match.get('user', {}).get('login', 'Unknown user')
            print(f"{i}. Observation #{obs_id} - {taxon_name}")
            print(f"   Created by: {creator}")
            print(f"   URL: https://www.inaturalist.org/observations/{obs_id}")
    else:
        print("\nNo matches found. Consider these possibilities:")
        print("1. The observation may have more than one digit mistyped")
        if search_mode == "genus":
            print("2. The genus name might be incorrect")
        elif search_mode == "user":
            print("2. The username might be incorrect")
        elif search_mode == "project":
            print("2. The project might be incorrect (try ID instead of slug/title)")
            
        print("3. The observation might not exist or has been removed")
        if len(obs_number) <= 5:
            print("4. This might be a Mushroom Observer number: https://mushroomobserver.org/" + obs_number)

    print(f"\nTotal time: {timedelta(seconds=int(time.time() - start_time))}")

if __name__ == "__main__":
    main()

