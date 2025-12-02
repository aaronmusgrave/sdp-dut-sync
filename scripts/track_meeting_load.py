#!/usr/bin/env python3
"""
DIAGNOSTIC: Find Aaron's missing tickets - FIXED
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
    print("🔍 DIAGNOSTIC: Finding Aaron's Closed Tickets")
    print("="*80)
    print()
    
    sdp_token = get_sdp_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Use YESTERDAY since it's now Dec 2
    yesterday = datetime.now() - timedelta(days=1)
    target_date = yesterday.date()
    target_str = target_date.strftime('%Y-%m-%d')
    
    print(f"📅 Searching for tickets closed on {target_str} (yesterday)\n")
    print(f"   (Script is running on {datetime.now().strftime('%Y-%m-%d')})\n")
    
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
    print(f"📥 Fetching details for first 30...\n")
    
    found_target = []
    found_other = []
    
    for ticket_id in ticket_ids[:30]:
        ticket = get_ticket_details(ticket_id, sdp_token)
        if not ticket:
            print(f"  ⚠️  Could not fetch ticket #{ticket_id}")
            continue
        
        subject = ticket.get('subject', 'No subject')[:60]
        status = ticket.get('status', {})
        status_name = status.get('name', 'Unknown') if isinstance(status, dict) else 'Unknown'
        
        # FIXED: Handle technician field safely
        tech_obj = ticket.get('technician')
        if isinstance(tech_obj, dict):
            tech = tech_obj.get('name', 'Unknown')
        else:
            tech = 'Unassigned'
        
        # Check all date fields
        created_time = safe_timestamp_to_date(ticket.get('created_time'))
        updated_time = safe_timestamp_to_date(ticket.get('last_updated_time'))
        resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
        completed_time = safe_timestamp_to_date(ticket.get('completed_time'))
        closed_time = safe_timestamp_to_date(ticket.get('closed_time'))
        
        is_target = False
        date_field_used = None
        date_value = None
        
        if completed_time and completed_time.date() == target_date:
            is_target = True
            date_field_used = "completed_time"
            date_value = completed_time
        elif resolved_time and resolved_time.date() == target_date:
            is_target = True
            date_field_used = "resolved_time"
            date_value = resolved_time
        elif closed_time and closed_time.date() == target_date:
            is_target = True
            date_field_used = "closed_time"
            date_value = closed_time
        
        info = {
            'id': ticket_id,
            'subject': subject,
            'status': status_name,
            'tech': tech,
            'created': created_time,
            'updated': updated_time,
            'resolved': resolved_time,
            'completed': completed_time,
            'closed': closed_time
        }
        
        if is_target:
            found_target.append((info, date_field_used, date_value))
        else:
            found_other.append(info)
    
    print("="*80)
    print(f"✅ TICKETS CLOSED ON {target_str}")
    print("="*80)
    print()
    
    if found_target:
        print(f"Found {len(found_target)} tickets:\n")
        for i, (info, field, date_val) in enumerate(found_target, 1):
            print(f"{i}. Ticket #{info['id']}")
            print(f"   Subject: {info['subject']}")
            print(f"   Technician: {info['tech']}")
            print(f"   Status: {info['status']}")
            print(f"   ✅ {field}: {date_val.strftime('%Y-%m-%d %H:%M:%S')}")
            print()
    else:
        print(f"❌ No tickets found with date {target_str}!\n")
    
    print("="*80)
    print(f"📊 OTHER RECENT CLOSED TICKETS (Not {target_str})")
    print("="*80)
    print()
    
    if found_other:
        print(f"Showing first 10 of {len(found_other)} other closed tickets:\n")
        for i, info in enumerate(found_other[:10], 1):
            print(f"{i}. Ticket #{info['id']}")
            print(f"   Subject: {info['subject']}")
            print(f"   Technician: {info['tech']}")
            print(f"   Status: {info['status']}")
            if info['completed']:
                print(f"   completed_time: {info['completed'].strftime('%Y-%m-%d %H:%M')}")
            elif info['resolved']:
                print(f"   resolved_time: {info['resolved'].strftime('%Y-%m-%d %H:%M')}")
            elif info['closed']:
                print(f"   closed_time: {info['closed'].strftime('%Y-%m-%d %H:%M')}")
            else:
                print(f"   ⚠️  No date fields set!")
            print()
    
    print("="*80)
    print("📝 SUMMARY")
    print("="*80)
    print()
    print(f"Date searched: {target_str}")
    print(f"Tickets found: {len(found_target)}")
    print(f"Expected: 7 tickets")
    print()
    
    if len(found_target) < 7:
        print("⚠️  MISSING TICKETS!")
        print()
        print("Possible reasons:")
        print("1. ❓ Some tickets status is 'Resolved' not 'Closed'")
        print("2. ❓ Some tickets don't have completed_time/resolved_time set")
        print("3. ❓ Some tickets assigned to different technician")
        print("4. ❓ Some tickets closed on a different date than you think")
        print()
        print("💡 Next steps:")
        print("   1. Check ServiceDesk for those 7 tickets manually")
        print("   2. Verify they all show status 'Closed'")
        print("   3. Check the 'Completed Time' or 'Resolved Time' field")
        print("   4. Make sure they're assigned to 'Aaron Musgrave'")
    else:
        print("✅ All expected tickets found!")

if __name__ == "__main__":
    from datetime import timedelta
    main()
