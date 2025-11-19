#!/usr/bin/env python3
"""
Fetch a specific notification to see if it has text content
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

def test_specific_notification():
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    ticket_id = "150534000025071271"
    
    # First get list of notifications
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        notifications = data.get('notifications', [])
        
        print(f"Found {len(notifications)} notifications\n")
        
        # Try to get details for first few notifications
        for idx, notif in enumerate(notifications[:3], 1):
            notif_id = notif.get('id')
            notif_type = notif.get('type', 'Unknown')
            
            print(f"{'='*60}")
            print(f"Notification {idx} - Type: {notif_type}")
            print(f"ID: {notif_id}")
            print(f"{'='*60}\n")
            
            # Get specific notification details
            detail
