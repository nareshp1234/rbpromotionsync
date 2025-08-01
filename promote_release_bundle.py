import os
import requests
import json
import subprocess
import sys

def get_release_bundle_names_with_project_keys(source_url, access_token):
    """
    Gets list of release bundles with project key from /lifecycle/api/v2/release_bundle/names.
    """
    api_url = f"{source_url}/lifecycle/api/v2/release_bundle/names"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    print(f"::debug::Fetching release bundle names from: {api_url}")
    try:
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"::error::Failed to get release bundle names: {e}")
        return None

def get_promotion_history(url, access_token, repository_key, release_bundle, bundle_version, project_key):
    """
    Fetches the full, sorted promotion history for a release bundle version.
    """
    api_url = f"{url}/lifecycle/api/v2/audit/{release_bundle}/{bundle_version}?project={project_key}&repository_key={repository_key}"
    print(f"::debug::Querying audit trail: {api_url}")
    try:
        response = requests.get(api_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        if response.status_code == 404:
            return [] # No history exists, return an empty list
        response.raise_for_status()
        
        audit_data = response.json()
        promotions = []
        if audit_data and "audits" in audit_data:
            for event in audit_data["audits"]:
                if (event.get("subject_type") == "PROMOTION" and 
                    not event.get("subject_reference", "").startswith("FED-")):
                    promotions.append(event)
        
        promotions.sort(key=lambda x: x.get("context", {}).get("promotion_created_millis", 0))
        return promotions
    except Exception as e:
        print(f"::error::Failed to get promotion history from {url}: {e}")
        return None

# --- Function to update the timestamp ---
def update_release_bundle_milliseconds(target_url, access_token, release_bundle, bundle_version, promotion_created_millis, project_key="default"):
    """
    Updates release bundle with a specific timestamp for a promotion record.
    """
    api_url = f"{target_url}/lifecycle/api/v2/promotion/records/{release_bundle}/{bundle_version}?project={project_key}&operation=copy&promotion_created_millis={promotion_created_millis}"
    print(f"Attempting to update promotion record with API: {api_url}")
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"::error::API request failed to {api_url}: {e}")
        return None

def parse_repos_to_set(repo_list):
    """
    Converts a list of repositories into a frozenset for order-independent 
    and format-independent comparison. Handles comma-separated strings.
    """
    if not repo_list:
        return frozenset()
    
    parsed_set = set()
    for item in repo_list:
        parsed_set.update(repo.strip() for repo in item.split(','))
    return frozenset(parsed_set)


def main():
    source_access_token = os.getenv("SOURCE_ACCESS_TOKEN")
    target_access_token = os.getenv("TARGET_ACCESS_TOKEN")
    source_url = os.getenv("SOURCE_URL")
    target_url = os.getenv("TARGET_URL")
    release_bundle_name = os.getenv("RELEASE_BUNDLE")
    bundle_version = os.getenv("BUNDLE_VERSION")
    input_repository_key = os.getenv("REPOSITORY_KEY")

    if not all([source_access_token, target_access_token, source_url, target_url, release_bundle_name, bundle_version, input_repository_key]):
        print("::error::Missing one or more required environment variables.")
        sys.exit(1)

    print(f"--- Starting State Sync for {release_bundle_name}/{bundle_version} ---")

    print("\n--- Determining Project Key ---")
    project_key = "default"
    names_response = get_release_bundle_names_with_project_keys(source_url, source_access_token)
    if names_response and "release_bundles" in names_response:
        for rb_info in names_response["release_bundles"]:
            if rb_info.get("repository_key") == input_repository_key:
                project_key = rb_info.get("project_key", "default")
                print(f"::notice::Matched repository_key '{input_repository_key}' to project_key '{project_key}'.")
                break
    
    print("\n--- Fetching Promotion Histories ---")
    source_promotions = get_promotion_history(source_url, source_access_token, input_repository_key, release_bundle_name, bundle_version, project_key)
    target_promotions = get_promotion_history(target_url, target_access_token, input_repository_key, release_bundle_name, bundle_version, project_key)

    if source_promotions is None or target_promotions is None:
        print("::error::Could not fetch promotion histories from source or target. Aborting.")
        sys.exit(1)
        
    target_promotions_set = set()
    for promo in target_promotions:
        ctx = promo.get("context", {})
        promo_tuple = (
            ctx.get("environment"),
            parse_repos_to_set(ctx.get("included_repository_keys", [])),
            parse_repos_to_set(ctx.get("excluded_repository_keys", []))
        )
        target_promotions_set.add(promo_tuple)

    promotions_to_sync = []
    for promo in source_promotions:
        ctx = promo.get("context", {})
        promo_tuple = (
            ctx.get("environment"),
            parse_repos_to_set(ctx.get("included_repository_keys", [])),
            parse_repos_to_set(ctx.get("excluded_repository_keys", []))
        )
        if promo_tuple not in target_promotions_set:
            promotions_to_sync.append(promo)

    if not promotions_to_sync:
        print("\n✅ Target is already in sync. No action needed.")
        sys.exit(0)

    print(f"\n--- Found {len(promotions_to_sync)} Missing Promotion(s) to Sync ---")
    for promo_event in promotions_to_sync:
        context = promo_event.get("context", {})
        environment = context.get("environment")
        included_repos = context.get("included_repository_keys", [])
        excluded_repos = context.get("excluded_repository_keys", [])
        # Get the original timestamp for this specific event
        original_promotion_millis = context.get("promotion_created_millis")
        
        print(f"\nSyncing promotion to '{environment}'...")

        include_param = f"--include-repos={','.join(included_repos)}" if included_repos else ""
        exclude_param = f"--exclude-repos={','.join(excluded_repos)}" if excluded_repos else ""
        
        jf_command = ["jf", "rbp", release_bundle_name, bundle_version, environment, f"--project={project_key}"]
        if include_param: jf_command.append(include_param)
        if exclude_param: jf_command.append(exclude_param)
        
        print(f"Executing command: {' '.join(jf_command)}")
        try:
            subprocess.run(jf_command, check=True, capture_output=True, text=True)
            print(f"::notice::Successfully synced promotion to '{environment}'.")

            # --- Call to update the timestamp after successful promotion ---
            if original_promotion_millis:
                print(f"NOTICE: Updating timestamp for promotion to {environment}...")
                try:
                    # Add +1 millisecond to the original timestamp before sending the update
                    updated_millis = int(original_promotion_millis) + 1
                except (ValueError, TypeError):
                    print(f"WARNING: original_promotion_millis '{original_promotion_millis}' is not a valid number. Cannot increment.")
                    updated_millis = original_promotion_millis
                
                update_response = update_release_bundle_milliseconds(
                    target_url,
                    target_access_token,
                    release_bundle_name,
                    bundle_version,
                    updated_millis,
                    project_key
                )
                if update_response is None:
                    print(f"ERROR: Failed to update timestamp for promotion to {environment}.")
                else:
                    print(f"SUCCESS: Timestamp updated for {environment}.")
            else:
                print(f"WARNING: Skipping timestamp update for {environment}: Original timestamp not available.")

        except subprocess.CalledProcessError as e:
            print(f"::error::Failed to sync promotion to '{environment}': {e.stderr}")
            sys.exit(1)

    print("\n--- Synchronization Complete ---")

if __name__ == "__main__":
    main()
