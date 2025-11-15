#!/usr/bin/env python3
"""
Test different ServiceDesk Plus API versions
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


def test_api_versions():
    print("=" * 70)
    print("Testing ServiceDesk Plus API Versions")
    print("=" * 70)
    
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    # Test different API versions and endpoints
    test_urls = [
        # API v3 (current)
        (f"https://{SDP_DOMAIN}/api/v3/requests", "API v3 - Requests"),
        
        # API v2
        (f"https://{SDP_DOMAIN}/api/v2/requests", "API v2 - Requests"),
        
        # API v1
        (f"https://{SDP_DOMAIN}/api/v1/requests", "API v1 - Requests"),
        
        # Legacy SDPAPI
        (f"https://{SDP_DOMAIN}/sdpapi/request", "Legacy SDPAPI - Request"),
        (f"https://{SDP_DOMAIN}/sdpapi/admin/request", "Legacy SDPAPI - Admin Request"),
        
        # Try without version
        (f"https://{SDP_DOMAIN}/api/requests", "API (no version) - Requests"),
        
        # Try REST API
        (f"https://{SDP_DOMAIN}/rest/api/requests", "REST API - Requests"),
        
        # Try with different auth
        (f"https://{SDP_DOMAIN}/api/v3/requests?TECHNICIAN_KEY={access_token}", "API v3 with TECHNICIAN_KEY"),
    ]
    
    print("\nTesting endpoints...\n")
    
    working_endpoints = []
    
    for url, description in test_urls:
        print(f"Testing: {description}")
        print(f"  URL: {url}")
        
        try:
            # Try GET first
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                requests_count = len(data.get('requests', []))
                print(f"  ✅ SUCCESS! Status: {response.status_code}")
                print(f"     Returned {requests_count} tickets")
                print(f"     Response keys: {list(data.keys())}")
                working_endpoints.append((description, url, requests_count))
            elif response.status_code == 404:
                print(f"  ❌ Not found (404)")
            elif response.status_code == 401:
                print(f"  ❌ Unauthorized (401)")
            elif response.status_code == 400:
                print(f"  ❌ Bad request (400)")
                print(f"     Response: {response.text[:150]}")
            else:
                print(f"  ⚠️  Status: {response.status_code}")
                print(f"     Response: {response.text[:150]}")
        except Exception as e:
            print(f"  ❌ Error: {str(e)}")
        
        print()
    
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if working_endpoints:
        print("\n✅ Working endpoints found:\n")
        for desc, url, count in working_endpoints:
            print(f"  • {desc}")
            print(f"    {url}")
            print(f"    Returns: {count} tickets\n")
    else:
        print("\n❌ No working endpoints found")
        print("\nOnly API v3 is available with 10-ticket limitation")
    
    print("=" * 70)


if __name__ == "__main__":
    test_api_versions()
