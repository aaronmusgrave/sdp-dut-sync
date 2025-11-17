#!/usr/bin/env python3
"""
Debug script to inspect conversation data
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

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

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
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def debug_ticket_conversations(ticket_id):
    """Fetch and display raw conversation data"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    print(f"\n{'='*60}")
    print(f"Fetching conversations for ticket: {ticket_id}")
    print(f"{'='*60}\n")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}\n")
        
        if response.status_code == 200:
            data = response.json()
            conversations = data.get('conversations', [])
            
            print(f"Found {len(conversations)} conversations\n")
            
            for idx, conv in enumerate(conversations, 1):
                print(f"--- Conversation {idx} ---")
                print(f"Keys available: {list(conv.keys())}")
                print(f"\nFrom: {conv.get('from')}")
                print(f"Created Time: {conv.get('created_time')}")
                print(f"Description: {conv.get('description', 'NO DESCRIPTION')[:200]}")
                print(f"Text: {conv.get('text', 'NO TEXT')[:200]}")
                
                # Print full raw data for first conversation
                if idx == 1:
                    print(f"\nFull raw data for conversation 1:")
                    print(json.dumps(conv, indent=2)[:1000])
                
                print("\n")
        else:
            print(f"Failed to fetch conversations: {response.text}")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Use the ticket ID from your example
    ticket_id = "150534000025071271"
    debug_ticket_conversations(ticket_id)
