#!/usr/bin/env python3
"""
Test uploading ticket 15706 to Dust with correct data source name
"""

import os
import requests
import json
import time

# Config
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')  # Fixed!

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

def get_sdp_token():
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    return response.json()['access_token']

def test_upload_ticket():
    """Test uploading ticket 15706"""
    access_token = get_sdp_token()
    
    ticket_id = "150534000028967021"
    
    # Fetch details
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"Failed to fetch ticket: {response.status_code}")
        return
    
    details = response.json().get('request', {})
    
    # Create simple document
    status = details.get('status', {})
    status_name = status.get('name', 'Unknown') if isinstance(status, dict) else 'Unknown'
    
    priority = details.get('priority', {})
    priority_name = priority.get('name', 'Unknown') if isinstance(priority, dict) else 'Unknown'
    
    display_id = details.get('display_id', {})
    if isinstance(display_id, dict):
        display_id_str = display_id.get('display_value', ticket_id)
    else:
        display_id_str = str(display_id)
    
    subject = details.get('subject', 'No subject')
    
    document = {
        "document_id": f"ticket-{ticket_id}",
        "timestamp": int(time.time() * 1000),
        "tags": [
            f"status:{status_name.lower().replace(' ', '-')}",
            f"priority:{priority_name.lower().replace(' ', '-')}",
            f"ticket-id:{display_id_str}"
        ],
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{ticket_id}",
        "text": f"""# {subject}

**Ticket ID:** {display_id_str}
**Status:** {status_name}
**Priority:** {priority_name}

Test document to verify upload.
"""
    }
    
    print("="*60)
    print("UPLOADING TEST DOCUMENT TO DUST")
    print("="*60)
    print(f"Data Source: {DUST_FOLDER_NAME}")
    print(f"Document ID: {document['document_id']}")
    print(f"Tags: {document['tags']}")
    print()
    
    # Upload to Dust
    dust_headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    dust_url = f"{DUST_API_BASE}/{document['document_id']}"
    
    print(f"Uploading to: {dust_url}")
    
    dust_response = requests.post(dust_url, headers=dust_headers, json=document)
    
    print(f"Status: {dust_response.status_code}")
    print(f"Response: {dust_response.text[:500]}")
    
    if dust_response.status_code in [200, 201]:
        print("\n✓ Upload successful!")
        
        # Wait a moment then verify
        print("\nWaiting 3 seconds then verifying...")
        time.sleep(3)
        
        verify_response = requests.get(dust_url, headers=dust_headers)
        
        if verify_response.status_code == 200:
            print("\n✓ Document verified in Dust!")
            doc = verify_response.json().get('document', {})
            print(f"  Document ID: {doc.get('document_id')}")
            print(f"  Tags: {doc.get('tags', [])}")
            print(f"  Timestamp: {doc.get('timestamp')}")
            
            print("\n✓✓ SUCCESS! The ticket is now in Dust with correct tags.")
            print("\nNow try asking Dust:")
            print('  "Show me all tickets with status open"')
            print('  "Find tickets tagged status:open"')
        else:
            print(f"\n⚠️  Could not verify: {verify_response.status_code}")
            print(verify_response.text[:500])
    else:
        print("\n✗ Upload failed!")
        print(f"Error: {dust_response.text}")

if __name__ == "__main__":
    test_upload_ticket()
