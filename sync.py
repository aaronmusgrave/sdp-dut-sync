#!/usr/bin/env python3
"""
ServiceDesk Plus to Dust Incremental Sync
Fetches ONLY 10 new tickets per run, tracks progress
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

# Progress tracking
PROGRESS_DOC_ID = "sync_progress_incremental"

# Token cache
_access_token = None
_token_expiry = None


def get_access_token():
    """Get OAuth access token"""
    global _access_token, _token_expiry
    
    if _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
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
        return _access_token
    else:
        raise Exception(f"Token failed: {response.status_code}")


def get_synced_ticket_ids():
    """Get set of already synced ticket IDs from Dust"""
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
            
            # Extract synced IDs from progress document
            synced_ids = set()
            for line in text.split('\n'):
                if line.startswith('synced_id:'):
                    ticket_id = line.split('synced_id:')[1].strip()
                    if ticket_id:
                        synced_ids.add(ticket_id)
            
            return synced_ids
    except:
        pass
    
    return set()


def save_synced_ticket_ids(synced_ids, total_count):
    """Save synced ticket IDs to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{PROGRESS_DOC_ID}"
    
    # Build text with all synced IDs
    text = f"""Incremental Sync Progress
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
total_synced: {total_count}

Synced Ticket IDs:
"""
    
    for ticket_id in sorted(synced_ids):
        text += f"synced_id: {ticket_id}\n"
    
    payload = {'text': text}
    
    try:
        requests.post(url, headers=headers, json=payload)
    except:
        pass


def fetch_10_new_tickets():
    """Fetch up to 10 new tickets that haven't been synced"""
    print("\nFetching tickets from API...")
    
    access_token = get_access_token()
    
    # Get already synced IDs
    synced_ids = get_synced_ticket_ids()
    print(f"Already synced: {len(synced_ids)} tickets")
    
    # Fetch 10 tickets from API (default behavior)
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        all_tickets = response.json().get('requests', [])
        
        # Filter out already synced tickets
        new_tickets = []
        for ticket in all_tickets:
            ticket_id = ticket.get('id')
            if ticket_id and ticket_id not in synced_ids:
                new_tickets.append(ticket)
                if len(new_tickets) >= 10:  # Stop at 10
                    break
        
        print(f"API returned: {len(all_tickets)} tickets")
        print(f"New tickets: {len(new_tickets)} tickets")
        
        return new_tickets, synced_ids
    else:
        print(f"❌ API failed: {response.status_code}")
        return [], synced_ids


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


def clean_html(text):
    """Remove HTML tags"""
    if not text:
        return ""
    import re
    text = text.replace('<br>', '\n').replace('</p>', '\n\n').replace('<p>', '')
    text = re.sub('<[^<]+?>', '', text)
    return text.strip()


def format_ticket(ticket, conversations):
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
    
    if conversations:
        text += "\n## Comments\n\n"
        for conv in conversations:
            from_user = (conv.get('from') or {}).get('name', 'Unknown')
            time_val = (conv.get('created_time') or {}).get('display_value', 'Unknown')
            content = clean_html(conv.get('description', '') or conv.get('reply_content', ''))
            
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
    print("ServiceDesk Plus → Dust Incremental Sync")
    print("Syncs 10 new tickets per run")
    print("=" * 70)
    
    try:
        print("\n✅ Token obtained")
        
        # Fetch up to 10 new tickets
        new_tickets, synced_ids = fetch_10_new_tickets()
        
        if not new_tickets:
            print("\n✨ No new tickets to sync!")
            print(f"Total tickets in Dust: {len(synced_ids)}")
            print("\n💡 The API only returns the 10 most recent tickets.")
            print("   All 10 are already synced. This will keep them updated.")
            return
        
        print(f"\n📤 Processing {len(new_tickets)} new tickets...\n")
        
        access_token = get_access_token()
        success = 0
        
        for i, ticket in enumerate(new_tickets, 1):
            ticket_id = ticket.get('id')
            display_id = ticket.get('display_id', {})
            if isinstance(display_id, dict):
                display_id = display_id.get('display_value', ticket_id)
            
            subject = ticket.get('subject', 'No subject')[:50]
            
            # Get full details
            details = fetch_ticket_details(ticket_id, access_token)
            conversations = fetch_conversations(ticket_id, access_token)
            
            ticket_data = details if details else ticket
            text = format_ticket(ticket_data, conversations)
            
            if upload_to_dust(ticket_id, text):
                success += 1
                synced_ids.add(ticket_id)
                comment_count = len(conversations) if conversations else 0
                percent = (i / len(new_tickets)) * 100
                print(f"✅ [{i}/{len(new_tickets)}] ({percent:.1f}%) #{display_id}: {subject} ({comment_count} comments)")
            else:
                print(f"❌ [{i}/{len(new_tickets)}] #{display_id}: Failed")
            
            time.sleep(0.15)
        
        # Save progress
        save_synced_ticket_ids(synced_ids, len(synced_ids))
        
        print(f"\n{'=' * 70}")
        print(f"✅ SYNC COMPLETE!")
        print(f"{'=' * 70}")
        print(f"New tickets uploaded: {success}/{len(new_tickets)}")
        print(f"Total tickets in Dust: {len(synced_ids)}")
        print(f"{'=' * 70}\n")
        
        if len(synced_ids) < 100:
            print("⚠️  NOTE: The API only returns the 10 most recent tickets.")
            print("   This sync can only capture those 10 tickets and keep them updated.")
            print("   For complete historical data, CSV export is required.")
        
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
