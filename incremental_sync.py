#!/usr/bin/env python3
"""
Incremental Sync - Runs every 15 minutes to capture new/updated tickets
Only syncs tickets created or modified since last run
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape

# Configuration from environment variables
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')  # FIXED!

# Sync window - look back 30 minutes to ensure we don't miss anything
SYNC_WINDOW_MINUTES = int(os.environ.get('SYNC_WINDOW_MINUTES', '30'))

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

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

def get_modified_since_timestamp():
    """Calculate the timestamp for modified_since filter"""
    cutoff_time = datetime.now() - timedelta(minutes=SYNC_WINDOW_MINUTES)
    timestamp_ms = int(cutoff_time.timestamp() * 1000)
    return timestamp_ms

def fetch_modified_tickets():
    """Fetch tickets modified since last sync"""
    access_token = get_access_token()
    all_tickets = []
    seen_ticket_ids = set()
    start_index = 1
    
    modified_since = get_modified_since_timestamp()
    cutoff_datetime = datetime.fromtimestamp(modified_since / 1000)
    
    print(f"Fetching tickets modified since: {cutoff_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"(Last {SYNC_WINDOW_MINUTES} minutes)")
    
    while True:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": start_index,
                "sort_field": "last_updated_time",
                "sort_order": "desc",
                "search_criteria": [
                    {
                        "field": "last_updated_time",
                        "condition": "greater than",
                        "value": modified_since
                    }
                ]
            }
        }
        
        params = {
            'input_data': json.dumps(list_info)
        }
        
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                if not tickets:
                    if len(all_tickets) == 0:
                        print(f"  No modified tickets found")
                    break
                
                new_tickets = []
                for ticket in tickets:
                    ticket_id = ticket.get('id')
                    if ticket_id not in seen_ticket_ids:
                        new_tickets.append(ticket)
                        seen_ticket_ids.add(ticket_id)
                
                if new_tickets:
                    print(f"  Found {len(new_tickets)} new tickets (batch {start_index})")
                
                all_tickets.extend(new_tickets)
                
                response_list_info = result.get('list_info', {})
                has_more = response_list_info.get('has_more_rows', False)
                
                if not has_more or len(new_tickets) == 0:
                    break
                
                start_index += 100
                time.sleep(0.3)
                
            elif response.status_code == 400:
                print("  Search criteria not supported, fetching recent tickets...")
                return fetch_recent_tickets_fallback()
            else:
                print(f"  Failed: {response.status_code}")
                print(f"  Response: {response.text[:500]}")
                break
                
        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            break
    
    if all_tickets:
        print(f"  ✓ Found {len(all_tickets)} modified tickets")
    
    return all_tickets

def fetch_recent_tickets_fallback():
    """Fallback: fetch most recent tickets without filter"""
    access_token = get_access_token()
    
    print("  Using fallback: fetching 100 most recent tickets")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    }
    
    params = {
        'input_data': json.dumps(list_info)
    }
    
    url = f"{SDP_API_BASE}/requests"
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            result = response.json()
            tickets = result.get('requests', [])
            print(f"  ✓ Fetched {len(tickets)} recent tickets")
            return tickets
    except:
        pass
    
    return []

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

def fetch_ticket_history(ticket_id, access_token):
    """Fetch ticket history (activity timeline)"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/history"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('history', [])
        return []
    except:
        return []

