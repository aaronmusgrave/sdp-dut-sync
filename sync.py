#!/usr/bin/env python3
"""
Diagnostic script to check what's happening with the sync
"""

import os
import requests
import json
from datetime import datetime

# Configuration
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}"

def get_access_token():
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    if response.status_code == 200:
        return response.json()['access_token']
    else:
        print(f"❌ Failed to get ServiceDesk token: {response.status_code}")
        return None

def check_servicedesk():
    """Check ServiceDesk connection and ticket count"""
    print("=" * 60)
    print("CHECKING SERVICEDESK PLUS")
    print("=" * 60)
    
    access_token = get_access_token()
    if not access_token:
        return
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {"list_info": {"row_count": 10, "start_index": 1}}
    params = {'input_data': json.dumps(list_info)}
    
    try:
        response = requests.get(f"{SDP_API_BASE}/requests", headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            tickets = result.get('requests', [])
            list_info_resp = result.get('list_info', {})
            
            print(f"✅ ServiceDesk connection successful")
            print(f"   Total tickets available: {list_info_resp.get('total_count', 'Unknown')}")
            print(f"   Fetched sample: {len(tickets)} tickets")
            
            if tickets:
                print(f"\n   Sample ticket:")
                first = tickets[0]
                print(f"   - ID: {first.get('id')}")
                print(f"   - Subject: {first.get('subject', 'No subject')[:50]}...")
                
        else:
            print(f"❌ ServiceDesk API failed: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            
    except Exception as e:
        print(f"❌ ServiceDesk error: {e}")

def check_dust():
    """Check Dust connection and document count"""
    print("\n" + "=" * 60)
    print("CHECKING DUST")
    print("=" * 60)
    
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    
    try:
        response = requests.get(DUST_API_BASE, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            datasource = data.get('data_source', {})
            
            print(f"✅ Dust connection successful")
            print(f"   Data source name: {datasource.get('name')}")
            print(f"   Document count: {datasource.get('document_count', 0)}")
            print(f"   Data source ID: {datasource.get('data_source_id', 'Unknown')[:20]}...")
            
            doc_count = datasource.get('document_count', 0)
            
            if doc_count == 0:
                print(f"\n   ⚠️  WARNING: No documents in Dust!")
                print(f"   This means either:")
                print(f"   1. Sync hasn't run successfully yet")
                print(f"   2. Uploads are failing")
                print(f"   3. Wrong data source name")
                
        elif response.status_code == 404:
            print(f"❌ Data source not found!")
            print(f"   Data source name: '{DUST_FOLDER_NAME}'")
            print(f"   Make sure this data source exists in Dust")
            
        else:
            print(f"❌ Dust API failed: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            
    except Exception as e:
        print(f"❌ Dust error: {e}")

def test_upload():
    """Test uploading a single document to Dust"""
    print("\n" + "=" * 60)
    print("TESTING DUST UPLOAD")
    print("=" * 60)
    
    test_doc = {
        "document_id": "test-diagnostic-doc",
        "timestamp": int(datetime.now().timestamp() * 1000),
        "tags": ["test"],
        "source_url": "https://test.com",
        "text": "This is a test document to verify Dust uploads work.",
        "structured_data": {
            "test_field": "test_value"
        }
    }
    
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/documents/{test_doc['document_id']}"
    
    try:
        print(f"Uploading test document...")
        response = requests.post(url, headers=headers, json=test_doc, timeout=30)
        
        if response.status_code in [200, 201]:
            print(f"✅ Test upload successful!")
            print(f"   Status: {response.status_code}")
            print(f"   Document ID: {test_doc['document_id']}")
            
            # Try to verify it exists
            print(f"\nVerifying upload...")
            verify_response = requests.get(DUST_API_BASE, headers={'Authorization': f'Bearer {DUST_API_KEY}'})
            if verify_response.status_code == 200:
                doc_count = verify_response.json().get('data_source', {}).get('document_count', 0)
                print(f"   Document count after upload: {doc_count}")
                
        else:
            print(f"❌ Test upload failed: {response.status_code}")
            print(f"   Response: {response.text[:500]}")
            
    except Exception as e:
        print(f"❌ Upload error: {e}")

def check_github_logs():
    """Check if sync ran in GitHub Actions"""
    print("\n" + "=" * 60)
    print("GITHUB ACTIONS CHECK")
    print("=" * 60)
    
    print("Go to: https://github.com/aaronmusgrave/sdp-dut-sync/actions")
    print()
    print("Look for:")
    print("  1. Did the workflow run?")
    print("  2. What was the final output?")
    print("     - Success count")
    print("     - Error count")
    print("  3. Any error messages?")
    print()
    print("Share the final summary from the most recent run.")

def main():
    print("\n" + "=" * 60)
    print("DUST SYNC DIAGNOSTIC TOOL")
    print("=" * 60 + "\n")
    
    # Check environment variables
    print("Checking environment variables...")
    required_vars = {
        'SDP_DOMAIN': SDP_DOMAIN,
        'SDP_CLIENT_ID': SDP_CLIENT_ID,
        'SDP_CLIENT_SECRET': SDP_CLIENT_SECRET,
        'SDP_REFRESH_TOKEN': SDP_REFRESH_TOKEN,
        'DUST_API_KEY': DUST_API_KEY,
        'DUST_WORKSPACE_ID': DUST_WORKSPACE_ID,
        'DUST_FOLDER_NAME': DUST_FOLDER_NAME
    }
    
    missing = []
    for var, value in required_vars.items():
        if not value:
            missing.append(var)
            print(f"  ❌ {var}: NOT SET")
        else:
            print(f"  ✅ {var}: Set")
    
    if missing:
        print(f"\n❌ Missing variables: {', '.join(missing)}")
        print("Cannot proceed without all variables set.")
        return
    
    print()
    
    # Run checks
    check_servicedesk()
    check_dust()
    test_upload()
    check_github_logs()
    
    print("\n" + "=" * 60)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
