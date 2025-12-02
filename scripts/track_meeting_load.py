#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
FINAL v6: Uses search with date filtering, doesn't fetch individual tickets
"""

import os
import requests
import json
import argparse
from datetime import datetime, timedelta

parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD', help='Process specific date')
parser.add_argument('--debug', action='store_true', help='Show debug output')

TENANT_ID = os.environ.get('AZURE_TENANT_ID')
CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')

WORK_HOURS_PER_DAY = 8
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'motorad.com')

DEBUG = False

def debug_print(message):
    if DEBUG:
        print(f"  [DEBUG] {message}")

def get_graph_token():
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
    raise Exception(f"Failed to get Graph token")

def get_sdp_token():
    print("Getting ServiceDesk token...")
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
    if response.status_code == 200:
        print("✓ ServiceDesk token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get SDP token")

def discover_active_technicians(sdp_token):
    """Discover technicians with recent activity only"""
    print("\n🔍 Auto-discovering technicians...")
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Get tickets updated in last 30 days
    thirty_days_ago = datetime.now() - timedelta(days=30)
    thirty_days_ms = int(thirty_days_ago.timestamp() * 1000)
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "status.name",
                    "condition": "is not",
                    "values": ["Closed", "Cancelled"]
                },
                {
                    "field": "last_updated_time",
                    "condition": "is after",
                    "value": str(thirty_days_ms)
                }
            ]
        }
    }
    
    technicians = {}
    start_index = 1
    pages_checked = 0
    
    while pages_checked < 20:
        list_info['list_info']['start_index'] = start_index
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                if not tickets:
                    break
                for ticket in tickets:
                    tech = ticket.get('technician')
                    if tech and isinstance(tech, dict):
                        tech_name = tech.get('name')
                        tech_email = tech.get('email_id')
                        if tech_name and tech_name != 'Unassigned':
                            if tech_name not in technicians:
                                technicians[tech_name] = {
                                    'email': tech_email,
                                    'id': tech.get('id'),
                                    'ticket_count': 0
                                }
                            technicians[tech_name]['ticket_count
