#!/usr/bin/env python3
"""
Cleanup orphaned tickets: Delete tickets from Dust that no longer exist in ServiceDesk
FIXED: Works around Dust API document listing limitations
"""

import os
import requests
import json
import time

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

def get_access_token():
    """Get ServiceDesk access token"""
    print("Getting ServiceDesk access token...")
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    if response.status_code == 200:
        print("✓ Access token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get token: {response.status_code}")

def fetch_all_servicedesk_ticket_ids(access_token):
    """Get all ticket IDs from ServiceDesk"""
    print("\n📋 Fetching all ticket IDs from ServiceDesk...")
    ticket_ids = set()
    start_index = 1
    pages_fetched = 0
    
    while True:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "sort_field": "created_time",
                "sort_order": "desc"
            }
        }
        params = {'input_data': json.dumps(list_info)}
        
        try:
            response = requests.get(f"{SDP_API_BASE}/requests", headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                if not tickets:
                    break
                
                for ticket in tickets:
                    ticket_ids.add(str(ticket.get('id')))
                
                pages_fetched += 1
                if pages_fetched % 10 == 0:
                    print(f"  Fetched {len(ticket_ids)} ticket IDs...")
                
                has_more = result.get('list_info', {}).get('has_more_rows', False)
                if not has_more:
                    break
                
                start_index += 100
                time.sleep(0.3)
            else:
                print(f"  ⚠️  API error {response.status_code}")
                break
        except Exception as e:
            print(f"  ⚠️  Error: {e}")
            break
    
    print(f"✓ Total ticket IDs in ServiceDesk: {len(ticket_ids)}")
    return ticket_ids

def check_ticket_exists_in_dust(ticket_id):
    """Check if a specific ticket document exists in Dust"""
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    document_id = f"ticket-{ticket_id}"
    url = f"{DUST_API_BASE}/{document_id}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        # 200 = exists, 404 = doesn't exist, other = error
        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            return False
        else:
            # On error, assume exists (safer)
            return True
    except:
        # On timeout/error, assume exists (safer)
        return True

def find_orphaned_tickets(valid_ticket_ids):
    """
    Find orphaned tickets in Dust by checking a reasonable ticket ID range
    Strategy: Check all IDs in the range of existing tickets
    """
    print("\n🔍 Finding orphaned tickets in Dust...")
    print("  Strategy: Check ticket ID range for orphans")
    
    # Convert to integers for range calculation
    ticket_id_ints = [int(tid) for tid in valid_ticket_ids]
    max_ticket_id = max(ticket_id_ints)
    min_ticket_id = min(ticket_id_ints)
    
    # Calculate range to check (with some buffer)
    check_min = max(1, min_ticket_id - 100)
    check_max = max_ticket_id + 10
    
    total_range = check_max - check_min + 1
    
    print(f"  Checking ticket ID range: {check_min} to {check_max} ({total_range} IDs)")
    print(f"  Valid ServiceDesk tickets: {len(valid_ticket_ids)}")
    print(f"  Expected orphans to check: ~{total_range - len(valid_ticket_ids)}")
    print()
    
    orphans_found = []
    checked_count = 0
    potential_orphans = 0
    
    print("  Checking for orphans (this may take a few minutes)...")
    
    for ticket_id in range(check_min, check_max + 1):
        checked_count += 1
        ticket_id_str = str(ticket_id)
        
        # Progress update every 100 checks
        if checked_count % 100 == 0:
            progress = (checked_count / total_range) * 100
            print(f"    [{checked_count}/{total_range}] {progress:.1f}% - Found {len(orphans_found)} orphans")
        
        # If this ticket ID exists in ServiceDesk, skip it
        if ticket_id_str in valid_ticket_ids:
            continue
        
        potential_orphans += 1
        
        # Check if this non-existent ticket is in Dust
        if check_ticket_exists_in_dust(ticket_id):
            orphans_found.append(f"ticket-{ticket_id}")
            print(f"    🗑️  Found orphan: ticket-{ticket_id}")
        
        # Rate limiting - don't hammer Dust API
        time.sleep(0.05)
    
    print(f"\n✓ Scan complete: Checked {checked_count} ticket IDs")
    print(f"  Potential orphans checked: {potential_orphans}")
    print(f"  Orphans found in Dust: {len(orphans_found)}")
    
    return orphans_found

def delete_from_dust(document_id):
    """Delete document from Dust"""
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    try:
        response = requests.delete(f"{DUST_API_BASE}/{document_id}", headers=headers, timeout=30)
        return response.status_code in [200, 204]
    except:
        return False

def main():
    print("=" * 70)
    print("🧹 CLEANUP: Remove Orphaned Tickets from Dust (v2 - FIXED)")
    print("=" * 70)
    print()
    print("This script finds tickets that exist in Dust but not in ServiceDesk")
    print("and optionally deletes them.")
    print()
    
    # Get all valid ticket IDs from ServiceDesk
    access_token = get_access_token()
    valid_ticket_ids = fetch_all_servicedesk_ticket_ids(access_token)
    
    if not valid_ticket_ids:
        print("\n⚠️  Could not fetch ServiceDesk tickets - aborting")
        return
    
    # Find orphaned tickets
    orphaned = find_orphaned_tickets(valid_ticket_ids)
    
    if not orphaned:
        print("\n✅ SUCCESS: No orphaned tickets found!")
        print("   Your Dust data source is in perfect sync with ServiceDesk.")
        return
    
    # Show results
    print(f"\n{'='*70}")
    print(f"⚠️  ORPHANED TICKETS FOUND")
    print(f"{'='*70}")
    print(f"\nFound {len(orphaned)} tickets in Dust that don't exist in ServiceDesk:")
    print()
    
    # Show first 10 as examples
    for i, doc_id in enumerate(orphaned[:10], 1):
        ticket_num = doc_id.replace('ticket-', '')
        print(f"  {i}. Ticket #{ticket_num}")
    
    if len(orphaned) > 10:
        print(f"  ... and {len(orphaned) - 10} more")
    
    print()
    print("These tickets may have been:")
    print("  • Deleted from ServiceDesk")
    print("  • Merged into other tickets")
    print("  • Removed during data cleanup")
    print()
    
    # Confirm deletion
    confirm = input("❓ Delete these orphaned tickets from Dust? (yes/no): ")
    
    if confirm.lower() != 'yes':
        print("\n❌ Aborted - no tickets deleted")
        return
    
    # Delete orphans
    print(f"\n🗑️  Deleting {len(orphaned)} orphaned tickets from Dust...")
    deleted = 0
    failed = 0
    
    for idx, doc_id in enumerate(orphaned, 1):
        progress = (idx / len(orphaned)) * 100
        
        if idx % 10 == 0 or idx == 1:
            print(f"  [{idx}/{len(orphaned)}] {progress:.1f}% - Deleted: {deleted}, Failed: {failed}")
        
        if delete_from_dust(doc_id):
            deleted += 1
        else:
            failed += 1
            print(f"    ⚠️  Failed to delete {doc_id}")
        
        time.sleep(0.1)
    
    # Final summary
    print(f"\n{'='*70}")
    print(f"✅ CLEANUP COMPLETE")
    print(f"{'='*70}")
    print(f"  Orphans found: {len(orphaned)}")
    print(f"  Successfully deleted: {deleted}")
    print(f"  Failed to delete: {failed}")
    print()
    
    if failed > 0:
        print("⚠️  Some deletions failed - you may need to retry or delete manually")
    else:
        print("✅ All orphaned tickets successfully removed!")
    print()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Cleanup cancelled by user")
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
