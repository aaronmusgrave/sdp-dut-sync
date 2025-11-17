#!/usr/bin/env python3
"""
Debug script to find actual conversation/comment content
"""

import os
import requests
import json
from datetime import datetime, timedelta

# Configuration
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

_access_token = None
_token_expiry = None

def get_access_token():
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
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained\n")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def check_ticket_detail(ticket_id):
    """Check if ticket detail has conversation/comment fields"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    print(f"{'='*60}")
    print(f"Checking FULL TICKET DETAIL for: {ticket_id}")
    print(f"{'='*60}\n")
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            ticket = data.get('request', {})
            
            # List all top-level keys
            print("All fields in ticket:")
            for key in sorted(ticket.keys()):
                print(f"  - {key}")
            
            print("\n" + "="*60)
            print("Checking for conversation-related fields:")
            print("="*60 + "\n")
            
            # Check specific fields that might contain text
            fields_to_check = [
                'description', 'short_description', 'notes', 'comments',
                'resolution', 'workaround', 'conversation', 'replies',
                'thread', 'messages', 'interactions', 'history'
            ]
            
            for field in fields_to_check:
                if field in ticket:
                    value = ticket[field]
                    print(f"{field}:")
                    if isinstance(value, str):
                        print(f"  {value[:200]}")
                    else:
                        print(f"  Type: {type(value)}")
                        print(f"  Value: {str(value)[:200]}")
                    print()
            
            # Print full raw response (truncated)
            print("\n" + "="*60)
            print("Full ticket data (first 2000 chars):")
            print("="*60)
            print(json.dumps(ticket, indent=2)[:2000])
            
        else:
            print(f"Failed: {response.text}")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

def try_replies_endpoint(ticket_id):
    """Try different possible endpoints for replies/comments"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    endpoints_to_try = [
        f"/requests/{ticket_id}/replies",
        f"/requests/{ticket_id}/comments",
        f"/requests/{ticket_id}/threads",
        f"/requests/{ticket_id}/messages",
        f"/requests/{ticket_id}/history"
    ]
    
    print("\n" + "="*60)
    print("Trying alternative endpoints:")
    print("="*60 + "\n")
    
    for endpoint in endpoints_to_try:
        url = f"{SDP_API_BASE}{endpoint}"
        print(f"Trying: {endpoint}")
        
        try:
            response = requests.get(url, headers=headers)
            print(f"  Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"  SUCCESS! Found data:")
                print(f"  {json.dumps(data, indent=2)[:300]}")
            else:
                print(f"  Failed: {response.json().get('response_status', {}).get('messages', [{}])[0].get('message', 'Unknown')}")
        except Exception as e:
            print(f"  Error: {e}")
        
        print()

if __name__ == "__main__":
    ticket_id = "150534000025071271"
    
    check_ticket_detail(ticket_id)
    try_replies_endpoint(ticket_id)
