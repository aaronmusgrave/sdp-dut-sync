#!/usr/bin/env python3
"""
ONE-TIME Initial Sync - Fetch all tickets
Uses the internal ticket IDs from API
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

# Progress tracking
PROGRESS_DOC_ID = "sync_progress_tracker"

# Token cache
_access_token = None
_token_expiry = None


def get_access_token():
    """Get a valid access token using refresh token"""
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
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("✅ Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")


def get_synced_ticket_ids():
    """Get list of already synced ticket IDs from Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{PROGRESS_DOC_ID}"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            text = data.get('document', {}).get('text', '')
            
            if 'synced_ids:' in text:
                ids_str = text.split('synced_ids:')[1].split('\n')[0].strip()
                return set(ids_str.split(',')) if ids_str else set()
        return set()
    except:
        return set()


def save_progress(synced_ids, success_count):
    """Save sync progress to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{PROGRESS_DOC_ID}"
    
    # Store up to last 1000 IDs
    ids_list = list(synced_ids)[-1000:]
    ids_str = ','.join(ids_list)
    
    text = f"""Sync Progress Tracker
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
total_synced: {success_count}
synced_ids: {ids_str}
"""
    
    payload = {'text': text}
    
    try:
        requests.post(url, headers=headers, json=payload)
    except:
        pass


def fetch_tickets_by_filter(access_token, filter_name=None, filter_value=None):
    """Fetch tickets with optional filter"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests"
    
    try:
        if filter_name and filter_value:
            search_criteria = {
                "list_info": {
                    "row_count": 100,
                    "filter_by": {
                        filter_name: {"name": filter_value}
                    }
                }
            }
            data = {'input_data': json.dumps(search_criteria)}
            response = requests.post(url, headers=headers, data=data)
        else:
            response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            tickets = response.json().get('requests', [])
            return tickets
        else:
            return []
    except Exception as e:
        return []


def fetch_all_tickets_multi_filter():
    """Fetch tickets using multiple filter strategies"""
    print("Fetching tickets using multiple filters...")
    print("=" * 70)
    
    access_token = get_access_token()
    all_tickets = {}  # Use dict to deduplicate by internal ID
    
    # Strategy 1: Fetch by Status
    print("\n📋 Fetching by STATUS...")
    statuses = ['Open', 'Closed', 'Resolved', 'On Hold', 'In Progress', 'Pending', 
                'Waiting for Customer', 'Waiting for Third Party']
    
    for status in statuses:
        tickets = fetch_tickets_by_filter(access_token, 'status', status)
        for ticket in tickets:
            ticket_id = ticket.get('id')
            if ticket_id and ticket_id not in all_tickets:
                all_tickets[ticket_id] = ticket
        if tickets:
            print(f"  {status}: {len(tickets)} tickets (total unique: {len(all_tickets)})")
        time.sleep(0.3)
    
    # Strategy 2: Fetch by Priority
    print("\n⚡ Fetching by PRIORITY...")
    priorities = ['Low', 'Medium', 'High', 'Urgent']
    
    for priority in priorities:
        tickets = fetch_tickets_by_filter(access_token, 'priority', priority)
        for ticket in tickets:
            ticket_id = ticket.get('id')
            if ticket_id and ticket_id not in all_tickets:
                all_tickets[ticket_id] = ticket
        if tickets:
            print(f"  {priority}: {len(tickets)} tickets (total unique: {len(all_tickets)})")
        time.sleep(0.3)
    
    # Strategy 3: Fetch without filter (most recent)
    print("\n🔄 Fetching RECENT tickets...")
    tickets = fetch_tickets_by_filter(access_token)
    for ticket in tickets:
        ticket_id = ticket.get('id')
        if ticket_id and ticket_id not in all_tickets:
            all_tickets[ticket_id] = ticket
    print(f"  Recent: {len(tickets)} tickets (total unique: {len(all_tickets)})")
    
    print(f"\n{'=' * 70}")
    print(f"✅ TOTAL UNIQUE TICKETS FOUND: {len(all_tickets)}")
    print(f"{'=' * 70}\n")
    
    return list(all_tickets.values())


def fetch_ticket_by_id(ticket_id, access_token):
    """Fetch a single ticket by its internal ID"""
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


def clean_html(text):
    """Basic HTML cleanup"""
    if not text:
        return ""
    text = text.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
    text = text.replace('</p>', '\n\n').replace('<p>', '')
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    import re
    text = re.sub('<[^<]+?>', '', text)
    return text.strip()


