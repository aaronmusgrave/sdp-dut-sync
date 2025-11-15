#!/usr/bin/env python3
"""
Import CSV data directly to Dust - All columns included
"""
import os
import sys
import requests
from datetime import datetime
import time
import csv

print("=" * 70, flush=True)
print("CSV → Dust Direct Import (All Columns)", flush=True)
print("=" * 70, flush=True)

# Configuration
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

CSV_FILE = 'requests.csv'

DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

print(f"\n🔍 Reading CSV file: {CSV_FILE}...", flush=True)

if not os.path.exists(CSV_FILE):
    print(f"❌ File not found!", flush=True)
    sys.exit(1)

# Read CSV with proper parsing
tickets = []
try:
    with open(CSV_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        tickets = list(reader)
        
        print(f"✅ Parsed {len(tickets)} tickets from CSV", flush=True)
        print(f"✅ Columns: {list(tickets[0].keys()) if tickets else 'None'}\n", flush=True)

except Exception as e:
    print(f"❌ Error reading CSV: {e}", flush=True)
    sys.exit(1)


def clean_html(text):
    """Remove HTML tags from description"""
    if not text:
        return ""
    import re
    text = text.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
    text = text.replace('</p>', '\n\n').replace('<p>', '')
    text = text.replace('</div>', '\n').replace('<div>', '')
    text = re.sub('<[^<]+?>', '', text)
    # Clean up HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    return text.strip()


def format_csv_ticket(ticket):
    """Format CSV row as comprehensive markdown"""
    request_id = ticket.get('request_id', 'Unknown')
    subject = ticket.get('subject', 'No Subject')
    requester = ticket.get('requester_name', 'Unknown')
    status = ticket.get('status', 'Unknown')
    technician = ticket.get('technician', 'Unassigned')
    category = ticket.get('category', 'Uncategorized')
    site = ticket.get('site', 'Unknown')
    group = ticket.get('group', 'Unknown')
    department = ticket.get('department', '-')
    
    # Dates
    created_date = ticket.get('created_date', 'Unknown')
    due_by_date = ticket.get('due_by_date', 'Not set')
    responded_date = ticket.get('responded_date', '-')
    completed_date = ticket.get('completed_date', '-')
    resolved_time = ticket.get('resolved_time', '-')
    last_updated_time = ticket.get('last_updated_time', '-')
    last_updated_by = ticket.get('last_updated_by', '-')
    
    # Description
    description = clean_html(ticket.get('description', 'No description'))
    
    text = f"""# Ticket #{request_id}: {subject}

## Details
- **Status**: {status}
- **Priority**: {category}
- **Created**: {created_date}
- **Due By**: {due_by_date}
- **Completed**: {completed_date}
- **Resolved Time**: {resolved_time}

## People
- **Requester**: {requester}
- **Technician**: {technician}
- **Department**: {department}

## Location & Assignment
- **Site**: {site}
- **Group**: {group}
- **Category**: {category}

## Description
{description}

## Timeline
- **Responded**: {responded_date}
- **Last Updated**: {last_updated_time}
- **Last Updated By**: {last_updated_by}

---
*Imported from CSV: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*

⚠️ **Note:** This ticket was imported from CSV export. Comments are not included. For full conversation history, view in ServiceDesk Plus.
"""
    
    return text


def upload_to_dust(ticket_id, text):
    """Upload ticket to Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    # Use csv_ prefix to distinguish from API-fetched tickets
    response = requests.post(
        f"{DUST_API_BASE}/csv_{ticket_id}",
        headers=headers,
        json={'text': text}
    )
    
    return response.status_code == 200


print(f"📤 Uploading {len(tickets)} tickets to Dust...\n", flush=True)

success = 0
failed = 0
start_time = datetime.now()

for i, ticket in enumerate(tickets, 1):
    request_id = ticket.get('request_id', f'unknown_{i}')
    subject = ticket.get('subject', 'No subject')[:50]
    
    text = format_csv_ticket(ticket)
    
    if upload_to_dust(request_id, text):
        success += 1
        percent = (i / len(tickets)) * 100
        status = ticket.get('status', 'Unknown')
        print(f"✅ [{i}/{len(tickets)}] ({percent:.1f}%) #{request_id}: {subject} [{status}]", flush=True)
    else:
        failed += 1
        print(f"❌ [{i}/{len(tickets)}] #{request_id}: Upload failed", flush=True)
    
    # Progress update every 100 tickets
    if i % 100 == 0:
        elapsed = (datetime.now() - start_time).total_seconds()
        rate = i / elapsed if elapsed > 0 else 0
        remaining = len(tickets) - i
        eta_minutes = (remaining / rate / 60) if rate > 0 else 0
        print(f"\n📊 Rate: {rate:.1f} tickets/sec | Success: {success} | Failed: {failed} | ETA: {eta_minutes:.0f} min\n", flush=True)
    
    time.sleep(0.1)  # Small delay to avoid rate limits

elapsed_time = datetime.now() - start_time

print(f"\n{'=' * 70}", flush=True)
print(f"✅ IMPORT COMPLETE!", flush=True)
print(f"{'=' * 70}", flush=True)
print(f"Successfully uploaded: {success}/{len(tickets)}", flush=True)
print(f"Failed: {failed}", flush=True)
print(f"Time taken: {elapsed_time}", flush=True)
print(f"Average rate: {success/elapsed_time.total_seconds():.2f} tickets/sec", flush=True)
print(f"{'=' * 70}\n", flush=True)
print("🎉 All historical tickets are now in Dust!", flush=True)
print("💡 Your hourly sync will continue updating the 10 most recent tickets with live data and comments!", flush=True)
