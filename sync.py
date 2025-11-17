#!/usr/bin/env python3
"""
Fetch full history to see conversation content
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

def get_full_history(ticket_id):
    """Fetch full history"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/history"
    
    print(f"{'='*60}")
    print(f"Full History for ticket: {ticket_id}")
    print(f"{'='*60}\n")
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            history_items = data.get('history', [])
            
            print(f"Found {len(history_items)} history items\n")
            
            for idx, item in enumerate(history_items, 1):
                print(f"{'='*60}")
                print(f"History Item {idx}")
                print(f"{'='*60}")
                print(json.dumps(item, indent=2))
                print("\n")
            
        else:
            print(f"Failed: {response.text}")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    ticket_id = "150534000025071271"
    get_full_history(ticket_id)
