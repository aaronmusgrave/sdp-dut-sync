#!/usr/bin/env python3
"""
Import tickets from CSV, fetch full details + comments from API
"""
import os
import requests
import pandas as pd
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

CSV_FILE = 'Copy of Requests_1.xlsx'

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


def read_ticket_ids_from_csv():
    """Read ticket IDs from Excel file"""
    print(f"Reading ticket IDs from {CSV_FILE}...")
    
    df = pd.read_excel(CSV_FILE)
    
    # Find ID column
    id_column = None
    for col in df.columns:
        if 'request' in col.lower() and 'id' in col.lower():
            id_column = col
            break
    
    if not id_column:
        for col in df.columns:
            if col.lower() == 'id':
                id_column = col
                break
    
    if not id_column:
        raise Exception(f"Could not find ID column. Available columns: {list(df.columns)}")
    
    ticket_ids = df[id_column].dropna().astype(int).tolist()
    
    print(f"✅ Found {len(ticket_ids)} ticket IDs")
    print(f"   ID range: {min(ticket_ids)} to {max(ticket_ids)}")
    
    return ticket_ids


def fetch_ticket_details(ticket_id, access_token):
    """Fetch full details for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('request', {})
        elif response.status_code == 404:
            return None
        else:
            return None
    except:
        return None


def fetch_conversations(ticket_id, access_token):
    """Fetch comments for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('conversations', [])
    except:
        pass
    return []


def fetch_notes(ticket_id, access_token):
    """Fetch notes for a ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notes"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('notes', [])
    except:
        pass
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
    else:
        display_id = str(display_id)
    
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
    """Main import function"""
    print("=" * 70)
    print("ServiceDesk Plus → Dust CSV Import")
    print("Imports all tickets from CSV with full details from API")
    print("=" * 70)
    
    try:
        ticket_ids = read_ticket_ids_from_csv()
        
        print(f"\n📤 Processing {len(ticket_ids)} tickets...\n")
        
        access_token = get_access_token()
        print("✅ Access token obtained\n")
        
        success = 0
        not_found = 0
        failed = 0
        
        start_time = datetime.now()
        
        for i, ticket_id in enumerate(ticket_ids, 1):
            if datetime.now() >= _token_expiry:
                access_token = get_access_token()
            
            ticket = fetch_ticket_details(ticket_id, access_token)
            
            if not ticket:
                not_found += 1
                if i % 100 == 0:
                    print(f"⚠️  [{i}/{len(ticket_ids)}] Ticket #{ticket_id}: Not found in API")
                continue
            
            conversations = fetch_conversations(ticket_id, access_token)
            notes = fetch_notes(ticket_id, access_token)
            
            text = format_ticket(ticket, conversations, notes)
            
            if upload_to_dust(ticket_id, text):
                success += 1
                
                display_id = ticket.get('display_id', {})
                if isinstance(display_id, dict):
                    display_id = display_id.get('display_value', ticket_id)
                
                subject = ticket.get('subject', 'No subject')[:50]
                comment_count = len(conversations) + len(notes)
                percent = (i / len(ticket_ids)) * 100
                
                print(f"✅ [{i}/{len(ticket_ids)}] ({percent:.1f}%) #{display_id}: {subject} ({comment_count} comments)")
            else:
                failed += 1
                print(f"❌ [{i}/{len(ticket_ids)}] Ticket #{ticket_id}: Upload failed")
            
            if i % 100 == 0:
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = i / elapsed if elapsed > 0 else 0
                remaining = len(ticket_ids) - i
                eta_seconds = remaining / rate if rate > 0 else 0
                eta_minutes = eta_seconds / 60
                print(f"\n📊 Rate: {rate:.1f} tickets/sec | Processed: {success} | Not found: {not_found} | ETA: {eta_minutes:.0f} min\n")
            
            time.sleep(0.2)
        
        elapsed_time = datetime.now() - start_time
        
        print(f"\n{'=' * 70}")
        print(f"✅ IMPORT COMPLETE!")
        print(f"{'=' * 70}")
        print(f"Successfully uploaded: {success}")
        print(f"Not found in API: {not_found}")
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
