#!/usr/bin/env python3
"""
Sync ServiceDesk Plus On-Demand tickets to Dust
First run: Full sync of all tickets (trying multiple API methods)
Subsequent runs: Incremental sync of only new/updated tickets
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

# Sync state tracking
SYNC_STATE_DOC_ID = "sync_state_tracker"

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
            raise Exception(f"No access_token in response: {data}")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained successfully")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code} - {response.text}")


def get_sync_state():
    """Get the sync state from Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{SYNC_STATE_DOC_ID}"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            text = data.get('document', {}).get('text', '')
            
            state = {
                'initial_sync_complete': 'initial_sync_complete: true' in text,
                'last_sync_timestamp': None
            }
            
            if 'last_sync_timestamp:' in text:
                timestamp_str = text.split('last_sync_timestamp:')[1].split('\n')[0].strip()
                state['last_sync_timestamp'] = int(timestamp_str)
            
            return state
        else:
            return {'initial_sync_complete': False, 'last_sync_timestamp': None}
    except Exception as e:
        print(f"No previous sync state found, will do full initial sync")
        return {'initial_sync_complete': False, 'last_sync_timestamp': None}


def save_sync_state(timestamp, initial_sync_complete=True):
    """Save the sync state to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{SYNC_STATE_DOC_ID}"
    
    text = f"""Sync State Tracker
