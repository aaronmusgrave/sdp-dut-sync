#!/usr/bin/env python3
"""
Cleanup orphaned tickets: Delete tickets from Dust that no longer exist in ServiceDesk
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
                print(f"  Fetched {len(ticket_ids)} ticket IDs...")
                has_more = result.get('list_info', {}).get('has_more_rows', False)
                if not has_more:
                    break
                start_index += 100
                time.sleep(0.3)
            else:
                break
        except Exception as e:
            print(f"Error: {e}")
            break
    
    print(f"✓ Total ticket IDs in ServiceDesk: {len(ticket_ids)}")
    return ticket_ids

def get_all_dust_documents():
    """Get all document IDs from Dust"""
    print("\n📋 Fetching all documents from Dust...")
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    
    try:
        response = requests.get(f"{DUST_API_BASE}", headers=headers, params={'limit': 10000}, timeout=60)
        if response.status_code == 200:
            docs = response.json().get('documents', [])
            doc_ids = [doc['document_id'] for doc in docs]
            print(f"✓ Found {len(doc_ids)} documents in Dust")
            return doc_ids
    except Exception as e:
        print(f"Error: {e}")
    return []

def delete_from_dust(document_id):
    """Delete document from Dust"""
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    try:
        response = requests.delete(f"{DUST_API_BASE}/{document_id}", headers=headers, timeout=30)
        return response.status_code in [200, 204]
    except:
        return False

def main():
    print("=" * 60)
    print("🧹 CLEANUP: Remove Orphaned Tickets from Dust")
    print("=" * 60)
    
    # Get all valid ticket IDs from ServiceDesk
    access_token = get_access_token()
    valid_ticket_ids = fetch_all_servicedesk_ticket_ids(access_token)
    
    # Get all documents from Dust
    dust_doc_ids = get_all_dust_documents()
    
    if not dust_doc_ids:
        print("\n⚠️  Could not fetch Dust documents - aborting")
        return
    
    # Find orphans
    valid_doc_ids = {f"ticket-{tid}" for tid in valid_ticket_ids}
    orphaned = [doc for doc in dust_doc_ids if doc not in valid_doc_ids and doc.startswith('ticket-')]
    
    if not orphaned:
        print("\n✅ No orphaned tickets found - Dust is in sync!")
        return
    
    print(f"\n⚠️  Found {len(orphaned)} orphaned tickets in Dust")
    print(f"Examples: {', '.join(orphaned[:5])}")
    print("\nThese tickets exist in Dust but NOT in ServiceDesk")
    
    confirm = input("\nDelete these orphaned tickets? (yes/no): ")
    if confirm.lower() != 'yes':
        print("Aborted - no tickets deleted")
        return
    
    # Delete orphans
    print(f"\n🗑️  Deleting {len(orphaned)} orphaned tickets...")
    deleted = 0
    failed = 0
    
    for idx, doc_id in enumerate(orphaned, 1):
        if idx % 50 == 0:
            print(f"  [{idx}/{len(orphaned)}] Deleting...")
        if delete_from_dust(doc_id):
            deleted += 1
        else:
            failed += 1
        time.sleep(0.1)
    
    print(f"\n✅ Cleanup Complete!")
    print(f"  Deleted: {deleted}")
    print(f"  Failed: {failed}")

if __name__ == "__main__":
    main()
