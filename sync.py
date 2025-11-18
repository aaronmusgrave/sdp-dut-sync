#!/usr/bin/env python3
"""
Full Sync - Initial import of all tickets with comprehensive field coverage
Handles pagination to fetch all tickets from ServiceDesk Plus
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape

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

# Pagination settings
TICKETS_PER_PAGE = 100
MAX_TICKETS = 10000  # Increased to ensure we get all tickets

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
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def fetch_all_tickets():
    """Fetch all tickets with pagination using start_index"""
    access_token = get_access_token()
    all_tickets = []
    seen_ticket_ids = set()
    start_index = 1
    
    print(f"Fetching all tickets with pagination (batches of {TICKETS_PER_PAGE})...")
    
    while len(all_tickets) < MAX_TICKETS:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": TICKETS_PER_PAGE,
                "start_index": start_index,
                "sort_field": "created_time",
                "sort_order": "desc"
            }
        }
        
        params = {
            'input_data': json.dumps(list_info)
        }
        
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                if not tickets:
                    print(f"  No more tickets. Total unique: {len(all_tickets)}")
                    break
                
                # Show date range for this batch
                if tickets:
                    first_created = tickets[0].get('created_time', {})
                    last_created = tickets[-1].get('created_time', {})
                    first_date = first_created.get('display_value', 'Unknown') if isinstance(first_created, dict) else 'Unknown'
                    last_date = last_created.get('display_value', 'Unknown') if isinstance(last_created, dict) else 'Unknown'
                    print(f"  Batch {start_index}-{start_index+len(tickets)-1}: {len
