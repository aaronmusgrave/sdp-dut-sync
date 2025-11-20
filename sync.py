#!/usr/bin/env python3
"""
Conversation Backfill - Add conversations to existing tickets in Dust
Updates existing tickets with conversation data
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
    
    print(f"Fetching all tickets...")
    
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
                break
                
        except Exception as e:
            break
    
    print(f"✓ Fetched {len(all_tickets)} tickets")
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
                time.sleep(5)
                continue
            else:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
        except:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
    
    return None

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    response = fetch_with_retry(url, headers)
    
    if response and response.status_code == 200:
        return response.json().get('request', {})
    return None

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch ticket conversations with full text content"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return []
        
        notifications = response.json().get('notifications', [])
        conversations = []
        
        # Limit to first 20 notifications
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
                                'sender_email': sender.get('email_id', ''),
                                'time': time_obj.get('display_value', 'Unknown'),
                                'time_ms': time_obj.get('value'),
                                'description': description,
                                'is_public': notification.get('is_public', True)
                            })
                    
                    time.sleep(0.1)
                    
                except Exception as e:
                    continue
        
        return conversations
        
    except Exception as e:
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

def safe_string(value, default=''):
    """Safely convert to string"""
    if value is None:
        return default
    try:
        return str(value).strip()
    except:
        return default

def safe_get_name(obj, default='Unknown'):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('name', default))
    return safe_string(obj, default)

def safe_get_display_value(obj, default=''):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('display_value', default))
    return safe_string(obj, default)

def safe_get_value(obj, default=None):
    """Get the value field from a dict"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get('value', default)
        if val is not None:
            try:
                return int(val)
            except:
                return default
        return default
    try:
        return int(obj)
    except:
        return default

def get_display_id(details):
    """Extract the display ID"""
    if 'display_id' in details:
        display_id = details['display_id']
        if isinstance(display_id, dict):
            return safe_string(display_id.get('display_value', display_id.get('value', details.get('id'))))
        return safe_string(display_id)
    
    return safe_string(details.get('id', 'Unknown'))

