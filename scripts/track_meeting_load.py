#!/usr/bin/env python3
"""
DIAGNOSTIC v3 - Fetches individual tickets to get all fields
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
    print("Getting ServiceDesk token...")
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
    if response.status_code == 200:
        print("✓ Token obtained\n")
        return response.json()['access_token']
    raise Exception(f"Failed to get token: {response.status_code}")

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
    """Fetch full details for a specific ticket"""
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

def diagnostic_scan(sdp_token):
    print("="*80)
    print("🔍 DIAGNOSTIC SCAN - Fetching Full Ticket Details")
    print("="*80)
    print()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Get list of tickets for Aaron
    list_info = {
        "list_info": {
            "row_count": 20,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "technician.name",
                    "condition": "is",
                    "value": "Aaron Musgrave"
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
        print(f"❌ Failed to get ticket list: {response.status_code}")
        return
    
    tickets = response.json().get('requests', [])
    print(f"✓ Found {len(tickets)} tickets for Aaron Musgrave")
    print(f"📥 Fetching full details for first 10 tickets...\n")
    
    today = datetime.now().date()
    found_today = []
    
    for i, ticket_summary in enumerate(tickets[:10], 1):
        ticket_id = ticket_summary.get('id')
        
        print(f"Ticket #{i} - ID: {ticket_id}")
        print(f"  Fetching full details...")
        
        # Get full ticket details
        ticket = get_ticket_details(ticket_id, sdp_token)
        
        if not ticket:
            print(f"  ❌ Could not fetch details")
            print()
            continue
        
        subject = ticket.get('subject', 'No subject')[:60]
        status = ticket.get('status', {})
        status_name = status.get('name', 'Unknown') if isinstance(status, dict) else 'Unknown'
        
        print(f"  Subject: {subject}")
        print(f"  Status: {status_name}")
        print()
        print(f"  📅 Date Fields (from full ticket details):")
        
        # Check ALL possible date fields
        date_fields = [
            'created_time',
            'last_updated_time',
            'resolved_time',
            'completed_time',
            'closed_time',
            'due_by_time',
            'first_response_due_by_time'
        ]
        
        has_today_date = False
        
        for field_name in date_fields:
            field_value = ticket.get(field_name)
            
            if field_value:
                date_obj = safe_timestamp_to_date(field_value)
                if date_obj:
                    date_str = date_obj.strftime('%Y-%m-%d %H:%M:%S')
                    marker = ""
                    
                    if date_obj.date() == today:
                        marker = " ✅ TODAY!"
                        has_today_date = True
                        found_today.append({
                            'id': ticket_id,
                            'field': field_name,
                            'date': date_obj,
                            'status': status_name
                        })
                    
                    print(f"    - {field_name}: {date_str}{marker}")
                else:
                    print(f"    - {field_name}: {field_value} (could not parse)")
            else:
                print(f"    - {field_name}: Not available")
        
        if has_today_date:
            print(f"\n  🎯 THIS TICKET WAS UPDATED/CLOSED TODAY!")
        
        print()
        print("-"*80)
        print()
    
    print()
    print("="*80)
    print("📝 SUMMARY & RECOMMENDATIONS")
    print("="*80)
    print()
    
    if found_today:
        print(f"✅ Found {len(found_today)} tickets with today's date!\n")
        
        for item in found_today:
            print(f"   Ticket #{item['id']}")
            print(f"     Status: {item['status']}")
            print(f"     Field: {item['field']}")
            print(f"     Time: {item['date'].strftime('%Y-%m-%d %H:%M:%S')}")
            print()
        
        # Determine which field to use
        field_usage = {}
        for item in found_today:
            field_usage[item['field']] = field_usage.get(item['field'], 0) + 1
        
        print("🔍 Which date field to use:")
        for field, count in sorted(field_usage.items(), key=lambda x: x[1], reverse=True):
            print(f"   {field}: {count} tickets")
        
        recommended_field = max(field_usage.items(), key=lambda x: x[1])[0]
        print(f"\n   ✅ RECOMMENDATION: Use '{recommended_field}' field")
        
    else:
        print("❌ No tickets found with today's date")
        print("\n   Possible reasons:")
        print("   1. No tickets closed today yet")
        print("   2. Date fields not populated")
        print("   3. Wrong technician name")
    
    print()
    print("🔧 Next Steps:")
    print("   1. The main script needs to fetch individual ticket details")
    print("   2. Can't rely on the /requests search - it doesn't include all fields")
    print("   3. Must use /requests/{id} endpoint for each ticket")

def main():
    print()
    print("="*80)
    print("🔬 SERVICEDESK DIAGNOSTIC v3")
    print("   Fetches full ticket details individually")
    print("="*80)
    print()
    
    sdp_token = get_sdp_token()
    diagnostic_scan(sdp_token)
    
    print()
    print("="*80)
    print("✅ DIAGNOSTIC COMPLETE")
    print("="*80)
    print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
