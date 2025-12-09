#!/usr/bin/env python3
"""Test what Azure AD actually returns for user profiles"""

import os
import requests
import json

def clean_credential(value):
    if not value:
        return value
    return value.strip().strip('"').strip("'").replace('\\', '').strip()

TENANT_ID = clean_credential(os.environ.get('AZURE_TENANT_ID'))
CLIENT_ID = clean_credential(os.environ.get('AZURE_CLIENT_ID'))
CLIENT_SECRET = clean_credential(os.environ.get('AZURE_CLIENT_SECRET'))

# Get token
print("Getting Graph token...")
url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
data = {
    'client_id': CLIENT_ID,
    'client_secret': CLIENT_SECRET,
    'scope': 'https://graph.microsoft.com/.default',
    'grant_type': 'client_credentials'
}

response = requests.post(url, data=data, timeout=30)
if response.status_code != 200:
    print(f"❌ Token failed: {response.status_code}")
    print(response.text)
    exit(1)

token = response.json()['access_token']
print("✓ Token obtained\n")

# Test user profiles
test_users = [
    'aaron.musgrave@motorad.com',
    'austin.rodriguez@motorad.com'
]

for email in test_users:
    print(f"{'='*70}")
    print(f"Testing: {email}")
    print(f"{'='*70}")
    
    # Try different API approaches
    
    # Approach 1: Standard endpoint with \$select
    print("\n1. Standard endpoint with \$select:")
    url = f"https://graph.microsoft.com/v1.0/users/{email}"
    headers = {
        'Authorization': f'Bearer {token}',
        'ConsistencyLevel': 'eventual'
    }
    params = {
        '\$select': 'displayName,jobTitle,department,officeLocation,mail,givenName,surname'
    }
    
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(json.dumps(data, indent=2))
    else:
        print(f"Error: {response.text[:500]}")
    
    # Approach 2: Without \$select (get all fields)
    print("\n2. Without \$select (all fields):")
    response = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{email}",
        headers=headers,
        timeout=30
    )
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        # Show relevant fields
        relevant_fields = ['displayName', 'jobTitle', 'department', 'officeLocation', 'mail', 'userPrincipalName']
        print("Relevant fields:")
        for field in relevant_fields:
            print(f"  {field}: {data.get(field)}")
    else:
        print(f"Error: {response.text[:500]}")
    
    print()
