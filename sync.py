#!/usr/bin/env python3
"""
Full Sync - Initial import of all tickets with comprehensive field coverage + conversations
Handles pagination to fetch all tickets from ServiceDesk Plus with robust error handling
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
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Pagination settings
TICKETS_PER_PAGE = 100
MAX_TICKETS = 10000

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
    """Fetch all tickets with pagination using start_index"""
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
        
        params = {
            'input_data': json.dumps(list_info)
        }
        
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                if not tickets:
                    print(f"  No more tickets. Total: {len(all_tickets)}")
                    break
                
                if tickets:
                    first_created = tickets[0].get('created_time', {})
                    last_created = tickets[-1].get('created_time', {})
                    first_date = first_created.get('display_value', 'Unknown') if isinstance(first_created, dict) else 'Unknown'
                    last_date = last_created.get('display_value', 'Unknown') if isinstance(last_created, dict) else 'Unknown'
                    batch_end = start_index + len(tickets) - 1
                    print(f"  Batch {start_index}-{batch_end}: {len(tickets)} tickets ({first_date} to {last_date})")
                
                new_tickets = []
                duplicate_count = 0
                
                for ticket in tickets:
                    ticket_id = ticket.get('id')
                    if ticket_id not in seen_ticket_ids:
                        new_tickets.append(ticket)
                        seen_ticket_ids.add(ticket_id)
                    else:
                        duplicate_count += 1
                
                if duplicate_count > 0:
                    print(f"    Warning: {duplicate_count} duplicates")
                
                all_tickets.extend(new_tickets)
                
                response_list_info = result.get('list_info', {})
                has_more = response_list_info.get('has_more_rows', False)
                
                print(f"    Total: {len(all_tickets)}, Has more: {has_more}")
                
                if not has_more:
                    print(f"  Reached end. Total: {len(all_tickets)}")
                    break
                
                if len(new_tickets) == 0:
                    print(f"  No new tickets. Total: {len(all_tickets)}")
                    break
                
                start_index += TICKETS_PER_PAGE
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
    
    print(f"\n  ✓ Fetched {len(all_tickets)} unique tickets")
    return all_tickets

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json().get('request', {})
        return None
    except Exception as e:
        print(f"      Error fetching details: {e}")
        return None

def fetch_ticket_history(ticket_id, access_token):
    """Fetch ticket history (activity timeline)"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/history"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json().get('history', [])
        return []
    except Exception as e:
        print(f"      Error fetching history: {e}")
        return []

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch ticket conversations with full text content - with robust error handling"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return []
        
        notifications = response.json().get('notifications', [])
        conversations = []
        
        # Limit to first 20 notifications to avoid timeout
        for notif in notifications[:20]:
            notif_id = notif.get('id')
            notif_type = notif.get('type', '')
            
            # Focus on actual conversations/replies
            if any(keyword in notif_type.lower() for keyword in ['conversation', 'reply', 'reqreply', 'technician']):
                try:
                    detail_url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications/{notif_id}"
                    detail_response = requests.get(detail_url, headers=headers, timeout=10)
                    
                    if detail_response.status_code == 200:
                        detail_data = detail_response.json()
                        notification = detail_data.get('notification', {})
                        
                        description = notification.get('description', '')
                        if description and len(description.strip()) > 0:
                            sender = notification.get('sender', {})
                            time_obj = notification.get('time', {})
                            
                            conversations.append({
                                'id': notif_id,
                                'type': notif_type,
                                'sender': sender.get('name', 'Unknown'),
                                'time': time_obj.get('display_value', 'Unknown'),
                                'description': description,
                                'is_public': notification.get('is_public', True)
                            })
                    
                    time.sleep(0.1)
                    
                except Exception as e:
                    # Skip this notification if it fails, continue with others
                    continue
        
        return conversations
        
    except Exception as e:
        # If entire conversation fetch fails, return empty and continue
        return []

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

