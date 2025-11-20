#!/usr/bin/env python3
"""
Full Structured Sync with Conversations - WORKING VERSION
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape

# Configuration
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Settings
TICKETS_PER_PAGE = 100
MAX_TICKETS = 10000
MAX_RETRIES = 3

# Token cache
_access_token = None
_token_expiry = None

def get_access_token(force_refresh=False):
    """Get a valid access token using refresh token"""
    global _access_token, _token_expiry
    
    if not force_refresh and _access_token and _token_expiry and datetime.now() < _token_expiry:
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
    seen_ticket_ids = set()
    start_index = 1
    
    print(f"Fetching all tickets with pagination (batches of {TICKETS_PER_PAGE})...")
    
    while len(all_tickets) < MAX_TICKETS:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": TICKETS_PER_PAGE,
                "start_index": start_index,
                "sort_field": "created_time",
                "sort_order": "desc"
            }
        }
        
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                if not tickets:
                    break
                
                batch_end = start_index + len(tickets) - 1
                print(f"  Batch {start_index}-{batch_end}: {len(tickets)} tickets")
                
                for ticket in tickets:
                    ticket_id = ticket.get('id')
                    if ticket_id not in seen_ticket_ids:
                        all_tickets.append(ticket)
                        seen_ticket_ids.add(ticket_id)
                
                response_list_info = result.get('list_info', {})
                has_more = response_list_info.get('has_more_rows', False)
                
                if not has_more or len(tickets) == 0:
                    break
                
                start_index += TICKETS_PER_PAGE
                time.sleep(0.5)
                
            else:
                print(f"  Failed: {response.status_code}")
                break
                
        except Exception as e:
            print(f"  Error: {e}")
            break
    
    print(f"\n  ✓ Fetched {len(all_tickets)} unique tickets")
    return all_tickets

def fetch_with_retry(url, headers, max_retries=MAX_RETRIES, timeout=30):
    """Fetch URL with retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 401:
                access_token = get_access_token(force_refresh=True)
                headers['Authorization'] = f'Zoho-oauthtoken {access_token}'
                continue
            elif response.status_code == 429:
                wait_time = (attempt + 1) * 5
                time.sleep(wait_time)
                continue
            else:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    time.sleep(wait_time)
                    continue
                return None
                
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
    
    return None

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket with retry"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    response = fetch_with_retry(url, headers)
    
    if response and response.status_code == 200:
        return response.json().get('request', {})
    return None

def strip_html(html_text):
    """Remove HTML tags and decode HTML entities"""
    if not html_text:
        return ""
    
    try:
        text = unescape(html_text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except:
        return str(html_text)

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
        return obj.get('display_value', default)
    return default

def get_display_id(details):
    """Extract the display ID"""
    if 'display_id' in details:
        display_id = details['display_id']
        if isinstance(display_id, dict):
            return display_id.get('display_value', str(display_id.get('value', details.get('id'))))
        return str(display_id)
    
    return str(details.get('id', 'Unknown'))

def format_ticket_for_dust(ticket, details):
    """Format ticket data into a simple document for Dust"""
    
    try:
        subject = details.get('subject', ticket.get('subject', 'No Subject'))
        status = safe_get_name(details.get('status'))
        priority = safe_get_name(details.get('priority'))
        requester = safe_get_name(details.get('requester'))
        technician = safe_get_name(details.get('technician'), 'Unassigned')
        display_id = get_display_id(details)
        
        created_time = safe_get_display_value(details.get('created_time'))
        category = safe_get_name(details.get('category'), 'Uncategorized')
        group = safe_get_name(details.get('group'), 'Unassigned')
        
        raw_description = details.get('description', '')
        clean_description = strip_html(raw_description)
        
        is_overdue = details.get('is_overdue', False)
        is_escalated = details.get('is_escalated', False)
        
        internal_id = ticket.get('id')
        
        document_text = f"""# {subject}

**Ticket ID:** {display_id}
**Status:** {status}
**Priority:** {priority}

**Requester:** {requester}
**Technician:** {technician}
**Group:** {group}
**Category:** {category}

**Created:** {created_time}

## Description

{clean_description}
"""
        
        dust_tags = [
            f"status:{status.lower().replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}",
            f"ticket-id:{display_id}",
            f"category:{category.lower().replace(' ', '-')}",
            f"group:{group.lower().replace(' ', '-')}",
        ]
        
        if is_overdue:
            dust_tags.append('overdue')
        if is_escalated:
            dust_tags.append('escalated')
        
        return {
            "document_id": f"ticket-{internal_id}",
            "timestamp": int(time.time() * 1000),
            "tags": dust_tags,
            "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{internal_id}",
            "text": document_text
        }
    except Exception as e:
        print(f"      Error formatting ticket: {e}")
        return None

def upload_to_dust(document):
    if not document:
        return False
    
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    url = f"{DUST_API_BASE}/{document['document_id']}"
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code in [200, 201]:
                return True
            elif attempt < MAX_RETRIES - 1:
                time.sleep(2)
                continue
            else:
                print(f"      Upload failed: {response.status_code} - {response.text[:100]}")
            return False
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
                continue
            else:
                print(f"      Upload exception: {e}")
            return False
    
    return False

def main():
    print("=" * 60)
    print(f"FULL SYNC - Starting at {datetime.now()}")
    print("=" * 60)
    
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
                     'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"ERROR: Missing: {', '.join(missing_vars)}")
        return
    
    print(f"Configuration:")
    print(f"  SDP Domain: {SDP_DOMAIN}")
    print(f"  Dust Workspace: {DUST_WORKSPACE_ID}")
    print(f"  Dust Data Source: {DUST_FOLDER_NAME}")
    print()
    
    tickets = fetch_all_tickets()
    print(f"\n{'='*60}")
    print(f"Total unique tickets to process: {len(tickets)}")
    print(f"{'='*60}\n")
    
    if not tickets:
        print("No tickets to process")
        return
    
    access_token = get_access_token()
    success_count = 0
    error_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        if idx % 100 == 0 or idx == 1:
            print(f"[{idx}/{len(tickets)}] Processing tickets...")
        
        try:
            details = fetch_ticket_details(ticket_id, access_token)
            if not details:
                error_count += 1
                if idx <= 10:  # Show first 10 errors
                    print(f"    ✗ Failed to get details for {ticket_id}")
                continue
            
            document = format_ticket_for_dust(ticket, details)
            
            if document and upload_to_dust(document):
                success_count += 1
                if idx % 100 == 0:
                    print(f"  ✓ {success_count} uploaded, {error_count} errors")
            else:
                error_count += 1
                if idx <= 10:  # Show first 10 errors
                    print(f"    ✗ Failed to upload {ticket_id}")
            
        except Exception as e:
            error_count += 1
            if idx <= 10:
                print(f"    ✗ Exception for {ticket_id}: {str(e)[:50]}")
            continue
        
        time.sleep(0.5)
        
        if idx % 250 == 0:
            access_token = get_access_token(force_refresh=True)
            print(f"\n  ℹ️  CHECKPOINT at ticket {idx}/{len(tickets)}")
            print(f"     Success: {success_count}, Errors: {error_count}")
            print(f"     Time: {datetime.now().strftime('%H:%M:%S')}\n")
    
    print(f"\n{'='*60}")
    print(f"FULL SYNC COMPLETED")
    print(f"  Success: {success_count}/{len(tickets)} tickets")
    print(f"  Errors: {error_count}")
    print(f"  Success rate: {(success_count/len(tickets)*100):.1f}%")
    print(f"  Finished at: {datetime.now()}")
    print(f"{'='*60}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
