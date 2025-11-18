#!/usr/bin/env python3
"""
Debug version to see what's happening with pagination
"""

import os
import requests
import json
from datetime import datetime, timedelta

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
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def debug_pagination():
    access_token = get_access_token()
    
    # Try fetching first 3 pages
    for page in range(1, 4):
        start_index = 1 + (page - 1) * 100
        
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "sort_field": "created_time",
                "sort_order": "desc"
            }
        }
        
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        
        print(f"\n{'='*60}")
        print(f"Page {page} (start_index={start_index})")
        print(f"{'='*60}")
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            result = response.json()
            tickets = result.get('requests', [])
            list_info_response = result.get('list_info', {})
            
            print(f"Status: {response.status_code}")
            print(f"Tickets returned: {len(tickets)}")
            print(f"has_more_rows: {list_info_response.get('has_more_rows')}")
            print(f"row_count: {list_info_response.get('row_count')}")
            
            if tickets:
                # Get created times
                first = tickets[0]
                last = tickets[-1]
                print(f"\nFirst ticket: ID {first.get('id')}")
                print(f"Last ticket: ID {last.get('id')}")
                
                # Try to get created time if available
                for idx, t in enumerate([first, last]):
                    label = "First" if idx == 0 else "Last"
                    created = t.get('created_time', {})
                    if isinstance(created, dict):
                        print(f"{label} created: {created.get('display_value', 'Unknown')}")
        else:
            print(f"Failed: {response.status_code}")
            print(response.text[:500])

if __name__ == "__main__":
    debug_pagination()
