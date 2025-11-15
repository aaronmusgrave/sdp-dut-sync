#!/usr/bin/env python3
"""
ServiceDesk Plus to Dust Sync
Testing different pagination parameter formats
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


def fetch_all_tickets(max_tickets=5000):
    """Fetch all tickets using offset/limit pagination - try multiple methods"""
    print("\nFetching tickets from ServiceDesk Plus...")
    
    access_token = get_access_token()
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests"
    
    all_tickets = []
    offset = 0
    limit = 100
    
    print("\n🔄 METHOD 1: offset/limit as query parameters")
    print("=" * 70)
    
    while len(all_tickets) < max_tickets:
        print(f"  Fetching: offset={offset}, limit={limit}...")
        
        response = requests.get(
            url,
            headers=headers,
            params={'offset': offset, 'limit': limit}
        )
        
        if response.status_code == 200:
            data = response.json()
            tickets = data.get('requests', [])
            
            if not tickets:
                print(f"  No more tickets")
                break
            
            all_tickets.extend(tickets)
            print(f"  ✅ Got {len(tickets)} tickets (total: {len(all_tickets)})")
            
            if len(tickets) < limit:
                break
            
            offset += limit
            time.sleep(0.3)
        else:
            print(f"  ❌ Method 1 failed: {response.status_code}")
            break
    
    # If Method 1 got results, return them
    if all_tickets:
        print(f"\n✅ Method 1 SUCCESS: {len(all_tickets)} tickets")
        return all_tickets
    
    # Try Method 2: URL-encoded parameters
    print("\n🔄 METHOD 2: URL with $limit and $skip")
    print("=" * 70)
    
    all_tickets = []
    skip = 0
    
    while len(all_tickets) < max_tickets:
        print(f"  Fetching: $skip={skip}, $limit={limit}...")
        
        response = requests.get(
            f"{url}?$skip={skip}&$limit={limit}",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            tickets = data.get('requests', [])
            
            if not tickets:
                break
            
            all_tickets.extend(tickets)
            print(f"  ✅ Got {len(tickets)} tickets (total: {len(all_tickets)})")
            
            if len(tickets) < limit:
                break
            
            skip += limit
            time.sleep(0.3)
        else:
            print(f"  ❌ Method 2 failed: {response.status_code}")
            break
    
    if all_tickets:
        print(f"\n✅ Method 2 SUCCESS: {len(all_tickets)} tickets")
        return all_tickets
    
    # Try Method 3: Range header
    print("\n🔄 METHOD 3: Range header")
    print("=" * 70)
    
    all_tickets = []
    start = 0
    
    while len(all_tickets) < max_tickets:
        end = start + limit - 1
        print(f"  Fetching: Range {start}-{end}...")
        
        headers_with_range = headers.copy()
        headers_with_range['Range'] = f'items={start}-{end}'
        
        response = requests.get(url, headers=headers_with_range)
        
        if response.status_code in [200, 206]:  # 206 = Partial Content
            data = response.json()
            tickets = data.get('requests', [])
            
            if not tickets:
                break
            
            all_tickets.extend(tickets)
            print(f"  ✅ Got {len(tickets)} tickets (total: {len(all_tickets)})")
            
            if len(tickets) < limit:
                break
            
            start += limit
            time.sleep(0.3)
        else:
            print(f"  ❌ Method 3 failed: {response.status_code}")
            break
    
    if all_tickets:
        print(f"\n✅ Method 3 SUCCESS: {len(all_tickets)} tickets")
        return all_tickets
    
    # Fallback: Just get the 10 default tickets
    print("\n🔄 FALLBACK: Default request (no pagination)")
    print("=" * 70)
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        all_tickets = response.json().get('requests', [])
        print(f"  Got {len(all_tickets)} tickets (API default)")
    
    print(f"\n✅ Total tickets fetched: {len(all_tickets)}")
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
    print("ServiceDesk Plus → Dust Sync")
    print("Testing Multiple Pagination Methods")
    print("=" * 70)
    
    try:
        tickets = fetch_all_tickets(max_tickets=5000)
        
        if not tickets:
            print("\n⚠️  No tickets found")
            return
        
        print(f"\n📤 Processing {len(tickets)} tickets...\n")
        
        access_token = get_access_token()
        success = 0
        
        start_time = datetime.now()
        
        for i, ticket in enumerate(tickets, 1):
            ticket_id = ticket.get('id')
            display_id = ticket.get('display_id', {})
            if isinstance(display_id, dict):
                display_id = display_id.get('display_value', ticket_id)
            
            subject = ticket.get('subject', 'No subject')[:50]
            
            details = fetch_ticket_details(ticket_id, access_token)
            conversations = fetch_conversations(ticket_id, access_token)
            
            ticket_data = details if details else ticket
            text = format_ticket(ticket_data, conversations)
            
            if upload_to_dust(ticket_id, text):
                success += 1
                comment_count = len(conversations) if conversations else 0
                percent = (i / len(tickets)) * 100
                print(f"✅ [{i}/{len(tickets)}] ({percent:.1f}%) #{display_id}: {subject} ({comment_count} comments)")
            else:
                print(f"❌ [{i}/{len(tickets)}] #{display_id}: Failed")
            
            time.sleep(0.15)
        
        elapsed_time = datetime.now() - start_time
        
        print(f"\n{'=' * 70}")
        print(f"✅ SYNC COMPLETE!")
        print(f"Successfully uploaded: {success}/{len(tickets)}")
        print(f"Time taken: {elapsed_time}")
        print(f"{'=' * 70}\n")
        
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
