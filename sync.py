#!/usr/bin/env python3
"""
Full Sync - Initial import of all tickets
Handles pagination to fetch all tickets from ServiceDesk Plus
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

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Pagination settings
TICKETS_PER_PAGE = 100
MAX_TICKETS = 5000

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

def fetch_all_tickets():
    """Fetch all tickets with pagination using start_index"""
    access_token = get_access_token()
    all_tickets = []
    seen_ticket_ids = set()
    start_index = 1
    
    print(f"Fetching all tickets with pagination (batches of {TICKETS_PER_PAGE})...")
    
    while len(all_tickets) < MAX_TICKETS:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": TICKETS_PER_PAGE,
                "start_index": start_index
            }
        }
        
        params = {
            'input_data': json.dumps(list_info)
        }
        
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                if not tickets:
                    print(f"  No more tickets. Total unique: {len(all_tickets)}")
                    break
                
                new_tickets = []
                duplicate_count = 0
                ticket_ids_in_batch = []
                
                for ticket in tickets:
                    ticket_id = ticket.get('id')
                    ticket_ids_in_batch.append(ticket_id)
                    if ticket_id not in seen_ticket_ids:
                        new_tickets.append(ticket)
                        seen_ticket_ids.add(ticket_id)
                    else:
                        duplicate_count += 1
                
                if ticket_ids_in_batch:
                    first_id = ticket_ids_in_batch[0]
                    last_id = ticket_ids_in_batch[-1]
                    print(f"  Batch at index {start_index}: got {len(tickets)} tickets (IDs: {first_id} to {last_id})")
                    print(f"    New: {len(new_tickets)}, Duplicates: {duplicate_count}, Total unique: {len(all_tickets) + len(new_tickets)}")
                
                all_tickets.extend(new_tickets)
                
                response_list_info = result.get('list_info', {})
                has_more = response_list_info.get('has_more_rows', False)
                
                if not has_more:
                    print(f"  API says no more rows. Total unique: {len(all_tickets)}")
                    break
                
                if len(new_tickets) == 0:
                    print(f"  No new tickets found. Total unique: {len(all_tickets)}")
                    break
                
                start_index += TICKETS_PER_PAGE
                time.sleep(0.5)
                
            else:
                print(f"  Failed: {response.status_code}")
                print(f"  Response: {response.text[:500]}")
                break
                
        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"\n  ✓ Fetched {len(all_tickets)} unique tickets")
    return all_tickets

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
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
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('conversations', [])
        return []
    except:
        return []

def safe_get_name(obj, default='Unknown'):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('name', default)
    return default

def safe_get_display_value(obj, default=''):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('display_value', default)
    return default

def format_ticket_for_dust(ticket, details, conversations):
    conversation_text = ""
    if conversations:
        conversation_text = "\n\n## Conversations\n\n"
        for conv in conversations:
            from_user = safe_get_name(conv.get('from'))
            created_time = safe_get_display_value(conv.get('created_time'))
            description = conv.get('description', '')
            conversation_text += f"**{from_user}** ({created_time}):\n{description}\n\n"
    
    subject = details.get('subject', ticket.get('subject', 'No Subject'))
    status = safe_get_name(details.get('status'))
    priority = safe_get_name(details.get('priority'))
    requester = safe_get_name(details.get('requester'))
    technician = safe_get_name(details.get('technician'), 'Unassigned')
    created_time = safe_get_display_value(details.get('created_time'))
    description = details.get('description', '')
    
    document_text = f"""# {subject}

**Ticket ID:** {ticket.get('id')}
**Status:** {status}
**Priority:** {priority}
**Requester:** {requester}
**Technician:** {technician}
**Created:** {created_time}

## Description

{description}

{conversation_text}
"""
    
    return {
        "document_id": f"ticket-{ticket.get('id')}",
        "timestamp": int(time.time() * 1000),
        "tags": [
            f"status:{status.lower().replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}"
        ],
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{ticket.get('id')}",
        "text": document_text
    }

def upload_to_dust(document):
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    url = f"{DUST_API_BASE}/{document['document_id']}"
    try:
        response = requests.post(url, headers=headers, json=document)
        if response.status_code in [200, 201]:
            return True
        return False
    except:
        return False

def main():
    print("=" * 60)
    print(f"FULL SYNC - Starting at {datetime.now()}")
    print("=" * 60)
    
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
                     'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"ERROR: Missing: {', '.join(missing_vars)}")
        return
    
    print(f"Configuration:")
    print(f"  SDP Domain: {SDP_DOMAIN}")
    print(f"  Dust Workspace: {DUST_WORKSPACE_ID}")
    print(f"  Dust Data Source: {DUST_FOLDER_NAME}")
    print()
    
    tickets = fetch_all_tickets()
    print(f"\n{'='*60}")
    print(f"Total unique tickets to process: {len(tickets)}")
    print(f"{'='*60}\n")
    
    if not tickets:
        print("No tickets to process")
        return
    
    access_token = get_access_token()
    success_count = 0
    error_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        if idx % 50 == 0 or idx == 1:
            print(f"[{idx}/{len(tickets)}] Processing tickets...")
        
        details = fetch_ticket_details(ticket_id, access_token)
        if not details:
            error_count += 1
            continue
        
        conversations = fetch_ticket_conversations(ticket_id, access_token)
        document = format_ticket_for_dust(ticket, details, conversations)
        
        if upload_to_dust(document):
            success_count += 1
            if idx % 50 == 0:
                print(f"  ✓ {success_count} uploaded, {error_count} errors")
        else:
            error_count += 1
        
        time.sleep(0.2)
        
        if idx % 500 == 0:
            access_token = get_access_token()
    
    print(f"\n{'='*60}")
    print(f"SYNC COMPLETED")
    print(f"  Success: {success_count}/{len(tickets)} tickets")
    print(f"  Errors: {error_count}")
    print(f"  Finished at: {datetime.now()}")
    print(f"{'='*60}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
