#!/usr/bin/env python3
"""
ServiceDesk Plus to Dust Sync
Uses 10-minute time chunks to work around 10-ticket API limit
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
PROGRESS_DOC_ID = "sync_progress_time_based"

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


def get_progress():
    """Get last processed timestamp from Dust"""
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
            
            if 'last_end_time:' in text:
                timestamp_str = text.split('last_end_time:')[1].split('\n')[0].strip()
                return int(timestamp_str)
    except:
        pass
    
    # Start from 2 years ago if no progress found
    two_years_ago = datetime.now() - timedelta(days=730)
    return int(two_years_ago.timestamp() * 1000)


def save_progress(end_timestamp, total_synced):
    """Save progress to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_BASE}/{PROGRESS_DOC_ID}"
    
    text = f"""Time-Based Sync Progress
last_end_time: {end_timestamp}
last_end_date: {datetime.fromtimestamp(end_timestamp/1000).strftime('%Y-%m-%d %H:%M:%S UTC')}
total_synced: {total_synced}
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
"""
    
    payload = {'text': text}
    
    try:
        requests.post(url, headers=headers, json=payload)
    except:
        pass


def fetch_tickets_in_time_range(start_time, end_time, access_token):
    """Fetch tickets created in a specific time range"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests"
    
    # Build filter for time range
    list_info = {
        "filter_by": {
            "created_time": {
                "from": start_time,
                "to": end_time
            }
        }
    }
    
    input_data = json.dumps({"list_info": list_info})
    
    response = requests.post(
        url,
        headers=headers,
        data={'input_data': input_data}
    )
    
    if response.status_code == 200:
        return response.json().get('requests', [])
    else:
        # Fallback: try without filter (returns most recent 10)
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('requests', [])
    
    return []


def fetch_all_tickets_time_based(max_tickets=5000):
    """Fetch all tickets by iterating through 10-minute time ranges"""
    print("\nFetching tickets using 10-minute time chunks...")
    print("Working around 10-ticket API limit")
    print("=" * 70)
    
    access_token = get_access_token()
    
    # Get last processed time
    last_end_time = get_progress()
    current_time = int(datetime.now().timestamp() * 1000)
    
    all_tickets = {}  # Use dict to deduplicate by ID
    
    # Time window: 10 minutes
    time_window_minutes = 10
    time_window_ms = time_window_minutes * 60 * 1000
    
    start_time = last_end_time
    iteration = 0
    
    print(f"\nStarting from: {datetime.fromtimestamp(start_time/1000).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Current time: {datetime.fromtimestamp(current_time/1000).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Time window: {time_window_minutes} minutes per chunk")
    print()
    
    while start_time < current_time and len(all_tickets) < max_tickets:
        iteration += 1
        end_time = min(start_time + time_window_ms, current_time)
        
        start_datetime = datetime.fromtimestamp(start_time/1000)
        end_datetime = datetime.fromtimestamp(end_time/1000)
        
        start_str = start_datetime.strftime('%Y-%m-%d %H:%M')
        end_str = end_datetime.strftime('%Y-%m-%d %H:%M')
        
        print(f"  Chunk {iteration}: {start_str} to {end_str}...")
        
        tickets = fetch_tickets_in_time_range(start_time, end_time, access_token)
        
        new_count = 0
        for ticket in tickets:
            ticket_id = ticket.get('id')
            if ticket_id and ticket_id not in all_tickets:
                all_tickets[ticket_id] = ticket
                new_count += 1
        
        if len(tickets) > 0:
            print(f"    ✅ Found {len(tickets)} tickets ({new_count} new, total: {len(all_tickets)})")
        
        # Save progress every 10 chunks
        if iteration % 10 == 0:
            save_progress(end_time, len(all_tickets))
            print(f"\n    📊 Progress saved - {len(all_tickets)} tickets so far\n")
        
        start_time = end_time
        time.sleep(0.3)  # Small delay between requests
        
        # Refresh token if needed
        if datetime.now() >= _token_expiry:
            access_token = get_access_token()
    
    # Final progress save
    save_progress(end_time, len(all_tickets))
    
    print(f"\n{'=' * 70}")
    print(f"✅ Total unique tickets fetched: {len(all_tickets)}")
    print(f"{'=' * 70}")
    
    return list(all_tickets.values())


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
    print("ServiceDesk Plus → Dust Time-Based Sync")
    print("Using 10-minute chunks")
    print("=" * 70)
    
    try:
        print("\n✅ Token obtained")
        
        # Fetch tickets using 10-minute time chunks
        tickets = fetch_all_tickets_time_based(max_tickets=5000)
        
        if not tickets:
            print("\n⚠️  No new tickets found in this time range")
            print("✅ All tickets may already be synced!")
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
            
            # Get full details
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
                failed += 1
                print(f"❌ [{i}/{len(tickets)}] #{display_id}: Failed")
            
            if i % 25 == 0:
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
        print("💡 TIP: Schedule this to run hourly to gradually fetch all tickets")
        print("   Each run will process the next time range automatically")
        
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
