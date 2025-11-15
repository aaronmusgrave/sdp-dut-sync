#!/usr/bin/env python3
"""
ServiceDesk Plus to Dust Sync
Uses Export API to fetch all tickets
"""
import os
import requests
import json
from datetime import datetime, timedelta
import time
import csv
from io import StringIO

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


def create_export_job(access_token):
    """Create an export job for all tickets"""
    print("\nCreating export job...")
    
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/export"
    
    # Request export of all requests
    export_config = {
        "module": "request",
        "format": "csv",
        "filter_by": {}  # No filter = all tickets
    }
    
    input_data = json.dumps(export_config)
    
    response = requests.post(
        url,
        headers=headers,
        data={'input_data': input_data}
    )
    
    if response.status_code == 200:
        data = response.json()
        job_id = data.get('export', {}).get('id')
        if job_id:
            print(f"✅ Export job created: {job_id}")
            return job_id
        else:
            print(f"Response: {data}")
            return None
    else:
        print(f"❌ Failed to create export: {response.status_code}")
        print(f"Response: {response.text}")
        return None


def check_export_status(job_id, access_token):
    """Check status of export job"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/export/{job_id}"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        export_info = data.get('export', {})
        status = export_info.get('status', 'Unknown')
        return status, export_info
    else:
        return None, None


def download_export(job_id, access_token):
    """Download the exported CSV file"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/export/{job_id}/download"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.text
    else:
        print(f"❌ Failed to download: {response.status_code}")
        return None


def fetch_all_tickets_via_export():
    """Fetch all tickets using Export API"""
    print("=" * 70)
    print("Fetching ALL tickets using Export API")
    print("=" * 70)
    
    access_token = get_access_token()
    
    # Create export job
    job_id = create_export_job(access_token)
    
    if not job_id:
        print("\n❌ Could not create export job")
        print("Falling back to standard API (10 tickets)...")
        return fetch_tickets_fallback(access_token)
    
    # Poll for completion
    print("\nWaiting for export to complete...")
    max_wait = 300  # 5 minutes
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        status, export_info = check_export_status(job_id, access_token)
        
        if status == 'Finished' or status == 'Completed':
            print(f"✅ Export completed!")
            
            # Download the file
            print("\nDownloading export file...")
            csv_data = download_export(job_id, access_token)
            
            if csv_data:
                # Parse CSV
                tickets = parse_csv_to_tickets(csv_data)
                print(f"✅ Parsed {len(tickets)} tickets from export")
                return tickets
            else:
                print("❌ Could not download export")
                return []
        
        elif status == 'Failed':
            print(f"❌ Export failed")
            return []
        
        else:
            print(f"  Status: {status}... waiting")
            time.sleep(5)
    
    print("❌ Export timed out")
    return []


def parse_csv_to_tickets(csv_data):
    """Parse CSV data into ticket objects"""
    tickets = []
    
    csv_reader = csv.DictReader(StringIO(csv_data))
    
    for row in csv_reader:
        # Map CSV columns to ticket structure
        ticket = {
            'id': row.get('Request ID') or row.get('ID') or row.get('id'),
            'display_id': {'display_value': row.get('Request ID') or row.get('ID')},
            'subject': row.get('Subject') or row.get('subject'),
            'status': {'name': row.get('Status') or row.get('status')},
            'priority': {'name': row.get('Priority') or row.get('priority')},
            'requester': {
                'name': row.get('Requester') or row.get('requester'),
                'email_id': row.get('Requester Email') or ''
            },
            'technician': {'name': row.get('Technician') or row.get('technician') or 'Unassigned'},
            'created_time': {'display_value': row.get('Created Time') or row.get('created_time')},
            'due_by_time': {'display_value': row.get('Due By Time') or ''},
            'category': {'name': row.get('Category') or 'Uncategorized'},
            'description': row.get('Description') or row.get('description') or 'No description',
            'resolution': {'content': row.get('Resolution') or ''},
        }
        
        tickets.append(ticket)
    
    return tickets


def fetch_tickets_fallback(access_token):
    """Fallback: fetch 10 tickets via standard API"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json().get('requests', [])
    return []


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


def format_ticket(ticket, conversations=None):
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
    print("ServiceDesk Plus → Dust Full Sync")
    print("Using Export API")
    print("=" * 70)
    
    try:
        # Fetch all tickets via Export API
        tickets = fetch_all_tickets_via_export()
        
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
            
            # Optionally fetch conversations for each ticket
            conversations = fetch_conversations(ticket_id, access_token) if ticket_id else None
            
            text = format_ticket(ticket, conversations)
            
            if upload_to_dust(ticket_id, text):
                success += 1
                comment_count = len(conversations) if conversations else 0
                percent = (i / len(tickets)) * 100
                print(f"✅ [{i}/{len(tickets)}] ({percent:.1f}%) #{display_id}: {subject} ({comment_count} comments)")
            else:
                failed += 1
                print(f"❌ [{i}/{len(tickets)}] #{display_id}: Failed")
            
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