initial_sync_complete: {str(initial_sync_complete).lower()}
last_sync_timestamp: {timestamp}
last_sync_date: {datetime.fromtimestamp(timestamp/1000).strftime('%Y-%m-%d %H:%M:%S UTC')}
"""
    
    payload = {'text': text}
    
    try:
        requests.post(url, headers=headers, json=payload)
    except Exception as e:
        print(f"Warning: Could not save sync state: {str(e)}")


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


def fetch_all_tickets(max_tickets=10000):
    """Fetch ALL tickets using multiple API methods"""
    print(f"Fetching ALL tickets (up to {max_tickets})...")
    print("=" * 60)
    
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    all_tickets = []
    
    # METHOD 1: Search API with GET parameters
    print("\nMETHOD 1: Search API with GET parameters")
    print("-" * 60)
    
    for start_index in range(1, max_tickets, 100):
        search_criteria = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "get_total_count": True
            }
        }
        
        url = f"{SDP_API_BASE}/requests"
        params = {'input_data': json.dumps(search_criteria)}
        
        print(f"Fetching from index {start_index}...")
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            data = response.json()
            tickets = data.get('requests', [])
            
            if tickets:
                all_tickets.extend(tickets)
                print(f"  Got {len(tickets)} tickets (total: {len(all_tickets)})")
                
                response_status = data.get('response_status', {})
                total_count = response_status.get('total_count')
                if total_count:
                    print(f"  API reports {total_count} total tickets available")
                
                if len(tickets) < 100:
                    print(f"  Reached end of results")
                    break
            else:
                print(f"  No more tickets")
                break
        else:
            print(f"Method 1 failed: {response.status_code} - {response.text[:200]}")
            break
        
        time.sleep(0.5)
    
    if all_tickets:
        print(f"\nMETHOD 1 SUCCESS: Fetched {len(all_tickets)} tickets")
        return all_tickets
    
    # METHOD 2: Direct query parameters
    print("\nMETHOD 2: Direct query parameters")
    print("-" * 60)
    all_tickets = []
    
    for start_index in range(1, max_tickets, 100):
        params = {
            'row_count': 100,
            'start_index': start_index
        }
        
        url = f"{SDP_API_BASE}/requests"
        print(f"Fetching from index {start_index}...")
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            data = response.json()
            tickets = data.get('requests', [])
            
            if tickets:
                all_tickets.extend(tickets)
                print(f"  Got {len(tickets)} tickets (total: {len(all_tickets)})")
                
                if len(tickets) < 100:
                    break
            else:
                break
        else:
            print(f"Method 2 failed: {response.status_code}")
            break
        
        time.sleep(0.5)
    
    if all_tickets:
        print(f"\nMETHOD 2 SUCCESS: Fetched {len(all_tickets)} tickets")
        return all_tickets
    
    # METHOD 3: Fetch by date ranges (30-day chunks)
    print("\nMETHOD 3: Fetch by date ranges")
    print("-" * 60)
    
    all_tickets = []
    current_date = datetime.now()
    
    # Go back 2 years in 30-day chunks
    for days_back in range(0, 730, 30):
        start_date = current_date - timedelta(days=days_back + 30)
        end_date = current_date - timedelta(days=days_back)
        
        start_timestamp = int(start_date.timestamp() * 1000)
        end_timestamp = int(end_date.timestamp() * 1000)
        
        search_criteria = {
            "list_info": {
                "row_count": 100,
                "filter_by": {
                    "created_time": {
                        "from": start_timestamp,
                        "to": end_timestamp
                    }
                }
            }
        }
        
        url = f"{SDP_API_BASE}/requests"
        data = {'input_data': json.dumps(search_criteria)}
        
        print(f"Fetching tickets from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}...")
        response = requests.post(url, headers=headers, data=data)
        
        if response.status_code == 200:
            tickets_data = response.json()
            tickets = tickets_data.get('requests', [])
            
            if tickets:
                all_tickets.extend(tickets)
                print(f"  Got {len(tickets)} tickets (total: {len(all_tickets)})")
        else:
            print(f"  Date range failed: {response.status_code}")
        
        time.sleep(0.5)
        
        if len(all_tickets) >= max_tickets:
            break
    
    if all_tickets:
        print(f"\nMETHOD 3 SUCCESS: Fetched {len(all_tickets)} tickets")
        return all_tickets
    
    # FALLBACK: Just get default tickets
    print("\nAll methods failed, using default API call...")
    print("-" * 60)
    
    url = f"{SDP_API_BASE}/requests"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        all_tickets = response.json().get('requests', [])
        print(f"Fallback: Got {len(all_tickets)} tickets")
    
    print(f"\nTOTAL FETCHED: {len(all_tickets)} tickets")
    print("=" * 60)
    return all_tickets


def fetch_modified_tickets(since_timestamp, max_tickets=1000):
    """Fetch tickets modified since timestamp"""
    print(f"Fetching tickets modified since {datetime.fromtimestamp(since_timestamp/1000).strftime('%Y-%m-%d %H:%M:%S')}...")
    
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    all_tickets = []
    
    # Try with modified date filter
    search_criteria = {
        "list_info": {
            "row_count": 100,
            "filter_by": {
                "last_updated_time": {
                    "from": since_timestamp
                }
            }
        }
    }
    
    url = f"{SDP_API_BASE}/requests"
    data = {'input_data': json.dumps(search_criteria)}
    
    response = requests.post(url, headers=headers, data=data)
    
    if response.status_code == 200:
        response_data = response.json()
        all_tickets = response_data.get('requests', [])
        print(f"Found {len(all_tickets)} modified tickets")
    else:
        print("Modified-date filter not supported, falling back to recent tickets")
        # Fallback: just get recent tickets
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            all_tickets = response.json().get('requests', [])
    
    return all_tickets


def clean_html(text):
    """Basic HTML cleanup"""
    if not text:
        return ""
    text = text.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
    text = text.replace('</p>', '\n\n').replace('<p>', '')
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    import re
    text = re.sub('<[^<]+?>', '', text)
    return text.strip()


def format_ticket_for_dust(ticket, detailed_ticket=None, conversations=None):
    """Format ticket as markdown for Dust"""
    ticket_id = ticket.get('id', 'Unknown')
    subject = ticket.get('subject', 'No Subject')
    
    ticket_data = detailed_ticket if detailed_ticket else ticket
    
    status = ticket_data.get('status', {}) or {}
    status_name = status.get('name', 'Unknown')
    
    priority = ticket_data.get('priority', {}) or {}
    priority_name = priority.get('name', 'Unknown')
    
    requester = ticket_data.get('requester', {}) or {}
    requester_name = requester.get('name', 'Unknown')
    requester_email = requester.get('email_id', '')
    
    technician = ticket_data.get('technician', {}) or {}
    technician_name = technician.get('name', 'Unassigned')
    
    created_time = ticket_data.get('created_time', {}) or {}
    created_time_display = created_time.get('display_value', 'Unknown')
    
    due_by_time = ticket_data.get('due_by_time', {}) or {}
    due_by_time_display = due_by_time.get('display_value', 'Not set')
    
    category = ticket_data.get('category', {}) or {}
    category_name = category.get('name', 'Uncategorized')
    
    description = clean_html(ticket_data.get('description', 'No description'))
    
    resolution = ticket_data.get('resolution', {}) or {}
    resolution_content = clean_html(resolution.get('content', ''))
    
    attachments = ticket_data.get('attachments', []) or []
    
    text = f"""# Ticket #{ticket_id}: {subject}

## Details
- **Status**: {status_name}
- **Priority**: {priority_name}
- **Category**: {category_name}
- **Created**: {created_time_display}
- **Due By**: {due_by_time_display}

## People
- **Requester**: {requester_name} ({requester_email})
- **Assigned To**: {technician_name}

