#!/usr/bin/env python3
"""
ServiceDesk Plus to Dust Sync
Uses page parameter for pagination - fetches ALL tickets!
"""
import os
import requests
import json
from datetime import datetime, timedelta
import time

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

# Token cache
_access_token = None
_token_expiry = None


def get_access_token():
    """Get OAuth access token"""
    global _access_token, _token_expiry
    
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    print("Getting access token...")
    
    response = requests.post(ZOHO_TOKEN_URL, data={
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    })
    
    if response.status_code == 200:
        data = response.json()
        _access_token = data['access_token']
        _token_expiry = datetime.now() + timedelta(seconds=3300)
        print("✅ Token obtained")
        return _access_token
    else:
        raise Exception(f"Token failed: {response.status_code}")


def fetch_all_tickets_with_pagination():
    """Fetch all tickets using page parameter"""
    print("\nFetching ALL tickets using page parameter...")
    print("=" * 70)
    
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    all_tickets = []
    page = 1
    
    while True:
        url = f"{SDP_API_BASE}/requests?page={page}"
        
        print(f"  Fetching page {page}...")
        
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            tickets = data.get('requests', [])
            
            if not tickets:
                print(f"  No more tickets on page {page}")
                break
            
            all_tickets.extend(tickets)
            print(f"  ✅ Got {len(tickets)} tickets (total so far: {len(all_tickets)})")
            
            # If we got fewer than expected, might be last page
            if len(tickets) < 10:
                print(f"  Last page reached")
                break
            
            page += 1
            time.sleep(1)  # Rate limit protection
            
        else:
            print(f"  ❌ Request failed: {response.status_code}")
            print(f"  Response: {response.text[:200]}")
            break
    
    print(f"\n{'=' * 70}")
    print(f"✅ Total tickets fetched: {len(all_tickets)}")
    print(f"{'=' * 70}")
    
    return all_tickets


def fetch_ticket_details(ticket_id, access_token):
    """Fetch full details for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('request', {})
    return None


def fetch_conversations(ticket_id, access_token):
    """Fetch comments for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('conversations', [])
    return []


def fetch_notes(ticket_id, access_token):
    """Fetch notes for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notes"
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('notes', [])
    return []


def clean_html(text):
    """Remove HTML tags"""
    if not text:
        return ""
    import re
    text = text.replace('<br>', '\n').replace('</p>', '\n\n').replace('<p>', '')
    text = re.sub('<[^<]+?>', '', text)
    return text.strip()


def format_ticket(ticket, conversations, notes):
    """Format ticket as markdown"""
    display_id = ticket.get('display_id', {})
    if isinstance(display_id, dict):
        display_id = display_id.get('display_value', 'Unknown')
    
    subject = ticket.get('subject', 'No Subject')
    status = (ticket.get('status') or {}).get('name', 'Unknown')
    priority = (ticket.get('priority') or {}).get('name', 'Unknown')
    
    requester = ticket.get('requester') or {}
    requester_name = requester.get('name', 'Unknown')
    requester_email = requester.get('email_id', '')
    
    technician = (ticket.get('technician') or {}).get('name', 'Unassigned')
    created = (ticket.get('created_time') or {}).get('display_value', 'Unknown')
    due_by = (ticket.get('due_by_time') or {}).get('display_value', 'Not set')
    category = (ticket.get('category') or {}).get('name', 'Uncategorized')
    description = clean_html(ticket.get('description', 'No description'))
    
    resolution = ticket.get('resolution') or {}
    resolution_content = clean_html(resolution.get('content', ''))
    
    attachments = ticket.get('attachments', []) or []
    
    text = f"""# Ticket #{display_id}: {subject}

## Details
- **Status**: {status}
- **Priority**: {priority}
- **Category**: {category}
- **Created**: {created}
- **Due By**: {due_by}

## People
- **Requester**: {requester_name} ({requester_email})
- **Assigned To**: {technician}

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
            except:
                size_kb = 0
            
            if size_kb > 0:
                text += f"- {name} ({size_kb:.1f} KB)\n"
            else:
                text += f"- {name}\n"
    
    # Add conversations
    if conversations:
        text += "\n## Comments\n\n"
        for conv in conversations:
            from_user = (conv.get('from') or {}).get('name', 'Unknown')
            time_val = (conv.get('created_time') or {}).get('display_value', 'Unknown')
            content = clean_html(conv.get('description', '') or conv.get('reply_content', ''))
            
            if content:
                text += f"### {from_user} - {time_val}\n{content}\n\n"
    
    # Add notes
    if notes:
        text += "\n## Notes\n\n"
        for note in notes:
            from_user = (note.get('added_by') or {}).get('name', 'Unknown')
            time_val = (note.get('added_time') or {}).get('display_value', 'Unknown')
            content = clean_html(note.get('description', ''))
            
            if content:
                text += f"### {from_user} - {time_val}\n{content}\n\n"
    
    text += f"\n---\n*Synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*\n"
    return text


def upload_to_dust(ticket_id, text):
    """Upload ticket to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    response = requests.post(
        f"{DUST_API_BASE}/{ticket_id}",
        headers=headers,
        json={'text': text, 'source_url': f"https://{SDP_DOMAIN}/app/{ticket_id}"}
    )
    
    return response.status_code == 200


def main():
    """Main sync function"""
    print("=" * 70)
    print("ServiceDesk Plus → Dust Full Sync")
    print("Using page parameter for pagination")
    print("=" * 70)
    
    try:
        # Fetch all tickets with pagination
        tickets = fetch_all_tickets_with_pagination()
        
        if not tickets:
            print("\n⚠️  No tickets found")
            return
        
        print(f"\n📤 Processing {len(tickets)} tickets...\n")
        
        access_token = get_access_token()
        success = 0
        failed = 0
        
        start_time = datetime.now()
        
        for i, ticket in enumerate(tickets, 1):
            ticket_id = ticket.get('id')
            display_id = ticket.get('display_id', {})
            if isinstance(display_id, dict):
                display_id = display_id.get('display_value', ticket_id)
            
            subject = ticket.get('subject', 'No subject')[:50]
            
            # Get full details, conversations, and notes
            details = fetch_ticket_details(ticket_id, access_token)
            conversations = fetch_conversations(ticket_id, access_token)
            notes = fetch_notes(ticket_id, access_token)
            
            ticket_data = details if details else ticket
            text = format_ticket(ticket_data, conversations, notes)
            
            if upload_to_dust(ticket_id, text):
                success += 1
                comment_count = len(conversations) + len(notes)
                percent = (i / len(tickets)) * 100
                print(f"✅ [{i}/{len(tickets)}] ({percent:.1f}%) #{display_id}: {subject} ({comment_count} comments)")
            else:
                failed += 1
                print(f"❌ [{i}/{len(tickets)}] #{display_id}: Failed")
            
            # Progress update every 50 tickets
            if i % 50 == 0:
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = i / elapsed if elapsed > 0 else 0
                remaining = len(tickets) - i
                eta_seconds = remaining / rate if rate > 0 else 0
                eta_minutes = eta_seconds / 60
                print(f"\n📊 Rate: {rate:.1f} tickets/sec | ETA: {eta_minutes:.0f} minutes\n")
            
            time.sleep(0.15)
        
        elapsed_time = datetime.now() - start_time
        
        print(f"\n{'=' * 70}")
        print(f"✅ SYNC COMPLETE!")
        print(f"{'=' * 70}")
        print(f"Successfully uploaded: {success}/{len(tickets)}")
        print(f"Failed: {failed}")
        print(f"Time taken: {elapsed_time}")
        print(f"{'=' * 70}\n")
        
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