def format_ticket_for_dust(ticket, conversations=None):
    """Format ticket as markdown for Dust"""
    ticket_id = ticket.get('id', 'Unknown')
    
    display_id = ticket.get('display_id', {})
    if isinstance(display_id, dict):
        display_id_value = display_id.get('display_value', 'Unknown')
    else:
        display_id_value = str(display_id)
    
    subject = ticket.get('subject', 'No Subject')
    
    status = ticket.get('status', {}) or {}
    status_name = status.get('name', 'Unknown')
    
    priority = ticket.get('priority', {}) or {}
    priority_name = priority.get('name', 'Unknown')
    
    requester = ticket.get('requester', {}) or {}
    requester_name = requester.get('name', 'Unknown')
    requester_email = requester.get('email_id', '')
    
    technician = ticket.get('technician', {}) or {}
    technician_name = technician.get('name', 'Unassigned')
    
    created_time = ticket.get('created_time', {}) or {}
    created_time_display = created_time.get('display_value', 'Unknown')
    
    due_by_time = ticket.get('due_by_time', {}) or {}
    due_by_time_display = due_by_time.get('display_value', 'Not set')
    
    category = ticket.get('category', {}) or {}
    category_name = category.get('name', 'Uncategorized')
    
    description = clean_html(ticket.get('description', 'No description'))
    
    resolution = ticket.get('resolution', {}) or {}
    resolution_content = clean_html(resolution.get('content', ''))
    
    attachments = ticket.get('attachments', []) or []
    
    text = f"""# Ticket #{display_id_value}: {subject}

## Details
- **Status**: {status_name}
- **Priority**: {priority_name}
- **Category**: {category_name}
- **Created**: {created_time_display}
- **Due By**: {due_by_time_display}

## People
- **Requester**: {requester_name} ({requester_email})
- **Assigned To**: {technician_name}

## Description
{description}
"""

    if resolution_content:
        text += f"\n## Resolution\n{resolution_content}\n"

    if attachments:
        text += "\n## Attachments\n"
        for att in attachments:
            name = att.get('name', 'Unknown')
            size_raw = att.get('size', 0)
            try:
                size_kb = float(size_raw) / 1024 if size_raw else 0
            except (ValueError, TypeError):
                size_kb = 0
            
            if size_kb > 0:
                text += f"- {name} ({size_kb:.1f} KB)\n"
            else:
                text += f"- {name}\n"

    if conversations:
        text += "\n## Comments & Updates\n\n"
        for conv in conversations:
            from_user = (conv.get('from', {}) or {}).get('name', 'Unknown')
            time_val = (conv.get('created_time', {}) or {}).get('display_value', 'Unknown')
            content = clean_html(conv.get('description', '') or conv.get('reply_content', ''))
            
            if content:
                text += f"### {from_user} - {time_val}\n{content}\n\n"

    text += f"\n---\n*Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*\n"
    return text


def upload_ticket_to_dust(ticket_id, ticket_text):
    """Upload a ticket to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{ticket_id}"
    payload = {
        'text': ticket_text,
        'source_url': f"https://{SDP_DOMAIN}/app/{ticket_id}"
    }
    
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 200


def sync_all_tickets():
    """Initial sync - fetch all available tickets"""
    print("=" * 70)
    print("SDPOnDemand → Dust Initial Sync")
    print("=" * 70)
    print()
    
    try:
        # Get already synced IDs
        synced_ids = get_synced_ticket_ids()
        print(f"Already synced: {len(synced_ids)} tickets\n")
        
        # Fetch all tickets using multi-filter approach
        tickets = fetch_all_tickets_multi_filter()
        
        # Filter out already synced tickets
        new_tickets = [t for t in tickets if t.get('id') not in synced_ids]
        
        if not new_tickets:
            print("✨ No new tickets to sync")
            return
        
        print(f"\n📤 Uploading {len(new_tickets)} new tickets to Dust...")
        print("-" * 70)
        
        access_token = get_access_token()
        success_count = len(synced_ids)  # Start with already synced count
        
        for i, ticket in enumerate(new_tickets, 1):
            ticket_id = ticket.get('id')
            display_id = ticket.get('display_id', {})
            if isinstance(display_id, dict):
                display_id_value = display_id.get('display_value', 'Unknown')
            else:
                display_id_value = str(display_id)
            
            subject = ticket.get('subject', 'No subject')[:50]
            
            # Fetch full details
            detailed_ticket = fetch_ticket_by_id(ticket_id, access_token)
            conversations = fetch_ticket_conversations(ticket_id, access_token)
            
            # Use detailed ticket if available, otherwise use summary
            ticket_to_format = detailed_ticket if detailed_ticket else ticket
            
            # Format and upload
            ticket_text = format_ticket_for_dust(ticket_to_format, conversations)
            
            if upload_ticket_to_dust(ticket_id, ticket_text):
                success_count += 1
                synced_ids.add(ticket_id)
                comment_count = len(conversations) if conversations else 0
                print(f"✅ [{i}/{len(new_tickets)}] Ticket #{display_id_value}: {subject} ({comment_count} comments)")
            else:
                print(f"❌ [{i}/{len(new_tickets)}] Failed: Ticket #{display_id_value}")
            
            # Save progress every 10 tickets
            if i % 10 == 0:
                save_progress(synced_ids, success_count)
                print(f"\n📊 Progress saved - Total synced: {success_count}\n")
        
        # Final save
        save_progress(synced_ids, success_count)
        
        print("-" * 70)
        print(f"\n✅ SYNC COMPLETE!")
        print(f"Total tickets in Dust: {success_count}")
        print(f"New tickets uploaded: {len(new_tickets)}")
        print(f"{'=' * 70}\n")
        
        print("\n💡 NOTE: This sync method can only fetch ~50-100 tickets per run")
        print("   due to API limitations. Run this script multiple times or")
        print("   schedule it to run regularly to gradually sync more tickets.")
        print(f"{'=' * 70}\n")
        
    except Exception as e:
        print(f"\n❌ Sync failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    sync_all_tickets()
