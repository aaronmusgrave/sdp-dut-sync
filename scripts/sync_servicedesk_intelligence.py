#!/usr/bin/env python3
"""Quick test to see newest tickets from API"""
import requests
import json
import os

access_token = "YOUR_TOKEN_HERE"  # Get from environment
domain = os.environ.get('SDP_DOMAIN')

url = f"https://{domain}/api/v3/requests"
headers = {
    'Authorization': f'Zoho-oauthtoken {access_token}',
    'Accept': 'application/vnd.manageengine.sdp.v3+json'
}

# Test 1: Sort by created_time
params1 = {
    'input_data': json.dumps({
        "list_info": {
            "row_count": 10,
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    })
}

print("TEST 1: Sorting by created_time (descending)")
response1 = requests.get(url, headers=headers, params=params1)
if response1.status_code == 200:
    tickets1 = response1.json().get('requests', [])
    for ticket in tickets1[:5]:
        created = ticket.get('created_time', {}).get('display_value', 'Unknown')
        subject = ticket.get('subject', 'No subject')
        print(f"  Created: {created} - {subject}")

# Test 2: Sort by last_updated_time
params2 = {
    'input_data': json.dumps({
        "list_info": {
            "row_count": 10,
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    })
}

print("\nTEST 2: Sorting by last_updated_time (descending)")
response2 = requests.get(url, headers=headers, params=params2)
if response2.status_code == 200:
    tickets2 = response2.json().get('requests', [])
    for ticket in tickets2[:5]:
        updated = ticket.get('last_updated_time', {}).get('display_value', 'Unknown')
        subject = ticket.get('subject', 'No subject')
        print(f"  Updated: {updated} - {subject}")

# Test 3: No sort (API default)
params3 = {
    'input_data': json.dumps({
        "list_info": {
            "row_count": 10
        }
    })
}

print("\nTEST 3: No sort specified (API default)")
response3 = requests.get(url, headers=headers, params=params3)
if response3.status_code == 200:
    tickets3 = response3.json().get('requests', [])
    for ticket in tickets3[:5]:
        created = ticket.get('created_time', {}).get('display_value', 'Unknown')
        subject = ticket.get('subject', 'No subject')
        print(f"  Created: {created} - {subject}")
