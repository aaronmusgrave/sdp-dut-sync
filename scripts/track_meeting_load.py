#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
AUTO-DISCOVERS technicians from ServiceDesk ticket assignments
PRIVACY MODE: Meeting titles hidden
SUPPORTS: 365-day historical backfill
"""

import os
import sys
import argparse
import requests
import json
import time
from datetime import datetime, timedelta
from collections import defaultdict

# Parse arguments first
parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--backfill', type=int, metavar='DAYS', 
                    help='Backfill historical data for the last N days (max 365)')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD',
                    help='Process specific date (format: 2025-11-25)')

# Microsoft Graph API credentials
TENANT_ID = os.environ.get('AZURE_TENANT_ID')
CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

# ServiceDesk credentials
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')

WORK_HOURS_PER_DAY = 8
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

# Email domain for your organization
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'motorad.com')

def get_graph_token():
    """Get Microsoft Graph API access token"""
    print("Getting Microsoft Graph token...")
    
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    
    response = requests.post(url, data=data)
    
    if response.status_code == 200:
        print("✓ Graph token obtained")
        return response.json()['access_token']
    else:
        raise Exception(f"Failed to get Graph token: {response.status_code}")

def get_sdp_token():
    """Get ServiceDesk Plus access token"""
    print("Getting ServiceDesk token...")
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        print("✓ ServiceDesk token obtained")
        return response.json()['access_token']
    else:
        raise Exception(f"Failed to get SDP token: {response.status_code}")

def discover_active_technicians(sdp_token):
    """Auto-discover technicians from ServiceDesk by finding all unique technicians assigned to active tickets"""
    print("\n🔍 Auto-discovering technicians from ServiceDesk...")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "status.name",
                    "condition": "is not",
                    "values": ["Closed", "Resolved", "Cancelled"]
                }
            ]
        }
    }
    
    technicians = {}
    start_index = 1
    pages_checked = 0
    max_pages = 20
    
    while pages_checked < max_pages:
        list_info['list_info']['start_index'] = start_index
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