def strip_html(html_text):
    """Remove HTML tags and decode HTML entities"""
    if not html_text:
        return ""
    
    text = unescape(html_text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

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

def parse_sdp_timestamp(timestamp_obj):
    """Parse ServiceDesk Plus timestamp to milliseconds"""
    if not timestamp_obj:
        return int(time.time() * 1000)
    
    if isinstance(timestamp_obj, dict):
        if 'value' in timestamp_obj:
            return int(timestamp_obj['value'])
    
    return int(time.time() * 1000)

def get_display_id(details):
    """Extract the display ID (short ticket number) from ticket details"""
    if 'display_id' in details:
        display_id = details['display_id']
        if isinstance(display_id, dict):
            return display_id.get('display_value', str(display_id.get('value', details.get('id'))))
        return str(display_id)
    
    return str(details.get('id', 'Unknown'))

def format_history_timeline(history):
    """Format history into readable activity timeline"""
    if not history:
        return ""
    
    timeline = "\n\n## Activity Timeline\n\n"
    
    for item in history:
        by = item.get('by', {})
        name = by.get('name', 'Unknown')
        time_obj = item.get('time', {})
        time_str = safe_get_display_value(time_obj)
        operation = item.get('operation', 'unknown')
        description = item.get('description', '')
        
        op_display = {
            'add': 'Created',
            'edit': 'Updated',
            'reply': 'Replied',
            'close': 'Closed',
            'reopen': 'Reopened'
        }.get(operation, operation.title())
        
        timeline += f"**{time_str}** - {name} - {op_display}\n"
        
        if description and description != 'null':
            timeline += f"  _{description}_\n"
        
        diff = item.get('diff', [])
        for change in diff:
            field = change.get('field', '')
            
            if field == 'status':
                prev_val = change.get('previous_value')
                curr_val = change.get('current_value')
                
                if prev_val is None:
                    prev = 'Unknown'
                elif isinstance(prev_val, dict):
                    prev = prev_val.get('name', 'Unknown')
                else:
                    prev = str(prev_val)
                
                if curr_val is None:
                    curr = 'Unknown'
                elif isinstance(curr_val, dict):
                    curr = curr_val.get('name', 'Unknown')
                else:
                    curr = str(curr_val)
                
                timeline += f"  Status: {prev} → {curr}\n"
                
            elif field == 'closure_comments':
                comments = change.get('current_value', '')
                if comments:
                    clean_comments = strip_html(comments)
                    timeline += f"  Closure note: {clean_comments}\n"
                    
            elif field == 'priority':
                prev_val = change.get('previous_value')
                curr_val = change.get('current_value')
                
                if prev_val is None:
                    prev = 'Unknown'
                elif isinstance(prev_val, dict):
                    prev = prev_val.get('name', 'Unknown')
                else:
                    prev = str(prev_val)
                
                if curr_val is None:
                    curr = 'Unknown'
                elif isinstance(curr_val, dict):
                    curr = curr_val.get('name', 'Unknown')
                else:
                    curr = str(curr_val)
                
                timeline += f"  Priority: {prev} → {curr}\n"
        
        timeline += "\n"
    
    return timeline

def format_ticket_for_dust(ticket, details, history):
    """Format ticket data into a document for Dust with all available fields"""
    
    subject = details.get('subject', ticket.get('subject', 'No Subject'))
    status = safe_get_name(details.get('status'))
    priority = safe_get_name(details.get('priority'))
    requester = safe_get_name(details.get('requester'))
    technician = safe_get_name(details.get('technician'), 'Unassigned')
    display_id = get_display_id(details)
    
    created_time = safe_get_display_value(details.get('created_time'))
    due_by_time = safe_get_display_value(details.get('due_by_time'), 'Not set')
    resolved_time = safe_get_display_value(details.get('resolved_time'), 'Not resolved')
    completed_time = safe_get_display_value(details.get('completed_time'), 'Not completed')
    
    category = safe_get_name(details.get('category'), 'Uncategorized')
    subcategory = safe_get_name(details.get('subcategory'), 'None')
    service_category = safe_get_name(details.get('service_category'), 'None')
    
    group = safe_get_name(details.get('group'), 'Unassigned')
    site = safe_get_name(details.get('site'), 'No site')
    
    impact = safe_get_name(details.get('impact'), 'Not specified')
    urgency = safe_get_name(details.get('urgency'), 'Not specified')
    
    sla = safe_get_name(details.get('sla'), 'No SLA')
    
    is_overdue = details.get('is_overdue', False)
    is_escalated = details.get('is_escalated', False)
    is_reopened = details.get('is_reopened', False)
    is_service_request = details.get('is_service_request', False)
    has_attachments = details.get('has_attachments', False)
    
    request_type = safe_get_name(details.get('request_type'), 'Incident')
    
    raw_description = details.get('description', '')
    clean_description = strip_html(raw_description)
    
    resolution_obj = details.get('resolution')
    resolution_text = ''
    if resolution_obj and isinstance(resolution_obj, dict):
        resolution_content = resolution_obj.get('content', '')
        if resolution_content:
            resolution_text = strip_html(resolution_content)
    
    tags_list = []
    for tag in details.get('tags', []):
        if isinstance(tag, dict):
            tag_name = tag.get('name', '')
            if tag_name:
                tags_list.append(tag_name)
    tags_str = ', '.join(tags_list) if tags_list else 'None'
    
    assets_list = []
    for asset in details.get('assets', []):
        if isinstance(asset, dict):
            asset_name = asset.get('name', '')
            if asset_name:
                assets_list.append(asset_name)
    assets_str = ', '.join(assets_list) if assets_list else 'None'
    
    udf_section = ''
    udf_fields = details.get('udf_fields', {})
    if udf_fields and isinstance(udf_fields, dict):
        udf_items = []
        for key, value in udf_fields.items():
            if value:
                udf_items.append(f"- **{key}**: {value}")
        if udf_items:
            udf_section = f"\n\n## Custom Fields\n\n" + '\n'.join(udf_items)
    
    email_cc = details.get('email_cc', [])
    email_cc_str = ', '.join(email_cc) if email_cc else 'None'
    
    timeline = format_history_timeline(history)
    
    status_indicators = []
    if is_overdue:
        status_indicators.append('⚠️ OVERDUE')
    if is_escalated:
        status_indicators.append('🔺 ESCALATED')
    if is_reopened:
        status_indicators.append('🔄 REOPENED')
    if has_attachments:
        status_indicators.append('📎 Has Attachments')
    
    status_line = ' | '.join(status_indicators) if status_indicators else ''
    
    document_text = f"""# {subject}

{status_line}

## Ticket Information

**Ticket ID:** {display_id}
**Type:** {request_type}
**Status:** {status}
**Priority:** {priority}

## Classification

**Category:** {category}
**Subcategory:** {subcategory}
**Service Category:** {service_category}
**Impact:** {impact}
**Urgency:** {urgency}

## Assignment

**Requester:** {requester}
**Technician:** {technician}
**Group:** {group}
**Site:** {site}

## Timeline

**Created:** {created_time}
**Due By:** {due_by_time}
**Resolved:** {resolved_time}
**Completed:** {completed_time}

## SLA

**SLA:** {sla}

## Description

{clean_description}
"""

    if resolution_text:
        document_text += f"\n\n## Resolution\n\n{resolution_text}\n"
    
    if assets_str != 'None':
        document_text += f"\n\n## Related Assets\n\n{assets_str}\n"
    
    if tags_str != 'None':
        document_text += f"\n\n## Tags\n\n{tags_str}\n"
    
    if email_cc_str != 'None':
        document_text += f"\n\n## Email CC\n\n{email_cc_str}\n"
    
    document_text += udf_section
    document_text += timeline
    
    last_updated_time = details.get('last_updated_time', {})
    timestamp = parse_sdp_timestamp(last_updated_time)
    
    internal_id = ticket.get('id')
    
    dust_tags = [
        f"status:{status.lower().replace(' ', '-')}",
        f"priority:{priority.lower().replace(' ', '-')}",
        f"ticket-id:{display_id}",
        f"type:{request_type.lower().replace(' ', '-')}",
        f"category:{category.lower().replace(' ', '-')}",
        f"group:{group.lower().replace(' ', '-')}",
    ]
    
    if is_overdue:
        dust_tags.append('overdue')
    if is_escalated:
        dust_tags.append('escalated')
    if is_reopened:
        dust_tags.append('reopened')
    if is_service_request:
        dust_tags.append('service-request')
    if has_attachments:
        dust_tags.append('has-attachments')
    
    for tag in tags_list:
        dust_tags.append(f"custom:{tag.lower().replace(' ', '-')}")
    
    return {
        "document_id": f"ticket-{internal_id}",
        "timestamp": timestamp,
        "tags": dust_tags,
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{internal_id}",
        "text": document_text
    }

def upload_to_dust(document):
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    url = f"{DUST_API_BASE}/{document['document_id']}"
    try:
        response = requests.post(url, headers=headers, json=document)
        if response.status_code in [200, 201]:
            return True
        return False
    except:
        return False

def main():
    print("=" * 60)
    print(f"INCREMENTAL SYNC - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
                     'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"ERROR: Missing: {', '.join(missing_vars)}")
        return
    
    print(f"Sync window: {SYNC_WINDOW_MINUTES} minutes")
    print()
    
    tickets = fetch_modified_tickets()
    
    if not tickets:
        print("\n✓ No modified tickets - nothing to sync")
        print(f"Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return
    
    print(f"\nProcessing {len(tickets)} modified tickets...\n")
    
    access_token = get_access_token()
    success_count = 0
    error_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        details = fetch_ticket_details(ticket_id, access_token)
        if not details:
            error_count += 1
            continue
        
        history = fetch_ticket_history(ticket_id, access_token)
        document = format_ticket_for_dust(ticket, details, history)
        
        if upload_to_dust(document):
            success_count += 1
        else:
            error_count += 1
        
        time.sleep(0.1)
    
    print(f"{'='*60}")
    print(f"SYNC COMPLETED")
    print(f"  Success: {success_count}/{len(tickets)} tickets")
    print(f"  Errors: {error_count}")
    print(f"  Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