def format_ticket_with_conversations(ticket, details, conversations):
    """Format ticket with conversations added to structured data"""
    
    try:
        internal_id = safe_string(ticket.get('id'))
        display_id = get_display_id(details)
        subject = safe_string(details.get('subject', 'No Subject'))
        
        status = safe_get_name(details.get('status'))
        priority = safe_get_name(details.get('priority'))
        
        requester_obj = details.get('requester', {})
        requester_name = safe_get_name(requester_obj)
        requester_email = safe_string(requester_obj.get('email_id', '') if isinstance(requester_obj, dict) else '')
        
        technician_obj = details.get('technician', {})
        technician_name = safe_get_name(technician_obj, 'Unassigned')
        technician_email = safe_string(technician_obj.get('email_id', '') if isinstance(technician_obj, dict) else '')
        
        category = safe_get_name(details.get('category'), 'Uncategorized')
        subcategory = safe_get_name(details.get('subcategory'), 'None')
        
        group = safe_get_name(details.get('group'), 'Unassigned')
        site = safe_get_name(details.get('site'), 'No site')
        
        request_type = safe_get_name(details.get('request_type'), 'Incident')
        
        created_time_ms = safe_get_value(details.get('created_time'))
        due_by_time_ms = safe_get_value(details.get('due_by_time'))
        last_updated_time_ms = safe_get_value(details.get('last_updated_time'))
        
        created_time_display = safe_get_display_value(details.get('created_time'))
        due_by_time_display = safe_get_display_value(details.get('due_by_time'), 'Not set')
        
        is_overdue = bool(details.get('is_overdue', False))
        is_escalated = bool(details.get('is_escalated', False))
        
        raw_description = details.get('description', '')
        clean_description = strip_html(raw_description)
        
        resolution_obj = details.get('resolution')
        resolution_text = ''
        if resolution_obj and isinstance(resolution_obj, dict):
            resolution_content = resolution_obj.get('content', '')
            if resolution_content:
                resolution_text = strip_html(resolution_content)
        
        # Process conversations - sort newest first
        conversations_structured = []
        conversations_text = ""
        
        if conversations:
            # Sort by timestamp, newest first
            conversations_sorted = sorted(
                conversations,
                key=lambda x: x.get('time_ms', 0) or 0,
                reverse=True
            )
            
            conversations_text = "\n\n## Conversations & Replies\n\n"
            conversations_text += f"Total: {len(conversations_sorted)} (newest first)\n\n"
            
            for idx, conv in enumerate(conversations_sorted, 1):
                try:
                    sender = conv.get('sender', 'Unknown')
                    sender_email = conv.get('sender_email', '')
                    time_str = conv.get('time', 'Unknown time')
                    time_ms = conv.get('time_ms')
                    conv_type = conv.get('type', 'Unknown')
                    description = conv.get('description', '')
                    is_public = conv.get('is_public', True)
                    
                    clean_text = strip_html(description)
                    
                    if len(clean_text.strip()) < 10:
                        continue
                    
                    # Add to structured array
                    conversations_structured.append({
                        'sender': sender,
                        'sender_email': sender_email,
                        'time': time_str,
                        'time_ms': time_ms,
                        'type': conv_type,
                        'is_public': is_public,
                        'text': clean_text[:500]  # Limit length
                    })
                    
                    # Add to text version
                    visibility = "Public" if is_public else "Private"
                    conversations_text += f"### Reply {idx} - {time_str}\n"
                    conversations_text += f"**From:** {sender}"
                    if sender_email:
                        conversations_text += f" ({sender_email})"
                    conversations_text += f"\n**Type:** {conv_type} ({visibility})\n\n"
                    conversations_text += f"{clean_text}\n\n"
                    conversations_text += "---\n\n"
                    
                except Exception as e:
                    continue
        
        conversation_count = len(conversations_structured)
        
        # Get latest conversation timestamp
        latest_conversation_time_ms = None
        if conversations_structured:
            latest_conversation_time_ms = conversations_structured[0].get('time_ms')
        
        # Create structured document
        structured_data = {
            "ticket_id": display_id,
            "internal_id": internal_id,
            "subject": subject,
            "status": status,
            "priority": priority,
            "request_type": request_type,
            "requester_name": requester_name,
            "requester_email": requester_email,
            "technician_name": technician_name,
            "technician_email": technician_email,
            "group": group,
            "site": site,
            "category": category,
            "subcategory": subcategory,
            "created_time": created_time_display,
            "due_by_time": due_by_time_display,
            "is_overdue": is_overdue,
            "is_escalated": is_escalated,
            "description": clean_description[:1000],
            "resolution": resolution_text[:1000] if resolution_text else "",
            "conversations": conversations_structured,
            "conversation_count": conversation_count
        }
        
        # Add numeric timestamps if valid
        if created_time_ms is not None:
            structured_data["created_time_ms"] = created_time_ms
        if due_by_time_ms is not None:
            structured_data["due_by_time_ms"] = due_by_time_ms
        if last_updated_time_ms is not None:
            structured_data["last_updated_time_ms"] = last_updated_time_ms
        if latest_conversation_time_ms is not None:
            structured_data["latest_conversation_time_ms"] = latest_conversation_time_ms
        
        dust_tags = [
            f"status:{status.lower().replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}",
            f"ticket-id:{display_id}",
            f"category:{category.lower().replace(' ', '-')}",
        ]
        
        if is_overdue:
            dust_tags.append('overdue')
        if is_escalated:
            dust_tags.append('escalated')
        if conversation_count > 0:
            dust_tags.append('has-conversations')
        
        text_content = f"""# {subject}

**Ticket ID:** {display_id}
**Status:** {status} | **Priority:** {priority}
**Type:** {request_type}

## Assignment
- Requester: {requester_name} ({requester_email})
- Technician: {technician_name}
- Group: {group}

## Classification
- Category: {category}
- Subcategory: {subcategory}

## Timeline
- Created: {created_time_display}
- Due By: {due_by_time_display}

## Description
{clean_description}
"""

        if resolution_text:
            text_content += f"\n## Resolution\n{resolution_text}\n"
        
        # Add conversations to text
        text_content += conversations_text
        
        timestamp = last_updated_time_ms if last_updated_time_ms else int(time.time() * 1000)
        
        return {
            "document_id": f"ticket-{internal_id}",
            "timestamp": timestamp,
            "tags": dust_tags,
            "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{internal_id}",
            "text": text_content,
            "structured_data": structured_data
        }
        
    except Exception as e:
        print(f"      Format error: {str(e)[:100]}")
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
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
                continue
            return False
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
                continue
            return False
    
    return False

def main():
    print("=" * 60)
    print(f"CONVERSATION BACKFILL - Starting at {datetime.now()}")
    print("=" * 60)
    print("This will add conversations to existing tickets in Dust")
    print()
    
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
                     'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"ERROR: Missing: {', '.join(missing_vars)}")
        return
    
    tickets = fetch_all_tickets()
    print(f"\nTotal tickets to process: {len(tickets)}\n")
    
    if not tickets:
        return
    
    access_token = get_access_token()
    success_count = 0
    error_count = 0
    conv_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        if idx % 100 == 0 or idx == 1:
            print(f"[{idx}/{len(tickets)}] Processing...")
        
        try:
            details = fetch_ticket_details(ticket_id, access_token)
            if not details:
                error_count += 1
                continue
            
            conversations = fetch_ticket_conversations(ticket_id, access_token)
            
            if conversations:
                conv_count += 1
            
            document = format_ticket_with_conversations(ticket, details, conversations)
            
            if document and upload_to_dust(document):
                success_count += 1
                if idx % 100 == 0:
                    print(f"  ✓ {success_count} updated, {conv_count} with conversations, {error_count} errors")
            else:
                error_count += 1
            
        except Exception as e:
            error_count += 1
            continue
        
        time.sleep(0.3)  # Slower for conversation fetching
        
        if idx % 250 == 0:
            access_token = get_access_token(force_refresh=True)
            print(f"  ℹ️  Checkpoint: {success_count}/{idx}, {conv_count} with conversations\n")
    
    print(f"\n{'='*60}")
    print(f"CONVERSATION BACKFILL COMPLETED")
    print(f"  Success: {success_count}/{len(tickets)}")
    print(f"  With conversations: {conv_count}")
    print(f"  Errors: {error_count}")
    print(f"  Finished: {datetime.now()}")
    print(f"{'='*60}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
