#!/usr/bin/env python3
"""
Debug script to inspect conversation details
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

# API Endpoints
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

def debug_conversation_detail(ticket_id, conversation_id):
    """Fetch detailed information for a single conversation"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations/{conversation_id}"
    
    print(f"Fetching conversation detail: {conversation_id}")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}\n")
        
        if response.status_code == 200:
            data = response.json()
            print("Full conversation detail:")
            print(json.dumps(data, indent=2))
        else:
            print(f"Failed: {response.text}\n")
            
    except Exception as e:
        print(f"Error: {e}\n")

def debug_notes(ticket_id):
    """Try fetching notes instead of conversations"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notes"
    
    print(f"\n{'='*60}")
    print(f"Trying to fetch NOTES for ticket: {ticket_id}")
    print(f"{'='*60}\n")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}\n")
        
        if response.status_code == 200:
            data = response.json()
            notes = data.get('notes', [])
            
            print(f"Found {len(notes)} notes\n")
            
            for idx, note in enumerate(notes, 1):
                print(f"--- Note {idx} ---")
                print(f"Keys: {list(note.keys())}")
                print(json.dumps(note, indent=2)[:500])
                print("\n")
        else:
            print(f"Failed: {response.text}\n")
            
    except Exception as e:
        print(f"Error: {e}\n")

def debug_resolutions(ticket_id):
    """Try fetching resolutions"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/resolutions"
    
    print(f"\n{'='*60}")
    print(f"Trying to fetch RESOLUTIONS for ticket: {ticket_id}")
    print(f"{'='*60}\n")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}\n")
        
        if response.status_code == 200:
            data = response.json()
            print(json.dumps(data, indent=2))
        else:
            print(f"Failed: {response.text}\n")
            
    except Exception as e:
        print(f"Error: {e}\n")

if __name__ == "__main__":
    ticket_id = "150534000025071271"
    conversation_id = "150534000025100105"  # First conversation from previous output
    
    # Try getting conversation detail
    debug_conversation_detail(ticket_id, conversation_id)
    
    # Try getting notes
    debug_notes(ticket_id)
    
    # Try getting resolutions
    debug_resolutions(ticket_id)
