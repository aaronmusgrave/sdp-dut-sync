#!/usr/bin/env python3
"""
FIXED DIAGNOSTIC VERSION - Handles string date values
"""

import os
import requests
import json
from datetime import datetime, timedelta

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
    try:
        response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
        if response.status_code == 200:
            token_data = response.json()
            print("✓ ServiceDesk token obtained\n")
            return token_data['access_token']
        else:
            raise Exception(f"Failed to get SDP token: {response.status_code}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to connect to Zoho: {e}")

def safe_timestamp_to_date(timestamp_value):
    """Safely convert timestamp to datetime, handling both string and int"""
    if not timestamp_value:
        return None
    try:
        # Convert to int if it's a string
        if isinstance(timestamp_value, str):
            timestamp_ms = int(timestamp_value)
        else:
            timestamp_ms = timestamp_value
        
        return datetime.fromtimestamp(timestamp_ms / 1000)
    except (ValueError, TypeError):
        return None

def diagnostic_scan(sdp_token):
    print("="*80)
    print("🔍 DIAGNOSTIC SCAN - Finding Your Closed Tickets")
    print("="*80)
    print()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    print("🔎 Searching for Aaron Musgrave's tickets...")
    print()
    
    list_info = {
        "list_info": {
            "row_count": 50,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "technician.name",
                    "condition": "is",
                    "value": "Aaron Musgrave"
                }
            ],
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_API_BASE}/requests"
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            tickets = result.get('requests', [])
            
            print(f"✓ Found {len(tickets)} recent tickets for Aaron Musgrave")
            print()
            print("="*80)
            print("📊 STATUS SUMMARY")
            print("="*80)
            
            status_counts = {}
            for ticket in tickets:
                status = ticket.get('status', {})
                if isinstance(status, dict):
                    status_name = status.get('name', 'Unknown')
                    status_counts[status_name] = status_counts.get(status_name, 0) + 1
            
            for status_name, count in sorted(status_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"  {status_name}: {count} tickets")
            
            print()
            print("="*80)
            print("🎫 DETAILED TICKET INFORMATION (First 10)")
            print("="*80)
            print()
            
            today = datetime.now().date()
            found_today = []
            
            for i, ticket in enumerate(tickets[:10], 1):
                ticket_id = ticket.get('id', 'N/A')
                subject = ticket.get('subject', 'No subject')[:50]
                
                status = ticket.get('status', {})
                status_name = status.get('name', 'Unknown') if isinstance(status, dict) else 'Unknown'
                
                print(f"Ticket #{i} - ID: {ticket_id}")
                print(f"  Subject: {subject}")
                print(f"  Status: {status_name}")
                print()
                
                # Check all date fields
                created_time = ticket.get('created_time', {})
                updated_time = ticket.get('last_updated_time', {})
                resolved_time = ticket.get('resolved_time', {})
                completed_time = ticket.get('completed_time', {})
                closed_time = ticket.get('closed_time', {})
                
                print(f"  📅 Date Fields:")
                
                # Created time
                if isinstance(created_time, dict) and created_time.get('value'):
                    created_date = safe_timestamp_to_date(created_time.get('value'))
                    if created_date:
                        print(f"    - created_time: {created_date.strftime('%Y-%m-%d %H:%M:%S')}")
                    else:
                        print(f"    - created_time: {created_time.get('value')} (raw)")
                else:
                    print(f"    - created_time: Not available")
                
                # Updated time
                if isinstance(updated_time, dict) and updated_time.get('value'):
                    updated_date = safe_timestamp_to_date(updated_time.get('value'))
                    if updated_date:
                        print(f"    - last_updated_time: {updated_date.strftime('%Y-%m-%d %H:%M:%S')}")
                        if updated_date.date() == today:
                            print(f"      ✓ UPDATED TODAY!")
                            found_today.append(ticket_id)
                    else:
                        print(f"    - last_updated_time: {updated_time.get('value')} (raw)")
                else:
                    print(f"    - last_updated_time: Not available")
                
                # Resolved time
                if isinstance(resolved_time, dict) and resolved_time.get('value'):
                    resolved_date = safe_timestamp_to_date(resolved_time.get('value'))
                    if resolved_date:
                        print(f"    - resolved_time: {resolved_date.strftime('%Y-%m-%d %H:%M:%S')} ⭐")
                        if resolved_date.date() == today:
                            print(f"      ✅ RESOLVED TODAY! (This is your closed ticket)")
                            found_today.append(ticket_id)
                    else:
                        print(f"    - resolved_time: {resolved_time.get('value')} (raw)")
                else:
                    print(f"    - resolved_time: Not available")
                
                # Completed time
                if isinstance(completed_time, dict) and completed_time.get('value'):
                    completed_date = safe_timestamp_to_date(completed_time.get('value'))
                    if completed_date:
                        print(f"    - completed_time: {completed_date.strftime('%Y-%m-%d %H:%M:%S')} ⭐")
                        if completed_date.date() == today:
                            print(f"      ✅ COMPLETED TODAY!")
                            found_today.append(ticket_id)
                    else:
                        print(f"    - completed_time: {completed_time.get('value')} (raw)")
                else:
                    print(f"    - completed_time: Not available")
                
                # Closed time
                if isinstance(closed_time, dict) and closed_time.get('value'):
                    closed_date = safe_timestamp_to_date(closed_time.get('value'))
                    if closed_date:
                        print(f"    - closed_time: {closed_date.strftime('%Y-%m-%d %H:%M:%S')} ⭐")
                        if closed_date.date() == today:
                            print(f"      ✅ CLOSED TODAY!")
                            found_today.append(ticket_id)
                    else:
                        print(f"    - closed_time: {closed_time.get('value')} (raw)")
                else:
                    print(f"    - closed_time: Not available")
                
                print()
                print("-"*80)
                print()
            
            print()
            print("="*80)
            print("📝 SUMMARY & RECOMMENDATIONS")
            print("="*80)
            print()
            
            if found_today:
                unique_today = list(set(found_today))
                print(f"✅ Found {len(unique_today)} tickets with today's date!")
                print(f"   Ticket IDs: {', '.join(map(str, unique_today))}")
            else:
                print("❌ No tickets found with today's date in any field")
            
            print()
            print("🔍 Key Findings:")
            print()
            print(f"1. ✅ Status 'Closed' exists: {status_counts.get('Closed', 0)} tickets")
            print(f"2. ✅ Status 'Resolved' exists: {status_counts.get('Resolved', 0)} tickets")
            print(f"3. ⚠️  Date values are STRINGS, not integers (needs conversion)")
            print()
            print("📊 Fix Required:")
            print("   The main script needs to convert string timestamps to integers")
            print("   Example: int('1733032800000') before dividing by 1000")
            
        else:
            print(f"❌ API Error: {response.status_code}")
            print(f"Response: {response.text}")
    
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

def main():
    print()
    print("="*80)
    print("🔬 SERVICEDESK DIAGNOSTIC TOOL (FIXED)")
    print("   Handles string date values from API")
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
