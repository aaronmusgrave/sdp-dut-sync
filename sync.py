#!/usr/bin/env python3
"""
Full Sync - Initial import of all tickets
Handles pagination to fetch all tickets from ServiceDesk Plus
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time

# Configuration from environment variables
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk-tickets')

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Pagination settings
TICKETS_PER_PAGE = 10
MAX_TICKETS = 5000

# Token cache
_access_token = None
_token_expiry = None

def get_access_token():
    """Get a valid access token using refresh token"""
    global _access_token, _token_expiry
    
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    print("Getting access token...")
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        data = response.json()
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def fetch_all_tickets():
    """Fetch all tickets with pagination"""
    access_token = get_access_token()
    all_tickets = []
    start_index = 1
    
    print(f"Fetching all tickets with pagination...")
    
    while len(all_tickets) < MAX_TICKETS:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "sort_field": "id",
                "sort_order": "asc"
            }
        }
        
        data = {
            'input_data': json.dumps(list_info)
        }
        
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, data=data)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                response_list_info = result.get('list_info', {})
                print(f"  Page {start_index}: got {len(tickets)} tickets")
                print(f"    Response list_info: {response_list_info}")
                
                if not tickets:
                    print(f"  No more tickets. Total: {len(all_tickets)}")
                    break
                
                all_tickets.extend(tickets)
                
                has_more = response_list_info.get('has_more_rows', False)
                total_count = response_list_info.get('row_count', 0)
                
                print(f"    Total so far: {len(all_tickets)}, has_more: {has_more}, total_count: {total_count}")
                
                if not has_more:
                    print(f"  API says no more rows. Total: {len(all_tickets)}")
                    break
                
                start_index += len(tickets)
                time.sleep(0.5)
                
            else:
                print(f"  Failed: {response.status_code}")
                print(f"  Response: {response.text[:500]}")
                break
                
        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            break
    
    # Check for duplicates
    ticket_ids = [t.get('id') for t in all_tickets]
    unique_ids = set(ticket_ids)
    if len(ticket_ids) != len(unique_ids):
        duplicates_count = len(ticket_ids) - len(unique_ids)
        print(f"\n  ⚠️  WARNING: Found {duplicates_count} duplicate tickets!")
        print(f"  Total fetched: {len(ticket_ids)}, Unique: {len(unique_ids)}")
    else:
        print(f"\n  ✓ All {len(ticket_ids)} tickets are unique (no duplicates)")
    
    return all_tickets

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('request', {})
        return None
    except:
        return None

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch conversations for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('conversations', [])
        return []
    except:
        return []

def safe_get_name(obj, default='Unknown'):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('name', default)
    return default

def safe_get_display_value(obj, default=''):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get('display_value', default
