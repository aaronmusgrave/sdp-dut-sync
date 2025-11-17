#!/usr/bin/env python3
"""
Incremental Sync - Runs hourly to capture new/updated tickets
Only syncs tickets created or modified since last run
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape

# Configuration from environment variables
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk-tickets')

# Sync window - how far back to look for changes
SYNC_WINDOW_HOURS = int(os.environ.get('SYNC_WINDOW_HOURS', '2'))

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

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

def get_modified_since_timestamp():
    """Calculate the timestamp for modified_since filter"""
    cutoff_time = datetime.now() - timedelta(hours=SYNC_WINDOW_HOURS)
    timestamp_ms = int(cutoff_time.timestamp() * 1000)
    return timestamp_ms

def fetch_modified_tickets():
    """Fetch tickets modified since last sync"""
    access_token = get_access_token()
    all_tickets = []
    seen_ticket_ids = set()
    start_index = 1
    
    modified_since = get_modified_since_timestamp()
    cutoff_datetime = datetime.fromtimestamp(modified_since / 1000)
    
    print(f"Fetching tickets modified since: {cutoff_datetime}")
    print(f"(Last {SYNC_WINDOW_HOURS} hours)")
    
    while True:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "sort_field": "last_updated_time",
                "sort_order": "desc",
                "search_criteria": [
                    {
                        "field": "last_updated_time",
                        "condition": "greater than",
                        "value": modified_since
                    }
                ]
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
                    print(f"  No more tickets. Total found: {len(all_tickets)}")
                    break
                
                new_tickets = []
                for ticket in tickets:
                    ticket_id = ticket.get('id')
                    if ticket_id not in seen_ticket_ids:
                        new_tickets.append(ticket)
                        seen_ticket_ids.add(ticket_id)
                
                print(f"  Batch at index {start_index}: got {len(tickets)} tickets, {len(new_tickets)} new")
                all_tickets.extend(new_tickets)
                
                response_list_info = result.get('list_info', {})
                has_more = response_list_info.get('has_more_rows', False)
                
                if not has_more or len(new_tickets) == 0:
                    break
                
                start_index += 100
                time.sleep(0.5)
                
            elif response.status_code == 400:
                print("  Search criteria not supported, fetching recent tickets instead...")
                return fetch_recent_tickets_fallback()
            else:
                print(f"  Failed: {response.status_code}")
                print(f"  Response: {response.text[:500]}")
                break
                
        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"\n  ✓ Found {len(all_tickets)} modified tickets")
    return all_tickets

def fetch_recent_tickets_fallback():
    """Fallback: fetch most recent tickets without filter"""
    access_token = get_access_token()
    
    print("  Using fallback: fetching 100 most recent tickets")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "sort_field": "last_updated_time",
            "sort_order": "desc"
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
            print(f"  ✓ Fetched {len(tickets)} recent tickets")
            return tickets
    except:
        pass
    
    return []

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

def strip_html(html_text):
    """Remove HTML tags and decode HTML entities"""
    if not html_text:
        return ""
    
    # Decode HTML entities
    text = unescape(html_text)
    
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

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

def parse_sdp_timestamp(timestamp_obj):
    """Parse ServiceDesk Plus timestamp to milliseconds"""
    if not timestamp_obj:
        return int(time.time() * 1000)
    
    if isinstance(timestamp_obj, dict):
        if 'value' in timestamp_obj:
            return int(timestamp_obj['value'])
    
    return int(time.time() * 1000)

def get_display_id(details):
    """Extract the display ID (short ticket number) from ticket details"""
    # Try to get display_id field
    if 'display_id' in details:
        display_id = details['display_id']
        if isinstance(display_id, dict):
            return display_id.get('display_value', details.get('id'))
        return str(display_id)
    
    # Fallback to id
    return str(details.get('id', 'Unknown'))

def format_ticket_for_dust(ticket, details, conversations):
    # Build conversation text with HTML stripped
    conversation_text = ""
    if conversations:
        conversation_text = "\n\n## Conversations\n\n"
        for conv in conversations:
            from_user = safe_get_name(conv.get('from'))
            created_time = safe_get_display_value(conv.get('created_time'))
            
            # Get description and strip HTML
            raw_description = conv.get('description', '')
            clean_description = strip_html(raw_description)
            
            if clean_description:  # Only add if there's actual content
                conversation_text += f"**{from_user}** ({created_time}):\n{clean_description}\n\n"
    
    subject = details.get('subject', ticket.get('subject', 'No Subject'))
    status = safe_get_name(details.get('status'))
    priority = safe_get_name(details.get('priority'))
    requester = safe_get_name(details.get('requester'))
    technician = safe_get_name(details.get('technician'), 'Unassigned')
    created_time = safe_get_display_value(details.get('created_time'))
    
    # Get display ID (short ticket number)
    display_id = get_display_id(details)
    
    # Strip HTML from description
    raw_description = details.get('description', '')
    clean_description = strip_html(raw_description)
    
    document_text = f"""# {subject}

**Ticket ID:** {display_id}
**Status:** {status}
**Priority:** {priority}
**Requester:** {requester}
**Technician:** {technician}
**Created:** {created_time}

## Description

{clean_description}

{conversation_text}
"""
    
    # Use the ticket's actual last updated time
    last_updated_time = details.get('last_updated_time', {})
    timestamp = parse_sdp_timestamp(last_updated_time)
    
    # Use internal ID for document_id (must be unique)
    internal_id = ticket.get('id')
    
    return {
        "document_id": f"ticket-{internal_id}",
        "timestamp": timestamp,
        "tags": [
            f"status:{status.lower().replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}",
            f"ticket-id:{display_id}"
        ],
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{internal_id}",
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
    print(f"INCREMENTAL SYNC - Starting at {datetime.now()}")
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
    print(f"  Sync Window: {SYNC_WINDOW_HOURS} hours")
    print()
    
    tickets = fetch_modified_tickets()
    
    if not tickets:
        print("\n✓ No modified tickets found - nothing to sync")
        return
    
    print(f"\n{'='*60}")
    print(f"Processing {len(tickets)} modified tickets")
    print(f"{'='*60}\n")
    
    access_token = get_access_token()
    success_count = 0
    error_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        if idx % 10 == 0 or idx == 1:
            print(f"[{idx}/{len(tickets)}] Processing...")
        
        details = fetch_ticket_details(ticket_id, access_token)
        if not details:
            error_count += 1
            continue
        
        conversations = fetch_ticket_conversations(ticket_id, access_token)
        document = format_ticket_for_dust(ticket, details, conversations)
        
        if upload_to_dust(document):
            success_count += 1
            if idx % 10 == 0:
                print(f"  ✓ {success_count} uploaded, {error_count} errors")
        else:
            error_count += 1
        
        time.sleep(0.2)
    
    print(f"\n{'='*60}")
    print(f"INCREMENTAL SYNC COMPLETED")
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
