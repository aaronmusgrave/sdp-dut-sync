#!/usr/bin/env python3
"""
DIAGNOSTIC VERSION - Shows raw ticket data to identify field names and values
Run this to see what the ServiceDesk API is actually returning
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

def diagnostic_scan(sdp_token):
    print("="*80)
    print("🔍 DIAGNOSTIC SCAN - Finding Your Closed Tickets")
    print("="*80)
    print()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Get tickets for Aaron Musgrave
    print("🔎 Searching for Aaron Musgrave's tickets...")
    print()
    
    # Search 1: ALL tickets (no status filter) sorted by last updated
    list_info = {
        "list_info": {
            "row_count": 50,  # Get 50 most recent
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
            
            # Collect all unique statuses
            status_counts = {}
            for ticket in tickets:
                status = ticket.get('status', {})
                if isinstance(status, dict):
                    status_name = status.get('name', 'Unknown')
                    status_counts[status_name] = status_counts.get(status_name, 0) + 1
            
            # Display status breakdown
            for status_name, count in sorted(status_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"  {status_name}: {count} tickets")
            
            print()
            print("="*80)
            print("🎫 DETAILED TICKET INFORMATION")
            print("="*80)
            print()
            
            # Show detailed info for first 10 tickets
            today = datetime.now().date()
            found_today = []
            
            for i, ticket in enumerate(tickets[:10], 1):
                ticket_id = ticket.get('id', 'N/A')
                subject = ticket.get('subject', 'No subject')[:50]
                
                status = ticket.get('status', {})
                status_name = status.get('name', 'Unknown') if isinstance(status, dict) else 'Unknown'
                
                # Check ALL possible date fields
                created_time = ticket.get('created_time', {})
                updated_time = ticket.get('last_updated_time', {})
                resolved_time = ticket.get('resolved_time', {})
                completed_time = ticket.get('completed_time', {})
                closed_time = ticket.get('closed_time', {})
                
                print(f"Ticket #{i} - ID: {ticket_id}")
                print(f"  Subject: {subject}")
                print(f"  Status: {status_name}")
                print(f"  Status Object: {status}")
                print()
                
                # Show all date fields
                print(f"  📅 Date Fields:")
                
                if isinstance(created_time, dict) and created_time.get('value'):
                    created_ms = created_time.get('value')
                    created_date = datetime.fromtimestamp(created_ms / 1000)
                    print(f"    - created_time: {created_date.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    print(f"    - created_time: {created_time}")
                
                if isinstance(updated_time, dict) and updated_time.get('value'):
                    updated_ms = updated_time.get('value')
                    updated_date = datetime.fromtimestamp(updated_ms / 1000)
                    print(f"    - last_updated_time: {updated_date.strftime('%Y-%m-%d %H:%M:%S')}")
                    if updated_date.date() == today:
                        found_today.append(ticket_id)
                else:
                    print(f"    - last_updated_time: {updated_time}")
                
                if isinstance(resolved_time, dict) and resolved_time.get('value'):
                    resolved_ms = resolved_time.get('value')
                    resolved_date = datetime.fromtimestamp(resolved_ms / 1000)
                    print(f"    - resolved_time: {resolved_date.strftime('%Y-%m-%d %H:%M:%S')} ⭐")
                    if resolved_date.date() == today:
                        print(f"      ✓ RESOLVED TODAY!")
                        found_today.append(ticket_id)
                else:
                    print(f"    - resolved_time: {resolved_time}")
                
                if isinstance(completed_time, dict) and completed_time.get('value'):
                    completed_ms = completed_time.get('value')
                    completed_date = datetime.fromtimestamp(completed_ms / 1000)
                    print(f"    - completed_time: {completed_date.strftime('%Y-%m-%d %H:%M:%S')} ⭐")
                    if completed_date.date() == today:
                        print(f"      ✓ COMPLETED TODAY!")
                        found_today.append(ticket_id)
                else:
                    print(f"    - completed_time: {completed_time}")
                
                if isinstance(closed_time, dict) and closed_time.get('value'):
                    closed_ms = closed_time.get('value')
                    closed_date = datetime.fromtimestamp(closed_ms / 1000)
                    print(f"    - closed_time: {closed_date.strftime('%Y-%m-%d %H:%M:%S')} ⭐")
                    if closed_date.date() == today:
                        print(f"      ✓ CLOSED TODAY!")
                        found_today.append(ticket_id)
                else:
                    print(f"    - closed_time: {closed_time}")
                
                print()
                print(f"  🔍 RAW TICKET DATA (first 500 chars):")
                ticket_str = json.dumps(ticket, indent=2)[:500]
                print(f"  {ticket_str}...")
                print()
                print("-"*80)
                print()
            
            print()
            print("="*80)
            print("📝 SUMMARY & RECOMMENDATIONS")
            print("="*80)
            print()
            
            if found_today:
                print(f"✅ Found {len(set(found_today))} tickets updated/closed today!")
                print(f"   Ticket IDs: {', '.join(map(str, set(found_today)))}")
            else:
                print("❌ No tickets found with today's date in any date field")
            
            print()
            print("🔍 What to check:")
            print()
            print("1. STATUS NAMES:")
            print(f"   - Your ServiceDesk uses these status names: {', '.join(status_counts.keys())}")
            print(f"   - Script searches for: 'Closed', 'Resolved'")
            if 'Closed' not in status_counts and 'Resolved' not in status_counts:
                print(f"   ⚠️  MISMATCH! Your tickets don't use 'Closed' or 'Resolved'")
                print(f"   → Recommendation: Update script to search for: {list(status_counts.keys())}")
            
            print()
            print("2. DATE FIELDS:")
            print("   - Check which date field is populated for closed tickets above")
            print("   - Script uses: resolved_time")
            print("   → Recommendation: Use the field that shows today's date for your closed tickets")
            
            print()
            print("3. TECHNICIAN NAME:")
            print("   - Script searches for: 'Aaron Musgrave'")
            print(f"   - Found {len(tickets)} tickets with this exact name")
            if len(tickets) == 0:
                print("   ⚠️  WARNING: No tickets found! Check if technician name is exact match")
            
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
    print("🔬 SERVICEDESK DIAGNOSTIC TOOL")
    print("   This will show you exactly what the API returns")
    print("="*80)
    print()
    
    sdp_token = get_sdp_token()
    diagnostic_scan(sdp_token)
    
    print()
    print("="*80)
    print("✅ DIAGNOSTIC COMPLETE")
    print("="*80)
    print()
    print("📧 Send the output above to diagnose the issue!")
    print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
