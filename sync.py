#!/usr/bin/env python3
"""
ServiceDesk Plus Ticket Sync
- Initial run: Full sync with rate limiting (75 tickets/minute max)
- Subsequent runs: Incremental sync of recent tickets
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time

# Configuration from environment variables
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk-tickets')

# Sync state file to track if initial sync is complete
SYNC_STATE_FILE = 'sync_state.json'

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Rate limiting
MAX_TICKETS_PER_MINUTE = 75
DELAY_BETWEEN_TICKETS = 60.0 / MAX_TICKETS_PER_MINUTE  # ~0.8 seconds

# Token cache
_access_token = None
_token_expiry = None

def get_access_token():
    """Get a valid access token using refresh token"""
    global _access_token, _token_expiry
    
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    print("Getting access token...")
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        data = response.json()
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def load_sync_state():
    """Load sync state from file"""
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, 'r') as f:
            return json.load(f)
    return {'initial_sync_complete': False, 'last_sync_time': None}

def save_sync_state(state):
    """Save sync state to file"""
    with open(SYNC_STATE_FILE, 'w') as f:
        json.dump(state, f)

def fetch_all_tickets_paginated(access_token):
    """Fetch all tickets with pagination"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    all_tickets = []
    start_index = 1
    page_size = 100  # Max per page
    
    while True:
        # Try without input_data wrapper - just direct params
        params = {
            'list_info': json.dumps({
                "row_count": page_size,
                "start_index": start_index
            })
        }
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params)
            print(f"Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"Response keys: {data.keys()}")
                tickets = data.get('requests', [])
                
                if not tickets:
                    break
                
                all_tickets.extend(tickets)
                print(f"Fetched {len(all_tickets)} tickets so far...")
                
                # Check if there are more pages
                list_info = data.get('list_info', {})
                has_more = list_info.get('has_more_rows', False)
                
                if not has_more:
                    break
                
                start_index += page_size
            else:
                print(f"Error fetching tickets: {response.status_code}")
                print(f"Response: {response.text}")
                
                # Try without any params as fallback
                if start_index == 1:
                    print("\nTrying without pagination params...")
                    response = requests.get(url, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        tickets = data.get('requests', [])
                        all_tickets.extend(tickets)
                        print(f"Fetched {len(tickets)} tickets without pagination")
                break
        except Exception as e:
            print(f"Exception fetching tickets: {e}")
            import traceback
            traceback.print_exc()
            break
    
    return all_tickets

def fetch_recent_tickets(access_token):
    """Fetch the most recent tickets for incremental sync"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    params = {
        'list_info': json.dumps({
            "row_count": 50
        })
    }
    url = f"{SDP_API_BASE}/requests"
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json().get('requests', [])
        else:
            # Try without params
            print("Trying without params...")
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                return response.json().get('requests', [])
            print(f"Error: {response.status_code} - {response.text}")
        return []
    except Exception as e:
        print(f"Exception: {e}")
        return []

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('request', {})
        return None
    except:
        return None

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch conversations for a ticket"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('conversations', [])
        return []
    except:
        return []

def upload_to_dust(ticket_data, ticket_id):
    """Upload a ticket to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{ticket_id}"
    
    payload = {
        "document_id": str(ticket_id),
        "text": json.dumps(ticket_data, indent=2),
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{ticket_id}",
        "timestamp": int(time.time())
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.status_code in [200, 201]
    except Exception as e:
        print(f"Error uploading ticket {ticket_id}: {e}")
        return False

def sync_ticket(ticket, access_token):
    """Sync a single ticket with full details and conversations"""
    ticket_id = ticket.get('id')
    
    # Fetch full details
    details = fetch_ticket_details(ticket_id, access_token)
    if not details:
        print(f"Could not fetch details for ticket {ticket_id}")
        return False
    
    # Fetch conversations
    conversations = fetch_ticket_conversations(ticket_id, access_token)
    
    # Combine all data
    full_ticket_data = {
        **details,
        'conversations': conversations
    }
    
    # Upload to Dust
    success = upload_to_dust(full_ticket_data, ticket_id)
    
    if success:
        print(f"✓ Synced ticket {ticket_id}")
    else:
        print(f"✗ Failed to sync ticket {ticket_id}")
    
    return success

def main():
    """Main sync logic"""
    print("Starting ServiceDesk Plus sync...")
    
    # Load sync state
    state = load_sync_state()
    access_token = get_access_token()
    
    if not state['initial_sync_complete']:
        print("\n=== INITIAL FULL SYNC ===")
        print("Fetching all tickets...")
        
        # Fetch all tickets
        all_tickets = fetch_all_tickets_paginated(access_token)
        total = len(all_tickets)
        print(f"Found {total} total tickets to sync")
        
        if total == 0:
            print("No tickets found. Check API permissions or try again.")
            return
        
        # Sync with rate limiting
        for i, ticket in enumerate(all_tickets, 1):
            print(f"Processing {i}/{total}...")
            
            # Refresh token if needed
            access_token = get_access_token()
            
            # Sync ticket
            sync_ticket(ticket, access_token)
            
            # Rate limiting: wait between tickets
            if i < total:  # Don't wait after the last ticket
                time.sleep(DELAY_BETWEEN_TICKETS)
        
        # Mark initial sync as complete
        state['initial_sync_complete'] = True
        state['last_sync_time'] = datetime.now().isoformat()
        save_sync_state(state)
        
        print("\n✓ Initial sync complete!")
    
    else:
        print("\n=== INCREMENTAL SYNC ===")
        print("Fetching recent tickets...")
        
        # Fetch recent tickets
        recent_tickets = fetch_recent_tickets(access_token)
        print(f"Found {len(recent_tickets)} recent tickets to check")
        
        # Sync recent tickets (no rate limiting needed for small batches)
        for ticket in recent_tickets:
            access_token = get_access_token()
            sync_ticket(ticket, access_token)
        
        # Update last sync time
        state['last_sync_time'] = datetime.now().isoformat()
        save_sync_state(state)
        
        print("\n✓ Incremental sync complete!")

if __name__ == "__main__":
    main()
