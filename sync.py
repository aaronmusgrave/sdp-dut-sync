#!/usr/bin/env python3
"""
Sync ServiceDesk Plus On-Demand tickets to Dust
Fetches tickets individually by ID from 1662 to 15666
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

# Ticket ID range
START_TICKET_ID = 1662
END_TICKET_ID = 15666

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
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")


def get_progress():
    """Get sync progress from Dust"""
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
            
            if 'last_processed_id:' in text:
                last_id = int(text.split('last_processed_id:')[1].split('\n')[0].strip())
                return last_id
        return START_TICKET_ID - 1
    except:
        return START_TICKET_ID - 1


def save_progress(last_id, success_count, failed_count):
    """Save sync progress to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{PROGRESS_DOC_ID}"
    
    text = f"""Sync Progress Tracker
last_processed_id: {last_id}
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
total_successful: {success_count}
total_failed: {failed_count}
progress: {last_id - START_TICKET_ID + 1}/{END_TICKET_ID - START_TICKET_ID + 1}
"""
    
    payload = {'text': text}
    
    try:
        requests.post(url, headers=headers, json=payload)
    except:
        pass


def fetch_ticket_by_id(ticket_id, access_token):
    """Fetch a single ticket by its display ID"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('request', {})
        elif response.status_code == 404:
            return None  # Ticket doesn't exist
        else:
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
    display_id = ticket.get('display_id', {}).get('display_value', 'Unknown')
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
    
    text = f"""# Ticket #{display_id}: {subject}

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
    """Fetch all tickets by ID range"""
    print("=" * 70)
    print("SDPOnDemand → Dust Full Sync")
    print("Fetching tickets individually by ID")
    print("=" * 70)
    
    try:
        # Get last processed ID
        last_processed = get_progress()
        start_from = last_processed + 1
        
        total_tickets = END_TICKET_ID - START_TICKET_ID + 1
        already_processed = start_from - START_TICKET_ID
        remaining = END_TICKET_ID - start_from + 1
        
        print(f"\nTicket ID Range: {START_TICKET_ID} to {END_TICKET_ID}")
        print(f"Total tickets to process: {total_tickets}")
        
        if already_processed > 0:
            print(f"Already processed: {already_processed}")
            print(f"Resuming from ID: {start_from}")
            print(f"Remaining: {remaining}")
        
        print(f"\n{'=' * 70}\n")
        
        access_token = get_access_token()
        
        success_count = 0
        not_found_count = 0
        failed_count = 0
        
        start_time = datetime.now()
        
        for ticket_id in range(start_from, END_TICKET_ID + 1):
            # Refresh token if needed
            if datetime.now() >= _token_expiry:
                access_token = get_access_token()
            
            # Fetch ticket
            ticket = fetch_ticket_by_id(ticket_id, access_token)
            
            if ticket is None:
                not_found_count += 1
                continue
            
            # Fetch conversations
            conversations = fetch_ticket_conversations(ticket_id, access_token)
            
            # Format and upload
            ticket_text = format_ticket_for_dust(ticket, conversations)
            display_id = ticket.get('display_id', {}).get('display_value', ticket_id)
            subject = ticket.get('subject', 'No subject')[:50]
            
            if upload_ticket_to_dust(ticket.get('id'), ticket_text):
                success_count += 1
                comment_count = len(conversations) if conversations else 0
                
                # Progress indicator
                processed = ticket_id - START_TICKET_ID + 1
                percent = (processed / total_tickets) * 100
                
                print(f"✅ [{processed}/{total_tickets}] ({percent:.1f}%) "
                      f"Ticket #{display_id}: {subject} ({comment_count} comments)")
            else:
                failed_count += 1
                print(f"❌ [{processed}/{total_tickets}] Failed: Ticket #{display_id}")
            
            # Save progress every 50 tickets
            if ticket_id % 50 == 0:
                save_progress(ticket_id, success_count, failed_count)
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = (ticket_id - start_from + 1) / elapsed if elapsed > 0 else 0
                remaining_tickets = END_TICKET_ID - ticket_id
                eta_seconds = remaining_tickets / rate if rate > 0 else 0
                eta_minutes = eta_seconds / 60
                
                print(f"\n📊 Progress saved | Rate: {rate:.1f} tickets/sec | ETA: {eta_minutes:.0f} minutes\n")
            
            # Small delay to avoid rate limiting
            time.sleep(0.1)
        
        # Final save
        save_progress(END_TICKET_ID, success_count, failed_count)
        
        elapsed_time = datetime.now() - start_time
        
        print(f"\n{'=' * 70}")
        print("✅ SYNC COMPLETE!")
        print(f"{'=' * 70}")
        print(f"Successfully uploaded: {success_count}")
        print(f"Not found (gaps in IDs): {not_found_count}")
        print(f"Failed: {failed_count}")
        print(f"Time taken: {elapsed_time}")
        print(f"{'=' * 70}\n")
        
    except Exception as e:
        print(f"\n❌ Sync failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    sync_all_tickets()
