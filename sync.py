#!/usr/bin/env python3
"""
Sync ServiceDesk Plus On-Demand tickets to Dust
Runs automatically via GitHub Actions
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


def get_access_token():
    """Get a valid access token using refresh token"""
    global _access_token, _token_expiry
    
    # Check if cached token is still valid
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    print("🔑 Getting access token...")
    
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
            raise Exception(f"No access_token in response: {data}")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print(f"✅ Access token obtained (valid for {expires_in}s)")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code} - {response.text}")


def fetch_sdp_tickets(limit=100, status_filter=None):
    """Fetch tickets from SDPOnDemand"""
    print(f"📥 Fetching tickets from SDPOnDemand...")
    
    access_token = get_access_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}'
    }
    
    # Simplified list_info - only essential parameters
    list_info = {
        'row_count': limit
    }
    
    if status_filter:
        list_info['filter_by'] = {'status': {'name': status_filter}}
    
    # Format the input_data correctly for SDPOnDemand API v3
    input_data = json.dumps({'list_info': list_info})
    
    # Use form data
    data = {
        'input_data': input_data
    }
    
    url = f"{SDP_API_BASE}/requests"
    print(f"   Calling: {url}")
    print(f"   Input data: {input_data}")
    
    # Use POST for SDPOnDemand API v3
    response = requests.post(url, headers=headers, data=data)
    
    print(f"   Response status: {response.status_code}")
    
    if response.status_code == 200:
        response_data = response.json()
        tickets = response_data.get('requests', [])
        print(f"✅ Fetched {len(tickets)} tickets")
        return tickets
    else:
        print(f"   Response body: {response.text}")
        raise Exception(f"Failed to fetch tickets: {response.status_code} - {response.text}")


def format_ticket_for_dust(ticket):
    """Format ticket as markdown for Dust"""
    ticket_id = ticket.get('id', 'Unknown')
    subject = ticket.get('subject', 'No Subject')
    status = ticket.get('status', {}).get('name', 'Unknown')
    priority = ticket.get('priority', {}).get('name', 'Unknown')
    requester = ticket.get('requester', {}).get('name', 'Unknown')
    requester_email = ticket.get('requester', {}).get('email_id', '')
    technician = ticket.get('technician', {}).get('name', 'Unassigned')
    created_time = ticket.get('created_time', {}).get('display_value', 'Unknown')
    due_by_time = ticket.get('due_by_time', {}).get('display_value',
