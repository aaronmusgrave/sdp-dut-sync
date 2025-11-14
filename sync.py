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


def fetch_sdp_tickets(limit=100, status_filter=None):
    """Fetch tickets from SDPOnDemand using GET request"""
    print(f"📥 Fetching tickets from SDPOnDemand...")
    
    access_token = get_access_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    
    # Try simple GET request without input_data
    url = f"{SDP_API_BASE}/requests"
    print(f"   Calling: {url}")
    
    response = requests.get(url, headers=headers)
    
    print(f"   Response status: {response.status_code}")
    
    if response.status_code == 200:
        response_data = response.json()
        tickets = response_data.get('requests', [])
        print(f"✅ Fetched {len(tickets)} tickets")
        # Limit tickets if needed
        return tickets[:limit]
    else:
        print(f"   Response body: {response.text}")
        raise Exception(f"Failed to fetch tickets: {response.status_code} - {response.text}")


def format_ticket_for_dust(ticket):
    """Format ticket as markdown for Dust"""
    ticket_id = ticket.get('id', 'Unknown')
    subject = ticket.get('subject', 'No Subject')
    status = ticket.get('status', {}).get('name', 'Unknown')
    priority = ticket.get('priority', {}).get('name', 'Unknown')
    requester = ticket.get('requester', {}).get('name', 'Unknown')
    requester_email = ticket.get('requester', {}).get('email_id', '')
    technician = ticket.get('technician', {}).get('name', 'Unassigned')
    created_time = ticket.get('created_time', {}).get('display_value', 'Unknown')
    due_by_time = ticket.get('due_by_time', {}).get('display_value', 'Not set')
    category = ticket.get('category', {}).get('name', 'Uncategorized')
    description = ticket.get('description', 'No description')
    
    # Basic HTML cleanup
    description = description.replace('<br>', '\n').replace('</p>', '\n\n')
    
    text = f"""# Ticket #{ticket_id}: {subject}

## Details
- **Status**: {status}
- **Priority**: {priority}
- **Category**: {category}
- **Created**: {created_time}
- **Due By**: {due_by_time}

## People
- **Requester**: {requester} ({requester_email})
- **Assigned To**: {technician}

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


def sync_tickets(limit=100, status_filter=None):
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
        print(f"   Dust Workspace: {DUST_WORKSPACE_ID}")
        print(f"   Dust Folder: {DUST_FOLDER_NAME}")
        print()
        
        # Fetch tickets
        tickets = fetch_sdp_tickets(limit=limit, status_filter=status_filter)
        
        if not tickets:
            print("⚠️  No tickets to sync")
            return
        
        print(f"\n📤 Uploading {len(tickets)} tickets to Dust...")
        print("-" * 60)
        
        success_count = 0
        failed_tickets = []
        
        for ticket in tickets:
            ticket_id = ticket.get('id')
            subject = ticket.get('subject', 'No subject')[:50]
            
            ticket_text = format_ticket_for_dust(ticket)
            
            if upload_ticket_to_dust(ticket_id, ticket_text):
                success_count += 1
                print(f"  ✅ #{ticket_id}: {subject}")
            else:
                failed_tickets.append(ticket_id)
                print(f"  ❌ #{ticket_id}: {subject}")
        
        print("-" * 60)
        print(f"\n🎉 Sync complete!")
        print(f"   Successfully uploaded: {success_count}/{len(tickets)}")
        
        if failed_tickets:
            print(f"   Failed tickets: {', '.join(map(str, failed_tickets))}")
        
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Sync failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    # Sync all tickets
    sync_tickets(limit=100, status_filter=None)
