#!/usr/bin/env python3
"""
DIAGNOSTIC: Find Aaron's missing tickets
"""

import os
import requests
import json
from datetime import datetime

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

def get_sdp_token():
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
    if response.status_code == 200:
        return response.json()['access_token']
    raise Exception(f"Failed to get token")

def safe_timestamp_to_date(value):
    if not value:
        return None
    try:
        if isinstance(value, dict):
            value = value.get('value')
        if isinstance(value, str):
            value = int(value)
        return datetime.fromtimestamp(value / 1000)
    except:
        return None

def get_ticket_details(ticket_id, sdp_token):
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json().get('request', {})
    except:
        pass
    return None

def main():
    print("="*80)
    print("🔍 DIAGNOSTIC: Finding Aaron's 7 Tickets")
    print("="*80)
    print()
    
    sdp_token = get_sdp_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    today = datetime.now().date()
    today_str = today.strftime('%Y-%m-%d')
    
    print(f"📅 Searching for tickets closed TODAY ({today_str})\n")
    
    # Search for Aaron's closed tickets
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "technician.name",
                    "condition": "is",
                    "value": "Aaron Musgrave"
                },
                {
                    "field": "status.name",
                    "condition": "is",
                    "values": ["Closed"]
                }
            ],
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_API_BASE}/requests"
    
    response = requests.get(url, headers=headers, params=params, timeout=30)
    
    if response.status_code != 200:
        print(f"❌ Failed to get tickets: {response.status_code}")
        return
    
    result = response.json()
    ticket_ids = [t['id'] for t in result.get('requests', [])]
    
    print(f"✓ Found {len(ticket_ids)} closed tickets assigned to Aaron Musgrave")
    print(f"📥 Fetching details for first 20...\n")
    
    found_today = []
    found_other = []
    
    for ticket_id in ticket_ids[:20]:
        ticket = get_ticket_details(ticket_id, sdp_token)
        if not ticket:
            continue
        
        subject = ticket.get('subject', 'No subject')[:60]
        status = ticket.get('status', {}).get('name', 'Unknown')
        tech = ticket.get('technician', {}).get('name', 'Unknown')
        
        # Check all date fields
        created_time = safe_timestamp_to_date(ticket.get('created_time'))
        updated_time = safe_timestamp_to_date(ticket.get('last_updated_time'))
        resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
        completed_time = safe_timestamp_to_date(ticket.get('completed_time'))
        closed_time = safe_timestamp_to_date(ticket.get('closed_time'))
        
        is_today = False
        date_field_used = None
        date_value = None
        
        if completed_time and completed_time.date() == today:
            is_today = True
            date_field_used = "completed_time"
            date_value = completed_time
        elif resolved_time and resolved_time.date() == today:
            is_today = True
            date_field_used = "resolved_time"
            date_value = resolved_time
        elif closed_time and closed_time.date() == today:
            is_today = True
            date_field_used = "closed_time"
            date_value = closed_time
        
        info = {
            'id': ticket_id,
            'subject': subject,
            'status': status,
            'tech': tech,
            'created': created_time,
            'updated': updated_time,
            'resolved': resolved_time,
            'completed': completed_time,
            'closed': closed_time
        }
        
        if is_today:
            found_today.append((info, date_field_used, date_value))
        else:
            found_other.append(info)
    
    print("="*80)
    print(f"✅ TICKETS CLOSED TODAY ({today_str})")
    print("="*80)
    print()
    
    if found_today:
        print(f"Found {len(found_today)} tickets:\n")
        for i, (info, field, date_val) in enumerate(found_today, 1):
            print(f"{i}. Ticket #{info['id']}")
            print(f"   Subject: {info['subject']}")
            print(f"   Technician: {info['tech']}")
            print(f"   Status: {info['status']}")
            print(f"   ✅ {field}: {date_val.strftime('%Y-%m-%d %H:%M:%S')}")
            print()
    else:
        print("❌ No tickets found with today's date!\n")
    
    print("="*80)
    print(f"📊 OTHER RECENT CLOSED TICKETS (Not Today)")
    print("="*80)
    print()
    
    if found_other:
        print(f"Found {len(found_other)} recent closed tickets:\n")
        for i, info in enumerate(found_other[:5], 1):
            print(f"{i}. Ticket #{info['id']}")
            print(f"   Subject: {info['subject']}")
            print(f"   Technician: {info['tech']}")
            print(f"   Status: {info['status']}")
            print(f"   completed_time: {info['completed'].strftime('%Y-%m-%d %H:%M') if info['completed'] else 'Not set'}")
            print(f"   resolved_time: {info['resolved'].strftime('%Y-%m-%d %H:%M') if info['resolved'] else 'Not set'}")
            print()
    
    print("="*80)
    print("📝 SUMMARY")
    print("="*80)
    print()
    print(f"Tickets with TODAY's date: {len(found_today)}")
    print(f"Expected: 7 tickets")
    print()
    
    if len(found_today) < 7:
        print("⚠️  MISSING TICKETS!")
        print()
        print("Possible reasons:")
        print("1. Some tickets status is 'Resolved' not 'Closed'")
        print("2. Some tickets don't have completed_time/resolved_time set")
        print("3. Some tickets assigned to different technician")
        print("4. Some tickets closed after this script ran")
        print()
        print("💡 Check ServiceDesk and verify:")
        print("   - All 7 tickets have status 'Closed' (not 'Resolved')")
        print("   - All 7 tickets are assigned to 'Aaron Musgrave'")
        print("   - All 7 tickets have 'Completed Time' field set to today")

if __name__ == "__main__":
    main()
