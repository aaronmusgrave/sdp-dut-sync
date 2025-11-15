#!/usr/bin/env python3
"""
Test ManageEngine Reports API
"""
import os
import requests
from datetime import datetime, timedelta

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

def get_access_token():
    response = requests.post(ZOHO_TOKEN_URL, data={
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    })
    
    if response.status_code == 200:
        return response.json()['access_token']
    raise Exception(f"Token failed: {response.status_code}")


def test_reports_api():
    print("Testing ManageEngine Reports API")
    print("=" * 70)
    
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    # Test different Reports API endpoints
    endpoints = [
        f"https://{SDP_DOMAIN}/api/v3/reports",
        f"https://{SDP_DOMAIN}/api/v3/reports/requests",
        f"https://{SDP_DOMAIN}/api/v3/analytics/requests",
        f"https://{SDP_DOMAIN}/sdpapi/request",  # Legacy API
        f"https://{SDP_DOMAIN}/api/cmdb/ci",
    ]
    
    for endpoint in endpoints:
        print(f"\nTrying: {endpoint}")
        response = requests.get(endpoint, headers=headers)
        print(f"  Status: {response.status_code}")
        
        if response.status_code == 200:
            print(f"  ✅ SUCCESS!")
            print(f"  Response preview: {response.text[:500]}")
        elif response.status_code == 404:
            print(f"  ❌ Not found")
        else:
            print(f"  Response: {response.text[:200]}")


if __name__ == "__main__":
    test_reports_api()
