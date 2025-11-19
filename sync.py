#!/usr/bin/env python3
"""
Check if conversations have actual text content
"""

import os
import requests
import json

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

def get_access_token():
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    return response.json()['access_token']

def check_conversations():
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    ticket_id = "150534000025071271"
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        conversations = data.get('conversations', [])
        
        print(f"Found {len(conversations)} conversations\n")
        
        for idx, conv in enumerate(conversations[:3], 1):  # Check first 3
            print(f"{'='*60}")
            print(f"Conversation {idx}")
            print(f"{'='*60}")
            print(f"All keys: {list(conv.keys())}\n")
            
            # Check every field for text content
            for key, value in conv.items():
                if value and isinstance(value, str) and len(value) > 10:
                    print(f"✓ {key}: {value[:200]}...\n")
                elif value and isinstance(value, dict):
                    print(f"📦 {key} (object): {list(value.keys())}")
                    # Check nested fields
                    for nested_key, nested_value in value.items():
                        if nested_value and isinstance(nested_value, str) and len(nested_value) > 10:
                            print(f"  ✓ {key}.{nested_key}: {nested_value[:200]}...\n")

if __name__ == "__main__":
    check_conversations()
