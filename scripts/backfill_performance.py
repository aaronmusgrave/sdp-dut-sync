#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
v14.4: With working diagnostic mode
"""

import os
import requests
import json
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD', help='Process specific date')
parser.add_argument('--debug', action='store_true', help='Show debug output')
parser.add_argument('--diagnose', action='store_true', help='Run diagnostic mode')
parser.add_argument('--technician', type=str, help='Technician name for diagnostic')

TENANT_ID = os.environ.get('AZURE_TENANT_ID')
CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')

WORK_HOURS_PER_DAY = 8
PARALLEL_TECHNICIANS = 8
PARALLEL_TICKETS = 10
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'motorad.com')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

def get_graph_token():
    logger.info("Getting Microsoft Graph token...")
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'scope': 'https://graph.microsoft.com/.default', 'grant_type': 'client_credentials'}
    response = requests.post(url, data=data)
    if response.status_code == 200:
        logger.info("OK Graph token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get Graph token")

def get_sdp_token():
    logger.info("Getting ServiceDesk token...")
    payload = {'client_id': SDP_CLIENT_ID, 'client_secret': SDP_CLIENT_SECRET, 'refresh_token': SDP_REFRESH_TOKEN, 'grant_type': 'refresh_token'}
    response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
    if response.status_code == 200:
        logger.info("OK ServiceDesk token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get SDP token")

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
    headers = {'Authorization': f'Zoho-oauthtoken {sdp_token}', 'Accept': 'application/vnd.manageengine.sdp.v3+json'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json().get('request', {})
    except:
        pass
    return None

def diagnose_ticket_dates(tech_name, sdp_token, date=None):
    """DIAGNOSTIC: Check ticket date fields for a technician"""
    if date is None:
        date = datetime.now() - timedelta(days=1)
    
    target_date = date.date()
    
    print("\n" + "="*70, flush=True)
    print(f"DIAGNOSTIC MODE", flush=True)
    print("="*70, flush=True)
    print(f"Technician: {tech_name}", flush=True)
    print(f"Target date: {target_date}", flush=True)
    print("="*70, flush=True)
    
    headers = {'Authorization': f'Zoho-oauthtoken {sdp_token}', 'Accept': 'application/vnd.manageengine.sdp.v3+json'}
    
    # Search for closed tickets by this technician
    list_info = {
        "list_info": {
            "row_count": 20,
            "start_index": 1,
            "search_criteria": [
                {"field": "technician.name", "condition": "is", "value": tech_name},
                {"field": "status.name", "condition": "is", "values": ["Closed"]}
            ],
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    }
    
    try:
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code != 200:
            print(f"ERROR: Search failed with status {response.status_code}", flush=True)
            return
        
        result = response.json()
        tickets = result.get('requests', [])
        
        print(f"\nFound {len(tickets)} closed tickets for {tech_name}", flush=True)
        
        if not tickets:
            print("No tickets found!", flush=True)
            return
        
        # Check if completed_time is in list response
        print(f"\nChecking if completed_time is in list response:", flush=True)
        first_ticket = tickets[0]
        print(f"  Fields available: {', '.join(first_ticket.keys())}", flush=True)
        
        if 'completed_time' in first_ticket:
            print(f"  OK completed_time IS in list response", flush=True)
        else:
            print(f"  -- completed_time NOT in list response", flush=True)
        
        # Now check first 5 tickets in detail
        print(f"\nChecking first 5 tickets in detail:", flush=True)
        print("="*70, flush=True)
        
        ticket_ids = [t['id'] for t in tickets[:5]]
        matches_found = []
        
        for i, ticket_id in enumerate(ticket_ids, 1):
            ticket = get_ticket_details(ticket_id, sdp_token)
            if not ticket:
                print(f"\nTicket #{ticket_id} - Could not fetch details", flush=True)
                continue
            
            print(f"\nTicket #{ticket_id} ({i}/5)", flush=True)
            print(f"  Subject: {ticket.get('subject', 'N/A')[:50]}", flush=True)
            
            # Check all date fields
            date_fields = [
                ('completed_time', 'Completed'),
                ('resolved_time', 'Resolved'),
                ('closed_time', 'Closed'),
                ('last_updated_time', 'Last Updated'),
                ('status_change_time', 'Status Changed')
            ]
            
            ticket_matches = []
            
            for field_name, display_name in date_fields:
                field_value = ticket.get(field_name)
                if field_value:
                    dt = safe_timestamp_to_date(field_value)
                    if dt:
                        matches = dt.date() == target_date
                        if matches:
                            ticket_matches.append(field_name)
                            print(f"  OK {display_name:15s}: {dt.strftime('%Y-%m-%d %H:%M:%S')} <- MATCHES TARGET!", flush=True)
                        else:
                            print(f"     {display_name:15s}: {dt.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
                else:
                    print(f"  -- {display_name:15s}: (empty/null)", flush=True)
            
            if ticket_matches:
                matches_found.append((ticket_id, ticket_matches))
        
        # Summary
        print("\n" + "="*70, flush=True)
        print("SUMMARY", flush=True)
        print("="*70, flush=True)
        print(f"Tickets checked: {len(ticket_ids)}", flush=True)
        print(f"Tickets matching {target_date}: {len(matches_found)}", flush=True)
        
        if matches_found:
            print(f"\nMatching tickets:", flush=True)
            for ticket_id, fields in matches_found:
                print(f"  Ticket {ticket_id}: {', '.join(fields)}", flush=True)
            
            # Determine which field to use
            all_matching_fields = set()
            for _, fields in matches_found:
                all_matching_fields.update(fields)
            
            print(f"\nRECOMMENDATION:", flush=True)
            if 'completed_time' in all_matching_fields:
                print(f"  Use 'completed_time' field (currently used)", flush=True)
            elif 'resolved_time' in all_matching_fields:
                print(f"  Use 'resolved_time' field instead!", flush=True)
            elif 'closed_time' in all_matching_fields:
                print(f"  Use 'closed_time' field instead!", flush=True)
        else:
            print(f"\nNO TICKETS MATCH {target_date}!", flush=True)
            print(f"Possible issues:", flush=True)
            print(f"  - Tickets might have been closed on different date", flush=True)
            print(f"  - Timezone mismatch", flush=True)
            print(f"  - Wrong technician name spelling", flush=True)
        
        print("="*70, flush=True)
        
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()

def discover_active_technicians(sdp_token):
    # ... (keeping existing function)
    logger.info("Auto-discovering technicians...")
    headers = {'Authorization': f'Zoho-oauthtoken {sdp_token}', 'Accept': 'application/vnd.manageengine.sdp.v3+json'}
    list_info = {"list_info": {"row_count": 100, "start_index": 1, "search_criteria": [{"field": "status.name", "condition": "is not", "values": ["Closed", "Cancelled"]}]}}
    technicians = {}
    start_index = 1
    pages_checked = 0
    while pages_checked < 20:
        list_info['list_info']['start_index'] = start_index
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                if not tickets:
                    break
                for ticket in tickets:
                    tech = ticket.get('technician')
                    if tech and isinstance(tech, dict):
                        tech_name = tech.get('name')
                        tech_email = tech.get('email_id')
                        if tech_name and tech_name != 'Unassigned':
                            if tech_name not in technicians:
                                technicians[tech_name] = {'email': tech_email}
                if not result.get('list_info', {}).get('has_more_rows', False):
                    break
                start_index += 100
                pages_checked += 1
            else:
                break
        except Exception as e:
            logger.warning(f"Error during discovery: {e}")
            break
    logger.info(f"OK Discovered {len(technicians)} active technicians")
    return technicians

# Include all other functions from v14.4...
# (I'll provide complete file but truncating here for readability)

def main():
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # DIAGNOSTIC MODE
    if args.diagnose:
        print("="*70, flush=True)
        print("STARTING DIAGNOSTIC", flush=True)
        print("="*70, flush=True)
        
        sdp_token = get_sdp_token()
        
        if args.date:
            target_date = datetime.strptime(args.date, '%Y-%m-%d')
        else:
            target_date = datetime.now() - timedelta(days=1)
        
        if args.technician:
            tech_name = args.technician
        else:
            tech_name = input("Enter technician name: ").strip()
        
        if not tech_name:
            print("No technician name provided", flush=True)
            return
        
        diagnose_ticket_dates(tech_name, sdp_token, target_date)
        return
    
    # Normal mode would continue here...
    print("ERROR: Normal mode not shown for brevity. Use --diagnose flag.", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
