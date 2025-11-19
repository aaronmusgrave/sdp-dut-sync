#!/usr/bin/env python3
"""
Debug why sync is stopping at 1300 tickets
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

_access_token = None
_token_expiry = None

def get_access_token():
    global _access_token, _token_expiry
    
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    if response.status_code == 200:
        data = response.json()
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def debug_pagination():
    """Debug pagination to see where it stops"""
    access_token = get_access_token()
    
    start_index = 1
    total_fetched = 0
    batch_num = 1
    
    print("Starting pagination debug...\n")
    
    while total_fetched < 5000:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "sort_field": "created_time",
                "sort_order": "desc"
            }
        }
        
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        
        print(f"Batch {batch_num} - start_index: {start_index}")
        
        try:
            response = requests.get(url, headers=headers, params=params)
            
            print(f"  Status: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                list_info_response = result.get('list_info', {})
                
                print(f"  Tickets returned: {len(tickets)}")
                print(f"  has_more_rows: {list_info_response.get('has_more_rows')}")
                print(f"  row_count: {list_info_response.get('row_count')}")
                
                if tickets:
                    total_fetched += len(tickets)
                    print(f"  Total fetched so far: {total_fetched}")
                    
                    # Show date range
                    first = tickets[0].get('created_time', {})
                    last = tickets[-1].get('created_time', {})
                    first_date = first.get('display_value', 'Unknown') if isinstance(first, dict) else 'Unknown'
                    last_date = last.get('display_value', 'Unknown') if isinstance(last, dict) else 'Unknown'
                    print(f"  Date range: {first_date} to {last_date}")
                else:
                    print("  ✗ No tickets returned - stopping")
                    break
                
                has_more = list_info_response.get('has_more_rows', False)
                
                if not has_more:
                    print(f"  ✗ API says no more rows - stopping")
                    print(f"\nFinal count: {total_fetched} tickets")
                    break
                
                print()  # Blank line between batches
                
                start_index += 100
                batch_num += 1
                time.sleep(0.5)
                
            elif response.status_code == 401:
                print("  ✗ Token expired, getting new token...")
                access_token = get_access_token()
                continue
                
            else:
                print(f"  ✗ Failed with status {response.status_code}")
                print(f"  Response: {response.text[:500]}")
                break
                
        except Exception as e:
            print(f"  ✗ Exception: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"\n{'='*60}")
    print(f"Debug complete. Total tickets fetched: {total_fetched}")
    print(f"{'='*60}")

if __name__ == "__main__":
    debug_pagination()
