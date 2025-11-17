#!/usr/bin/env python3
"""
Incremental Sync - Runs hourly to capture new/updated tickets
Fetches the most recent tickets to catch new ones
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

def fetch_recent_tickets():
    """Fetch the most recent tickets"""
    access_token = get_access_token()
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    url = f"{SDP_API_BASE}/requests"
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Fetch tickets response: {response.status_code}")
        if response.status_code == 200:
            tickets = response.json().get('requests', [])
            return tickets
        else:
            print(f"Failed to fetch tickets: {response.status_code}")
            print(f"Response: {response.text}")
        return []
    except Exception as e:
        print(f"Error fetching tickets: {e}")
        return []

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('request', {})
        else:
            print(f"Failed to fetch details for ticket {ticket_id}: {response.status_code}")
        return None
    except Exception as e:
        print(f"Error fetching ticket {ticket_id} details: {e}")
        return None

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch conversations for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('conversations', [])
        else:
            print(f"Failed to fetch conversations for ticket {ticket_id}: {response.status_code}")
        return []
    except Exception as e:
        print(f"Error fetching conversations for ticket {ticket_id}: {e}")
        return []

def safe_get_name(obj, default='Unknown'):
    """Safely extract name from an object that might be None or a dict"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('name', default)
    return default

def safe_get_display_value(obj, default=''):
    """Safely extract display_value from an object that might be None or a dict"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('display_value', default)
    return default

def format_ticket_for_dust(ticket, details, conversations):
    """Format ticket data into a document for Dust"""
    
    # Build conversation text
    conversation_text = ""
    if conversations:
        conversation_text = "\n\n## Conversations\n\n"
        for conv in conversations:
            from_user = safe_get_name(conv.get('from'))
            created_time = safe_get_display_value(conv.get('created_time'))
            description = conv.get('description', '')
            conversation_text += f"**{from_user}** ({created_time}):\n{description}\n\n"
    
    # Safely extract all fields
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
        "timestamp": int(time.time()),
        "tags": [
            f"status:{status.lower().replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}"
        ],
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{ticket.get('id')}",
        "text": document_text
    }

def upload_to_dust(document):
    """Upload a single document to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{document['document_id']}"
    
    try:
        response = requests.post(url, headers=headers, json=document)
        if response.status_code in [200, 201]:
            print(f"✓ Uploaded ticket {document['document_id']}")
            return True
        else:
            print(f"✗ Failed to upload {document['document_id']}: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"✗ Error uploading {document['document_id']}: {e}")
        return False

def main():
    """Main sync function"""
    print("=" * 50)
    print(f"Starting incremental sync at {datetime.now()}")
    print("=" * 50)
    
    # Validate required environment variables
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
                     'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"ERROR: Missing required environment variables: {', '.join(missing_vars)}")
        return
    
    print(f"Configuration:")
    print(f"  SDP Domain: {SDP_DOMAIN}")
    print(f"  Dust Workspace: {DUST_WORKSPACE_ID}")
    print(f"  Dust Data Source: {DUST_FOLDER_NAME}")
    print()
    
    # Fetch recent tickets
    tickets = fetch_recent_tickets()
    print(f"Found {len(tickets)} recent tickets")
    
    if not tickets:
        print("No tickets to process")
        return
    
    # Get access token once for all detail/conversation fetches
    access_token = get_access_token()
    
    # Process each ticket
    success_count = 0
    for ticket in tickets:
        ticket_id = ticket.get('id')
        print(f"\nProcessing ticket {ticket_id}...")
        
        # Fetch details and conversations
        details = fetch_ticket_details(ticket_id, access_token)
        if not details:
            print(f"Skipping ticket {ticket_id} - no details available")
            continue
        
        conversations = fetch_ticket_conversations(ticket_id, access_token)
        
        # Format and upload to Dust
        document = format_ticket_for_dust(ticket, details, conversations)
        if upload_to_dust(document):
            success_count += 1
        
        # Small delay to avoid rate limiting
        time.sleep(0.5)
    
    print(f"\n{'='*50}")
    print(f"Sync completed: {success_count}/{len(tickets)} tickets uploaded successfully")
    print(f"Finished at {datetime.now()}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
