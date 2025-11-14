#!/usr/bin/env python3
"""
Sync ServiceDesk Plus On-Demand tickets to Dust
Runs automatically via GitHub Actions
"""
import os
import requests
import json
from datetime import datetime, timedelta

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
    
    # Check if cached token is still valid
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    print("🔑 Getting access token...")
    
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
        print(f"✅ Access token obtained (valid for 3600s)")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code} - {response.text}")


def fetch_sdp_tickets(limit=500, status_filter=None):
    """Fetch tickets from SDPOnDemand with pagination"""
    print(f"📥 Fetching up to {limit} tickets from SDPOnDemand...")
    
    access_token = get_access_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    
    all_tickets = []
    start_index = 1
    page_size = 100  # Fetch 100 tickets per page
    
    while len(all_tickets) < limit:
        # Build the request with pagination
        list_info = {
            'row_count': min(page_size, limit - len(all_tickets)),
            'start_index': start_index
        }
        
        if status_filter:
            list_info['filter_by'] = {'status': {'name': status_filter}}
        
        input_data = json.dumps({'list_info': list_info})
        data = {'input_data': input_data}
        
        url = f"{SDP_API_BASE}/requests"
        response = requests.post(url, headers=headers, data=data)
        
        if response.status_code == 200:
            response_data = response.json()
            tickets = response_data.get('requests', [])
            
            if not tickets:
                # No more tickets to fetch
                break
            
            all_tickets.extend(tickets)
            print(f"   Fetched page {start_index // page_size + 1}: {len(tickets)} tickets (total: {len(all_tickets)})")
            
            # If we got fewer tickets than requested, we've reached the end
            if len(tickets) < page_size:
                break
            
            start_index += page_size
        else:
            # If pagination fails, fall back to simple GET
            print(f"   Pagination failed, trying simple GET...")
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                response_data = response.json()
                all_tickets = response_data.get('requests', [])
                break
            else:
                raise Exception(f"Failed to fetch tickets: {response.status_code} - {response.text}")
    
    print(f"✅ Fetched {len(all_tickets)} tickets total")
    return all_tickets[:limit]


def format_ticket_for_dust(ticket):
    """Format ticket as markdown for Dust"""
    ticket_id = ticket.get('id', 'Unknown')
    subject = ticket.get('subject', 'No Subject')
    
    # Safely get nested values with None checks
    status = ticket.get('status', {}) or {}
    status_name = status.get('name', 'Unknown')
    
    priority = ticket.get('priority', {}) or {}
    priority_name = priority.get('name', 'Unknown')
    
    requester = ticket.get('requester', {}) or {}
    requester_name = requester.get('name', 'Unknown')
    requester_email = requester.get('email_id', '')
    
    technician = ticket.get('technician', {}) or {}
    technician_name = technician.get('name', 'Unassigned')
    
    created_time = ticket.get('created_time', {}) or {}
    created_time_display = created_time.get('display_value', 'Unknown')
    
    due_by_time = ticket.get('due_by_time', {}) or {}
    due_by_time_display = due_by_time.get('display_value', 'Not set')
    
    category = ticket.get('category', {}) or {}
    category_name = category.get('name', 'Uncategorized')
    
    description = ticket.get('description', 'No description')
    
    # Basic HTML cleanup
    if description:
        description = description.replace('<br>', '\n').replace('</p>', '\n\n')
    
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

---
*Synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*
"""
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
    
    if response.status_code != 200:
        print(f"      Dust API error: {response.status_code} - {response.text}")
    
    return response.status_code == 200


def sync_tickets(limit=500, status_filter=None):
    """Main sync function"""
    print("🚀 Starting SDPOnDemand → Dust sync")
    print("=" * 60)
    
    try:
        # Validate environment variables
        required_vars = [
            'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
            'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID'
        ]
        missing = [var for var in required_vars if not os.environ.get(var)]
        if missing:
            raise Exception(f"Missing required environment variables: {', '.join(missing)}")
        
        print(f"✅ All required environment variables present")
        print(f"   SDP Domain: {SDP_DOMAIN}")
        print(f"   Dust
