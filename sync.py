#!/usr/bin/env python3
"""
Test ManageEngine conversation endpoints to see what data is available
"""

import os
import requests
import json

# Configuration
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

def test_endpoints(ticket_id):
    """Test all conversation-related endpoints"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    endpoints = {
        'notifications': f"/requests/{ticket_id}/notifications",
        'notes': f"/requests/{ticket_id}/notes",
        'conversations': f"/requests/{ticket_id}/conversations"
    }
    
    results = {}
    
    for name, endpoint in endpoints.items():
        url = f"{SDP_API_BASE}{endpoint}"
        
        print(f"\n{'='*60}")
        print(f"Testing: {name.upper()}")
        print(f"URL: {url}")
        print(f"{'='*60}\n")
        
        try:
            response = requests.get(url, headers=headers)
            print(f"Status Code: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                results[name] = data
                
                # Print structure
                print(f"\nResponse Keys: {list(data.keys())}")
                
                # Get the main data array
                items_key = name  # 'notifications', 'notes', or 'conversations'
                items = data.get(items_key, [])
                
                print(f"Number of items: {len(items)}")
                
                if items:
                    print(f"\n--- First Item Structure ---")
                    first_item = items[0]
                    print(f"Keys available: {list(first_item.keys())}")
                    
                    # Print full first item (truncated)
                    print(f"\n--- First Item Content (first 1000 chars) ---")
                    print(json.dumps(first_item, indent=2)[:1000])
                    
                    # Look for text/description fields
                    print(f"\n--- Checking for Text Content ---")
                    text_fields = ['description', 'text', 'content', 'body', 'message', 'note_text']
                    for field in text_fields:
                        if field in first_item:
                            value = first_item[field]
                            if value:
                                print(f"✓ Found '{field}': {str(value)[:200]}")
                            else:
                                print(f"✗ Field '{field}' exists but is empty/null")
                    
                    # Check nested objects
                    for key, value in first_item.items():
                        if isinstance(value, dict):
                            print(f"\nNested object '{key}': {list(value.keys())}")
                else:
                    print("No items found in response")
            else:
                print(f"Failed: {response.text[:500]}")
                results[name] = None
                
        except Exception as e:
            print(f"Error: {e}")
            results[name] = None
    
    return results

def test_specific_notification(ticket_id, notification_id):
    """Test getting a specific notification"""
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications/{notification_id}"
    
    print(f"\n{'='*60}")
    print(f"Testing: SPECIFIC NOTIFICATION")
    print(f"URL: {url}")
    print(f"{'='*60}\n")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("\nFull Response:")
            print(json.dumps(data, indent=2))
        else:
            print(f"Failed: {response.text[:500]}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # Test with ticket that has conversations
    ticket_id = "150534000025071271"  # The ticket we know has 10 conversations
    
    print("="*60)
    print("TESTING CONVERSATION ENDPOINTS")
    print(f"Ticket ID: {ticket_id}")
    print("="*60)
    
    results = test_endpoints(ticket_id)
    
    # If we found notifications, test getting a specific one
    if results.get('notifications'):
        notifications = results['notifications'].get('notifications', [])
        if notifications:
            first_notification_id = notifications[0].get('id')
            if first_notification_id:
                test_specific_notification(ticket_id, first_notification_id)
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    for endpoint, data in results.items():
        if data:
            items = data.get(endpoint, [])
            print(f"✓ {endpoint}: Found {len(items)} items")
        else:
            print(f"✗ {endpoint}: Failed or empty")
