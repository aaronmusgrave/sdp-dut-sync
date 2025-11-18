#!/usr/bin/env python3
"""
Check the exact tags for the most recently synced ticket
"""

import os
import requests
import json
from datetime import datetime, timedelta

# ServiceDesk
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

# Dust
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = 'servicedesk-tickets'

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}"

def get_sdp_token():
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    return response.json()['access_token']

def get_recent_ticket_from_sdp():
    """Get the most recently modified ticket from ServiceDesk"""
    access_token = get_sdp_token()
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 1,
            "start_index": 1,
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_API_BASE}/requests"
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        tickets = response.json().get('requests', [])
        if tickets:
            ticket_id = tickets[0].get('id')
            
            # Get full details
            detail_url = f"{SDP_API_BASE}/requests/{ticket_id}"
            detail_response = requests.get(detail_url, headers=headers)
            
            if detail_response.status_code == 200:
                details = detail_response.json().get('request', {})
                
                status = details.get('status', {})
                status_name = status.get('name', 'Unknown') if isinstance(status, dict) else str(status)
                
                priority = details.get('priority', {})
                priority_name = priority.get('name', 'Unknown') if isinstance(priority, dict) else str(priority)
                
                display_id = details.get('display_id', {})
                if isinstance(display_id, dict):
                    display_id = display_id.get('display_value', ticket_id)
                
                print("="*60)
                print("MOST RECENT TICKET IN SERVICEDESK")
                print("="*60)
                print(f"Internal ID: {ticket_id}")
                print(f"Display ID: {display_id}")
                print(f"Subject: {details.get('subject')}")
                print(f"Status: {status_name}")
                print(f"Priority: {priority_name}")
                print(f"\nExpected tags:")
                print(f"  - status:{status_name.lower().replace(' ', '-')}")
                print(f"  - priority:{priority_name.lower().replace(' ', '-')}")
                print(f"  - ticket-id:{display_id}")
                
                return ticket_id

def check_ticket_in_dust(ticket_id):
    """Check what tags this ticket has in Dust"""
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    
    doc_id = f"ticket-{ticket_id}"
    url = f"{DUST_API_BASE}/documents/{doc_id}"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        doc = response.json().get('document', {})
        
        print("\n" + "="*60)
        print("SAME TICKET IN DUST")
        print("="*60)
        print(f"Document ID: {doc.get('document_id')}")
        print(f"Tags: {doc.get('tags', [])}")
        
        # Extract status from text
        text = doc.get('text', '')
        for line in text.split('\n')[:20]:
            if 'Status:' in line or 'Priority:' in line or 'Ticket ID:' in line:
                print(f"  {line.strip()}")
        
        # Check for status tags
        tags = doc.get('tags', [])
        status_tags = [t for t in tags if t.startswith('status:')]
        priority_tags = [t for t in tags if t.startswith('priority:')]
        
        print(f"\nStatus tags: {status_tags}")
        print(f"Priority tags: {priority_tags}")
        
    elif response.status_code == 404:
        print("\n⚠️  Ticket NOT FOUND in Dust!")
        print("This ticket may not have synced yet or there's an ID mismatch")
    else:
        print(f"\nFailed to check Dust: {response.status_code}")
        print(response.text)

def sample_open_tickets():
    """Sample tickets with 'open' in their tags"""
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    
    url = f"{DUST_API_BASE}/documents"
    params = {'limit': 100, 'offset': 0}
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        documents = response.json().get('documents', [])
        
        print("\n" + "="*60)
        print("SAMPLING ALL STATUS TAGS IN DUST")
        print("="*60)
        
        status_distribution = {}
        open_tickets = []
        
        for doc in documents:
            tags = doc.get('tags', [])
            status_tags = [t for t in tags if t.startswith('status:')]
            
            for tag in status_tags:
                status_distribution[tag] = status_distribution.get(tag, 0) + 1
                
                if tag == 'status:open':
                    open_tickets.append(doc.get('document_id'))
        
        print("\nStatus tag distribution (from sample of 100):")
        for tag, count in sorted(status_distribution.items()):
            print(f"  {tag}: {count}")
        
        print(f"\n✓ Found {len(open_tickets)} tickets with 'status:open' tag")
        if open_tickets:
            print(f"  Examples: {open_tickets[:5]}")
    else:
        print(f"Failed: {response.status_code}")

if __name__ == "__main__":
    print("TAG FORMAT VERIFICATION\n")
    
    # Get most recent ticket from ServiceDesk
    ticket_id = get_recent_ticket_from_sdp()
    
    if ticket_id:
        # Check same ticket in Dust
        check_ticket_in_dust(ticket_id)
    
    # Sample open tickets
    sample_open_tickets()
    