def format_conversations(conversations):
    """Format conversations into readable text"""
    if not conversations:
        return ""
    
    conv_text = "\n\n## Conversations & Replies\n\n"
    conv_text += f"Total conversations: {len(conversations)}\n\n"
    
    for idx, conv in enumerate(conversations, 1):
        try:
            sender = conv.get('sender', 'Unknown')
            time_str = conv.get('time', 'Unknown time')
            conv_type = conv.get('type', 'Unknown')
            description = conv.get('description', '')
            is_public = conv.get('is_public', True)
            
            clean_text = strip_html(description)
            
            if len(clean_text.strip()) < 10:
                continue
            
            visibility = "Public" if is_public else "Private"
            
            conv_text += f"### Conversation {idx}\n"
            conv_text += f"**From:** {sender}\n"
            conv_text += f"**Time:** {time_str}\n"
            conv_text += f"**Type:** {conv_type} ({visibility})\n\n"
            conv_text += f"{clean_text}\n\n"
            conv_text += "---\n\n"
        except Exception as e:
            continue
    
    return conv_text

def format_history_timeline(history):
    """Format history into readable activity timeline"""
    if not history:
        return ""
    
    timeline = "\n\n## Activity Timeline\n\n"
    
    for item in history:
        try:
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
        except Exception as e:
            continue
    
    return timeline

def format_ticket_for_dust(ticket, details, history, conversations):
    """Format ticket data into a document for Dust with all available fields"""
    
    try:
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
        
        conversations_text = format_conversations(conversations)
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
        
        document_text += conversations_text
        
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
        if conversations:
            dust_tags.append('has-conversations')
        
        for tag in tags_list:
            dust_tags.append(f"custom:{tag.lower().replace(' ', '-')}")
        
        return {
            "document_id": f"ticket-{internal_id}",
            "timestamp": timestamp,
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
    try:
        response = requests.post(url, headers=headers, json=document, timeout=30)
        if response.status_code in [200, 201]:
            return True
        return False
    except Exception as e:
        return False

def main():
    print("=" * 60)
    print(f"FULL SYNC WITH CONVERSATIONS - Starting at {datetime.now()}")
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
    conv_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        if idx % 50 == 0 or idx == 1:
            print(f"[{idx}/{len(tickets)}] Processing tickets...")
        
        try:
            details = fetch_ticket_details(ticket_id, access_token)
            if not details:
                error_count += 1
                if idx % 50 == 0:
                    print(f"    ✗ Failed to get details for ticket {ticket_id}")
                continue
            
            history = fetch_ticket_history(ticket_id, access_token)
            conversations = fetch_ticket_conversations(ticket_id, access_token)
            
            if conversations:
                conv_count += 1
            
            document = format_ticket_for_dust(ticket, details, history, conversations)
            
            if document and upload_to_dust(document):
                success_count += 1
                if idx % 50 == 0:
                    print(f"  ✓ {success_count} uploaded, {conv_count} with conversations, {error_count} errors")
            else:
                error_count += 1
                if idx % 50 == 0:
                    print(f"    ✗ Failed to upload ticket {ticket_id}")
            
        except Exception as e:
            error_count += 1
            if idx % 50 == 0:
                print(f"    ✗ Exception processing ticket {ticket_id}: {str(e)[:100]}")
            continue
        
        time.sleep(0.3)
        
        # Refresh token and show checkpoint every 500 tickets
        if idx % 500 == 0:
            access_token = get_access_token()
            print(f"\n  ℹ️  CHECKPOINT at ticket {idx}/{len(tickets)}")
            print(f"     Success: {success_count}, Errors: {error_count}, With conversations: {conv_count}")
            print(f"     Time: {datetime.now().strftime('%H:%M:%S')}\n")
    
    print(f"\n{'='*60}")
    print(f"FULL SYNC COMPLETED")
    print(f"  Success: {success_count}/{len(tickets)} tickets")
    print(f"  With conversations: {conv_count}")
    print(f"  Errors: {error_count}")
    print(f"  Finished at: {datetime.now()}")
    print(f"{'='*60}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