## Description
{description}
"""

    if resolution_content:
        text += f"\n## Resolution\n{resolution_content}\n"

    if attachments:
        text += "\n## Attachments\n"
        for att in attachments:
            name = att.get('name', 'Unknown')
            size_raw = att.get('size', 0)
            try:
                size_kb = float(size_raw) / 1024 if size_raw else 0
            except (ValueError, TypeError):
                size_kb = 0
            
            if size_kb > 0:
                text += f"- {name} ({size_kb:.1f} KB)\n"
            else:
                text += f"- {name}\n"

    if conversations:
        text += "\n## Comments & Updates\n\n"
        for conv in conversations:
            from_user = (conv.get('from', {}) or {}).get('name', 'Unknown')
            time_val = (conv.get('created_time', {}) or {}).get('display_value', 'Unknown')
            content = clean_html(conv.get('description', '') or conv.get('reply_content', ''))
            
            if content:
                text += f"### {from_user} - {time_val}\n{content}\n\n"

    text += f"\n---\n*Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*\n"
    return text


def upload_ticket_to_dust(ticket_id, ticket_text):
    """Upload a ticket to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{ticket_id}"
    payload = {
        'text': ticket_text,
        'source_url': f"https://{SDP_DOMAIN}/app/{ticket_id}"
    }
    
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 200


def sync_tickets(include_details=True):
    """Main sync function"""
    print("Starting SDPOnDemand to Dust sync")
    print("=" * 60)
    
    try:
        required_vars = [
            'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
            'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID'
        ]
        missing = [var for var in required_vars if not os.environ.get(var)]
        if missing:
            raise Exception(f"Missing required environment variables: {', '.join(missing)}")
        
        print(f"All required environment variables present")
        print(f"SDP Domain: {SDP_DOMAIN}")
        print(f"Dust Workspace: {DUST_WORKSPACE_ID}")
        print(f"Dust Folder: {DUST_FOLDER_NAME}")
        if include_details:
            print(f"Including: Comments, Attachments, Resolution")
        print()
        
        sync_state = get_sync_state()
        current_timestamp = int(datetime.now().timestamp() * 1000)
        
        if not sync_state['initial_sync_complete']:
            print("INITIAL SYNC - Fetching ALL tickets")
            print()
            tickets = fetch_all_tickets(max_tickets=5000)
            is_initial_sync = True
        else:
            print("INCREMENTAL SYNC")
            last_sync = sync_state['last_sync_timestamp']
            print(f"Last sync: {datetime.fromtimestamp(last_sync/1000).strftime('%Y-%m-%d %H:%M:%S')}")
            print()
            tickets = fetch_modified_tickets(last_sync, max_tickets=1000)
            is_initial_sync = False
        
        if not tickets:
            print("No tickets to sync")
            print("=" * 60)
            return
        
        print(f"\nUploading {len(tickets)} tickets to Dust...")
        print("-" * 60)
        
        access_token = get_access_token()
        success_count = 0
        failed_tickets = []
        
        for i, ticket in enumerate(tickets, 1):
            ticket_id = ticket.get('id')
            subject = ticket.get('subject', 'No subject')[:50]
            
            detailed_ticket = None
            conversations = None
            
            if include_details:
                detailed_ticket = fetch_ticket_details(ticket_id, access_token)
                conversations = fetch_ticket_conversations(ticket_id, access_token)
            
            ticket_text = format_ticket_for_dust(ticket, detailed_ticket, conversations)
            
            if upload_ticket_to_dust(ticket_id, ticket_text):
                success_count += 1
                comment_count = len(conversations) if conversations else 0
                detail_info = f" ({comment_count} comments)" if include_details and comment_count > 0 else ""
                print(f"SUCCESS [{i}/{len(tickets)}] #{ticket_id}: {subject}{detail_info}")
            else:
                failed_tickets.append(ticket_id)
                print(f"FAILED [{i}/{len(tickets)}] #{ticket_id}: {subject}")
        
        save_sync_state(current_timestamp, initial_sync_complete=True)
        
        print("-" * 60)
        print(f"\nSync complete!")
        print(f"Successfully uploaded: {success_count}/{len(tickets)}")
        
        if failed_tickets:
            print(f"Failed tickets: {', '.join(map(str, failed_tickets))}")
        
        if is_initial_sync:
            print(f"\nInitial sync complete! Future syncs will be incremental.")
        else:
            print(f"\nNext sync will fetch tickets modified after: {datetime.fromtimestamp(current_timestamp/1000).strftime('%Y-%m-%d %H:%M:%S')}")
        
        print("=" * 60)
        
    except Exception as e:
        print(f"\nSync failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    sync_tickets(include_details=True)
