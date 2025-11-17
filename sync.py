#!/usr/bin/env python3
"""
Incremental Sync - Runs hourly to capture new/updated tickets
Fetches the 50 most recent tickets to catch new ones
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
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk-tickets')

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Token cache
_access_token = None
_token_expiry = None

# Input data for filtering
input_data = {
    "list_info": {
        "row_count": 50
    }
}

params = {
    "input_data": json.dumps(input_data)
}

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

def fetch_recent_tickets():
    """Fetch the most recent tickets"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests"
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json().get('requests', [])
        else:
            print(f"Failed to fetch tickets: {response.status_code}")
        return []
    except Exception as e:
        print(f"Error fetching tickets: {e}")
        return []

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json().get('request', {})
        else:
            print(f"Failed to fetch details for ticket {ticket_id}: {response.status_code}")
        return None
    except Exception as e:
        print(f"Error fetching ticket {ticket_id} details: {e}")
        return None

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch conversations for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json().get('conversations', [])
        else:
            print(f"Failed to fetch conversations for ticket {ticket_id}: {response.status_code}")
        return []
    except Exception as e:
        print(f"Error fetching conversations for ticket {ticket_id}: {e}")
        return []

def format_ticket_for_dust(ticket, details, conversations):
    """Format ticket data into a document for Dust"""
    
    # Build conversation text
    conversation_text = ""
    if conversations:
        conversation_text = "\n\n## Conversations\n\n"
        for conv in conversations:
            from_user = conv.get('from', {}).get('name', 'Unknown')
            created_time = conv.get('created_time', {}).get('display_value', '')
            description = conv.get('description', '')
            conversation_text += f"**{from_user}** ({created_time}):\n{description}\n\n"
    
    # Build the document text
    subject = details
