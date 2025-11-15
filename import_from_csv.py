#!/usr/bin/env python3
"""
Import tickets from CSV - Verbose debug version
"""
import os
import sys
import requests
from datetime import datetime, timedelta
import time

print("=" * 70, flush=True)
print("ServiceDesk Plus → Dust CSV Import", flush=True)
print("=" * 70, flush=True)

# Configuration
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

CSV_FILE = 'requests.csv'

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Token cache
_access_token = None
_token_expiry = None

print(f"\n🔍 Checking for CSV file...", flush=True)
print(f"Current directory: {os.getcwd()}", flush=True)
print(f"Looking for: {CSV_FILE}", flush=True)

if not os.path.exists(CSV_FILE):
    print(f"❌ File not found!", flush=True)
    sys.exit(1)

print(f"✅ File found! Size: {os.path.getsize(CSV_FILE)} bytes", flush=True)

print(f"\nReading CSV file (simple method)...", flush=True)

# Read CSV manually
ticket_ids = []
try:
    with open(CSV_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        print(f"✅ Read {len(lines)} lines", flush=True)
        
        # Find ID column
        header = lines[0].strip().split(',')
        print(f"Columns: {header}", flush=True)
        
        id_col_index = None
        for i, col in enumerate(header):
            if 'id' in col.lower() or 'request' in col.lower():
                id_col_index = i
                print(f"✅ Using column {i}: '{col}'", flush=True)
                break
        
        if id_col_index is None:
            print("❌ Could not find ID column", flush=True)
            sys.exit(1)
        
        # Extract IDs
        for line in lines[1:]:
            parts = line.strip().split(',')
            if len(parts) > id_col_index:
                try:
                    tid = int(parts[id_col_index].strip('"'))
                    ticket_ids.append(tid)
                except:
                    pass
        
        print(f"✅ Found {len(ticket_ids)} ticket IDs", flush=True)
        if ticket_ids:
            print(f"   Range: {min(ticket_ids)} to {max(ticket_ids)}", flush=True)

except Exception as e:
    print(f"❌ Error reading CSV: {e}", flush=True)
    sys.exit(1)

if not ticket_ids:
    print("❌ No ticket IDs found", flush=True)
    sys.exit(1)


def get_access_token():
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


def fetch_ticket_details(ticket_id, access_token):
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('request', {})
    except:
        pass
    return None


def fetch_conversations(ticket_id, access_token):
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('conversations', [])
    except:
        pass
    return []


def clean_html(text):
    if not text:
        return ""
    import re
    text = text.replace('<br>', '\n').replace('</p>', '\n\n').replace('<p>', '')
    text = re.sub('<[^<]+?>', '', text)
    return text.strip()


def format_ticket(ticket, conversations):
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
    category = (ticket.get('category') or {}).get('name', 'Uncategorized')
    description = clean_html(ticket.get('description', 'No description'))
    
    text = f"""# Ticket #{display_id}: {subject}

## Details
- **Status**: {status}
- **Priority**: {priority}
- **Category**: {category}
- **Created**: {created}

## People
- **Requester**: {requester_name} ({requester_email})
- **Assigned To**: {technician}

## Description
{description}
"""
    
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


print(f"\n📤 Processing {len(ticket_ids)} tickets...\n", flush=True)

access_token = get_access_token()
print("✅ Access token obtained\n", flush=True)

success = 0
not_found = 0
start_time = datetime.now()

for i, ticket_id in enumerate(ticket_ids, 1):
    print(f"[{i}/{len(ticket_ids)}] Fetching ticket #{ticket_id}...", flush=True)
    
    if datetime.now() >= _token_expiry:
        access_token = get_access_token()
    
    ticket = fetch_ticket_details(ticket_id, access_token)
    
    if not ticket:
        print(f"  ⚠️  Not found in API", flush=True)
        not_found += 1
        continue
    
    print(f"  ✅ Got details, fetching comments...", flush=True)
    conversations = fetch_conversations(ticket_id, access_token)
    print(f"  ✅ Got {len(conversations)} comments, formatting...", flush=True)
    
    text = format_ticket(ticket, conversations)
    
    print(f"  ✅ Uploading to Dust...", flush=True)
    if upload_to_dust(ticket_id, text):
        success += 1
        display_id = ticket.get('display_id', {})
        if isinstance(display_id, dict):
            display_id = display_id.get('display_value', ticket_id)
        
        subject = ticket.get('subject', 'No subject')[:50]
        percent = (i / len(ticket_ids)) * 100
        
        print(f"✅ [{i}/{len(ticket_ids)}] ({percent:.1f}%) #{display_id}: {subject}", flush=True)
    else:
        print(f"  ❌ Upload failed", flush=True)
    
    if i % 100 == 0:
        elapsed = (datetime.now() - start_time).total_seconds()
        rate = i / elapsed if elapsed > 0 else 0
        remaining = len(ticket_ids) - i
        eta_min = (remaining / rate / 60) if rate > 0 else 0
        print(f"\n📊 Rate: {rate:.1f}/sec | Success: {success} | Not found: {not_found} | ETA: {eta_min:.0f}min\n", flush=True)
    
    time.sleep(0.2)

elapsed = datetime.now() - start_time

print(f"\n{'=' * 70}", flush=True)
print(f"✅ IMPORT COMPLETE!", flush=True)
print(f"{'=' * 70}", flush=True)
print(f"Successfully uploaded: {success}", flush=True)
print(f"Not found in API: {not_found}", flush=True)
print(f"Time taken: {elapsed}", flush=True)
print(f"{'=' * 70}\n", flush=True)
