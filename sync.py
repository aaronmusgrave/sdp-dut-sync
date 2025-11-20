#!/usr/bin/env python3
"""
Test uploading a single real ticket to Dust
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape

# Configuration
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
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    if response.status_code == 200:
        return response.json()['access_token']
    return None

def strip_html(html_text):
    if not html_text:
        return ""
    try:
        text = unescape(html_text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except:
        return str(html_text)

def safe_get_name(obj, default='Unknown'):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('name', default)
    return default

def test_single_ticket_upload():
    """Fetch one real ticket and try to upload it"""
    print("=" * 60)
    print("TESTING SINGLE TICKET UPLOAD")
    print("=" * 60)
    
    # Get ServiceDesk token
    access_token = get_access_token()
    if not access_token:
        print("❌ Failed to get ServiceDesk token")
        return
    
    # Fetch one ticket
    print("\n1. Fetching one ticket from ServiceDesk...")
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {"list_info": {"row_count": 1, "start_index": 1}}
    params = {'input_data': json.dumps(list_info)}
    
    response = requests.get(f"{SDP_API_BASE}/requests", headers=headers, params=params)
    
    if response.status_code != 200:
        print(f"❌ Failed to fetch ticket: {response.status_code}")
        return
    
    tickets = response.json().get('requests', [])
    if not tickets:
        print("❌ No tickets found")
        return
    
    ticket = tickets[0]
    ticket_id = ticket.get('id')
    print(f"✅ Fetched ticket: {ticket_id}")
    
    # Get ticket details
    print(f"\n2. Getting ticket details...")
    detail_url = f"{SDP_API_BASE}/requests/{ticket_id}"
    detail_response = requests.get(detail_url, headers=headers)
    
    if detail_response.status_code != 200:
        print(f"❌ Failed to get details: {detail_response.status_code}")
        return
    
    details = detail_response.json().get('request', {})
    print(f"✅ Got details")
    
    # Format document
    print(f"\n3. Formatting document...")
    
    subject = details.get('subject', 'No Subject')
    status = safe_get_name(details.get('status'))
    priority = safe_get_name(details.get('priority'))
    
    description = strip_html(details.get('description', ''))
    
    document = {
        "document_id": f"ticket-{ticket_id}",
        "timestamp": int(time.time() * 1000),
        "tags": [
            f"status:{status.lower().replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}"
        ],
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{ticket_id}",
        "text": f"# {subject}\n\n**Status:** {status}\n**Priority:** {priority}\n\n{description}"
    }
    
    print(f"✅ Document formatted")
    print(f"   Document ID: {document['document_id']}")
    print(f"   Subject: {subject[:50]}...")
    
    # Upload to Dust
    print(f"\n4. Uploading to Dust...")
    print(f"   URL: {DUST_API_BASE}/{document['document_id']}")
    
    dust_headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    upload_url = f"{DUST_API_BASE}/{document['document_id']}"
    
    print(f"\n   Sending request...")
    upload_response = requests.post(upload_url, headers=dust_headers, json=document, timeout=30)
    
    print(f"   Status code: {upload_response.status_code}")
    print(f"   Response: {upload_response.text[:500]}")
    
    if upload_response.status_code in [200, 201]:
        print(f"\n✅ SUCCESS! Ticket uploaded to Dust")
        print(f"\nGo check Dust - you should see ticket: {document['document_id']}")
    else:
        print(f"\n❌ FAILED to upload")
        print(f"Full response: {upload_response.text}")
    
    # Verify in Dust
    print(f"\n5. Verifying in Dust...")
    verify_url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}"
    verify_response = requests.get(verify_url, headers={'Authorization': f'Bearer {DUST_API_KEY}'})
    
    if verify_response.status_code == 200:
        doc_count = verify_response.json().get('data_source', {}).get('document_count', 0)
        print(f"   Total documents in Dust: {doc_count}")

if __name__ == "__main__":
    test_single_ticket_upload()
    
