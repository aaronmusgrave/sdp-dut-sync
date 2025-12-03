#!/usr/bin/env python3
"""
Backfill performance data - WITH REAL-TIME PROGRESS
Uses optimized v14.2 tracker script with parallel workers
Processes multiple dates simultaneously for maximum speed
"""

import subprocess
import time
import json
import os
import sys
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed

#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
v14.3: ULTRA-OPTIMIZED - Date range filter in API search
- Filters by completed_time date range in API (10-50x faster!)
- Only fetches tickets from target date
- Parallel ticket fetching for remaining tickets
- Expected: 30-60 seconds per date (vs 5+ minutes!)
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
parser.add_argument('--diagnose', action='store_true', help='Run diagnostic mode to check date fields')

TENANT_ID = os.environ.get('AZURE_TENANT_ID')
CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')

# Configuration
DAYS_TO_BACKFILL = 20
WORK_HOURS_PER_DAY = 8
PARALLEL_TECHNICIANS = 8  # Process 8 technicians at once
PARALLEL_TICKETS = 10      # Fetch 10 ticket details at once (per technician)
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'motorad.com')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

def get_graph_token():
    logger.info("Getting Microsoft Graph token...")
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    response = requests.post(url, data=data)
    if response.status_code == 200:
        logger.info("OK Graph token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get Graph token")

def get_sdp_token():
    logger.info("Getting ServiceDesk token...")
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
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
    """Fetch full ticket details including all date fields"""
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

def diagnose_ticket_dates(tech_name, sdp_token, date=None):
    """DIAGNOSTIC: Check which date fields are actually populated"""
    if date is None:
        date = datetime.now() - timedelta(days=1)
    
    target_date = date.date()
    
    print("\n" + "="*70, flush=True)
    print(f"DIAGNOSTIC MODE - CHECKING DATE FIELDS", flush=True)
    print("="*70, flush=True)
    print(f"Technician: {tech_name}", flush=True)
    print(f"Target date: {target_date}", flush=True)
    print("="*70, flush=True)
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 10,
            "start_index": 1,
            "search_criteria": [
                {"field": "technician.name", "condition": "is", "value": tech_name},
                {"field": "status.name", "condition": "is", "values": ["Closed"]}
            ],
            "sort_field": "completed_time",
            "sort_order": "desc"
        }
    }
    
    try:
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code != 200:
            print(f"Search failed: {response.status_code}", flush=True)
            return
        
        result = response.json()
        ticket_ids = [t['id'] for t in result.get('requests', [])]
        
        print(f"\nFound {len(ticket_ids)} recent closed tickets", flush=True)
        print(f"Showing first 5 with full date field details:\n", flush=True)
        
        tickets_on_target_date = []
        
        for i, ticket_id in enumerate(ticket_ids[:5], 1):
            ticket = get_ticket_details(ticket_id, sdp_token)
            if not ticket:
                print(f"Could not fetch details for ticket {ticket_id}", flush=True)
                continue
            
            print(f"Ticket #{ticket_id} - {i}/5", flush=True)
            
            date_fields = [
                ('completed_time', 'Completed Time'),
                ('resolved_time', 'Resolved Time'),
                ('closed_time', 'Closed Time'),
                ('last_updated_time', 'Last Updated')
            ]
            
            matches_found = []
            
            for field_name, display_name in date_fields:
                field_value = ticket.get(field_name)
                if field_value:
                    dt = safe_timestamp_to_date(field_value)
                    if dt:
                        matches = dt.date() == target_date
                        if matches:
                            matches_found.append(field_name)
                            print(f"  OK {display_name:20s}: {dt.strftime('%Y-%m-%d %H:%M:%S')} MATCHES", flush=True)
                        else:
                            print(f"     {display_name:20s}: {dt.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
                else:
                    print(f"  -- {display_name:20s}: (empty)", flush=True)
            
            if matches_found:
                tickets_on_target_date.append((ticket_id, matches_found))
            
            print(flush=True)
        
        print("="*70, flush=True)
        print(f"Tickets matching {target_date}: {len(tickets_on_target_date)}", flush=True)
        print("="*70, flush=True)
        
    except Exception as e:
        print(f"Error: {e}", flush=True)

def discover_active_technicians(sdp_token):
    logger.info("Auto-discovering technicians...")
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {"field": "status.name", "condition": "is not", "values": ["Closed", "Cancelled"]}
            ]
        }
    }
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

def resolve_technician_email(tech_name, tech_info, graph_token):
    if tech_info.get('email'):
        return tech_info['email']
    try:
        headers = {'Authorization': f'Bearer {graph_token}', 'ConsistencyLevel': 'eventual'}
        url = "https://graph.microsoft.com/v1.0/users"
        params = {'$filter': f"displayName eq '{tech_name}'", '$select': 'mail,userPrincipalName'}
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            users = response.json().get('value', [])
            if users:
                return users[0].get('mail') or users[0].get('userPrincipalName')
    except Exception as e:
        logger.debug(f"Error resolving email for {tech_name}: {e}")
    name_parts = tech_name.lower().split()
    if len(name_parts) >= 2:
        return f"{name_parts[0]}.{name_parts[-1]}@{EMAIL_DOMAIN}"
    return None

def get_tech_meetings(email, graph_token, date=None):
    if not email:
        return {'meeting_count': 0, 'total_hours': 0}
    if date is None:
        date = datetime.now()
    start_time = date.replace(hour=8, minute=0, second=0, microsecond=0)
    end_time = date.replace(hour=17, minute=0, second=0, microsecond=0)
    url = f"https://graph.microsoft.com/v1.0/users/{email}/calendar/calendarView"
    headers = {'Authorization': f'Bearer {graph_token}', 'Prefer': 'outlook.timezone="Central Standard Time"'}
    params = {
        'startDateTime': start_time.isoformat(),
        'endDateTime': end_time.isoformat(),
        '$select': 'isAllDay,isCancelled,responseStatus,start,end'
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 200:
            events = response.json().get('value', [])
            total_minutes = 0
            count = 0
            for event in events:
                if event.get('isCancelled') or event.get('responseStatus', {}).get('response') == 'declined':
                    continue
                if event.get('isAllDay'):
                    duration_minutes = WORK_HOURS_PER_DAY * 60
                else:
                    start = datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(event['end']['dateTime'].replace('Z', '+00:00'))
                    meeting_start = max(start, start_time)
                    meeting_end = min(end, end_time)
                    if meeting_end > meeting_start:
                        duration_minutes = (meeting_end - meeting_start).total_seconds() / 60
                    else:
                        continue
                duration_minutes = min(duration_minutes, WORK_HOURS_PER_DAY * 60)
                total_minutes += duration_minutes
                count += 1
            total_minutes = min(total_minutes, WORK_HOURS_PER_DAY * 60)
            return {'meeting_count': count, 'total_hours': round(total_minutes / 60, 2)}
        else:
            logger.debug(f"Calendar API returned {response.status_code} for {email}")
    except Exception as e:
        logger.debug(f"Error fetching meetings for {email}: {e}")
    return {'meeting_count': 0, 'total_hours': 0}

def count_closed_tickets_optimized(tech_name, sdp_token, date=None):
    """
    v14.3 ULTRA-OPTIMIZED: Date range filter in API search!
    - Filters by completed_time date range (only gets tickets from target date)
    - 10-50x fewer tickets to fetch
    - Expected: 30-60 seconds per date (vs 5+ minutes!)
    """
    if date is None:
        date = datetime.now()
    if date.date() >= datetime.now().date():
        date = datetime.now() - timedelta(days=1)
    
    target_date = date.date()
    target_date_str = target_date.strftime('%Y-%m-%d')
    
    logger.debug(f"Counting closed for {tech_name} on {target_date}")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # OPTIMIZATION: Add date range filter to search criteria!
    list_info = {
        "list_info": {
            "row_count": 100,  # Reduced from 500 (we're filtering by date now)
            "start_index": 1,
            "search_criteria": [
                {"field": "technician.name", "condition": "is", "value": tech_name},
                {"field": "status.name", "condition": "is", "values": ["Closed"]},
                {
                    "field": "completed_time",
                    "condition": "between",
                    "values": [
                        f"{target_date_str} 00:00:00",
                        f"{target_date_str} 23:59:59"
                    ]
                }
            ],
            "sort_field": "completed_time",
            "sort_order": "desc"
        }
    }
    
    try:
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code != 200:
            logger.warning(f"Ticket search failed for {tech_name}: {response.status_code}")
            return 0
        
        result = response.json()
        tickets = result.get('requests', [])
        ticket_ids = [t['id'] for t in tickets]
        
        logger.debug(f"  Found {len(ticket_ids)} tickets for {tech_name} on {target_date}")
        
        count = 0
        
        # Fetch ticket details in parallel (10 at a time)
        def fetch_and_verify_ticket(ticket_id):
            """Fetch ticket and verify it matches criteria"""
            ticket = get_ticket_details(ticket_id, sdp_token)
            if not ticket:
                return False
            
            # Verify technician
            tech = ticket.get('technician')
            if not tech or not isinstance(tech, dict):
                return False
            
            ticket_tech_name = tech.get('name', '')
            if ticket_tech_name != tech_name:
                return False
            
            # Verify status
            status = ticket.get('status')
            if not status or not isinstance(status, dict):
                return False
            
            status_name = status.get('name', '')
            if status_name != 'Closed':
                return False
            
            # Verify completed_time (should already match from filter, but double-check)
            completed_time = safe_timestamp_to_date(ticket.get('completed_time'))
            
            if not completed_time:
                logger.debug(f"    Ticket {ticket_id} has no completed_time")
                return False
            
            if completed_time.date() == target_date:
                logger.debug(f"    OK Ticket {ticket_id} completed on {target_date}")
                return True
            
            return False
        
        # Process tickets in parallel
        with ThreadPoolExecutor(max_workers=PARALLEL_TICKETS) as executor:
            futures = [executor.submit(fetch_and_verify_ticket, tid) for tid in ticket_ids]
            
            for future in as_completed(futures):
                if future.result():
                    count += 1
        
        logger.debug(f"  OK {tech_name}: {count} tickets completed on {target_date}")
        return count
        
    except Exception as e:
        logger.warning(f"Error counting tickets for {tech_name}: {e}")
        return 0

def get_tech_ticket_metrics(tech_name, sdp_token, date=None):
    if date is None:
        date = datetime.now()
    headers = {'Authorization': f'Zoho-oauthtoken {sdp_token}', 'Accept': 'application/vnd.manageengine.sdp.v3+json'}
    metrics = {'active_tickets': 0, 'closed_today': 0, 'updated_today': 0}
    
    active_list_info = {
        "list_info": {
            "row_count": 250,
            "start_index": 1,
            "search_criteria": [
                {"field": "technician.name", "condition": "is", "value": tech_name},
                {"field": "status.name", "condition": "is not", "values": ["Closed", "Cancelled"]}
            ]
        }
    }
    try:
        params = {'input_data': json.dumps(active_list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 200:
            result = response.json()
            metrics['active_tickets'] = len(result.get('requests', []))
        else:
            logger.debug(f"Active ticket search returned {response.status_code} for {tech_name}")
    except Exception as e:
        logger.debug(f"Error getting active tickets for {tech_name}: {e}")
    
    metrics['closed_today'] = count_closed_tickets_optimized(tech_name, sdp_token, date)
    metrics['updated_today'] = 0
    return metrics

def calculate_adjusted_performance(meeting_data, ticket_metrics):
    meeting_hours = meeting_data['total_hours']
    available_hours = max(0.5, WORK_HOURS_PER_DAY - meeting_hours)
    tickets_closed = ticket_metrics['closed_today']
    velocity_closed = tickets_closed / available_hours if available_hours > 0 else 0
    score = min(40, velocity_closed * 10)
    active = ticket_metrics['active_tickets']
    if 5 <= active <= 15:
        score += 15
    elif active < 5:
        score += active * 3
    else:
        score += max(0, 15 - (active - 15) * 0.5)
    score = max(0, min(100, score))
    return {
        'available_hours': round(available_hours, 2),
        'velocity_closed': round(velocity_closed, 2),
        'adjusted_score': round(score, 1),
        'meeting_percentage': round((meeting_hours / WORK_HOURS_PER_DAY) * 100, 1)
    }

def process_single_technician(tech_name, tech_info, graph_token, sdp_token, target_date):
    """Process a single technician - designed for parallel execution"""
    logger.info(f"  Processing {tech_name}...")
    meetings = get_tech_meetings(tech_info.get('email'), graph_token, target_date)
    tickets = get_tech_ticket_metrics(tech_name, sdp_token, target_date)
    performance = calculate_adjusted_performance(meetings, tickets)
    logger.info(f"    -> {tickets['closed_today']} closed, {meetings['meeting_count']} meetings, score {performance['adjusted_score']}")
    return {
        'name': tech_name,
        'meetings': meetings,
        'tickets': tickets,
        'performance': performance
    }

def save_metrics_to_file(tech_data, date):
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    output = {}
    for tech, data in tech_data.items():
        output[tech] = {
            'date': date.isoformat(),
            'meetings': {'count': data['meetings']['meeting_count'], 'hours': data['meetings']['total_hours']},
            'tickets': data['tickets'],
            'performance': data['performance']
        }
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    logger.info(f"File saved: {filename}")
    return filename

def upload_to_dust(tech_data, date):
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        logger.warning("Dust API credentials not found, skipping upload")
        return
    logger.info("Uploading to Dust...")
    DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    headers = {'Authorization': f'Bearer {DUST_API_KEY}', 'Content-Type': 'application/json'}
    date_str = date.strftime('%Y-%m-%d')
    uploaded = 0
    failed = 0
    for tech_name, data in tech_data.items():
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        document = {
            "document_id": f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp": int(date.timestamp() * 1000),
            "tags": ["performance-metrics", f"technician:{tech_name.lower().replace(' ', '-')}", f"date:{date_str}"],
            "text": f"""# {tech_name} - {date.strftime('%B %d, %Y')}

## Calendar & Availability
- **Meetings:** {m['meeting_count']} meetings
- **Meeting Time:** {m['total_hours']} hours ({p['meeting_percentage']}%)
- **Available:** {p['available_hours']} hours

## Ticket Activity
- **Closed:** {t['closed_today']}
- **Updated:** {t['updated_today']}
- **Active:** {t['active_tickets']}

## Performance
- **Velocity:** {p['velocity_closed']} tickets/hour
- **Score:** {p['adjusted_score']}/100
""",
            "structured_data": {
                "technician_name": tech_name,
                "date": date_str,
                "meeting_count": m['meeting_count'],
                "meeting_hours": m['total_hours'],
                "meeting_percentage": p['meeting_percentage'],
                "available_hours": p['available_hours'],
                "tickets_closed_today": t['closed_today'],
                "tickets_updated_today": t['updated_today'],
                "active_tickets": t['active_tickets'],
                "velocity_closed": p['velocity_closed'],
                "adjusted_score": p['adjusted_score']
            }
        }
        url = f"{DUST_API_BASE}/{document['document_id']}"
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code in [200, 201]:
                uploaded += 1
            else:
                failed += 1
                logger.debug(f"Dust upload failed for {tech_name}: {response.status_code}")
        except Exception as e:
            failed += 1
            logger.debug(f"Dust upload error for {tech_name}: {e}")
    logger.info(f"OK Uploaded: {uploaded}/{len(tech_data)} (failed: {failed})")

def main():
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # DIAGNOSTIC MODE
    if args.diagnose:
        print("="*70, flush=True)
        print("DIAGNOSTIC MODE", flush=True)
        print("="*70, flush=True)
        print(flush=True)
        
        sdp_token = get_sdp_token()
        
        if args.date:
            target_date = datetime.strptime(args.date, '%Y-%m-%d')
        else:
            target_date = datetime.now() - timedelta(days=1)
        
        tech_name = input("Enter technician name (e.g., 'Aaron Musgrave'): ").strip()
        if not tech_name:
            print("No technician name provided", flush=True)
            return
        
        diagnose_ticket_dates(tech_name, sdp_token, target_date)
        return
    
    # NORMAL MODE
    print("="*70, flush=True)
    print("TECHNICIAN PERFORMANCE TRACKER v14.3 - ULTRA-OPTIMIZED!", flush=True)
    print(f"   {PARALLEL_TECHNICIANS} parallel technicians | Date range filter", flush=True)
    print("   Expected: 30-60 sec per date | 10-50x faster!", flush=True)
    print("="*70, flush=True)
    print(flush=True)
    
    graph_token = get_graph_token()
    sdp_token = get_sdp_token()
    
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d')
    else:
        target_date = datetime.now() - timedelta(days=1)
    
    logger.info(f"Processing: {target_date.strftime('%Y-%m-%d')}")
    
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        logger.error("No technicians found")
        return
    
    logger.info("Resolving emails...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            info['email'] = resolve_technician_email(tech_name, info, graph_token)
    
    tech_data = {}
    logger.info(f"Analyzing {len(technicians_info)} technicians with parallel processing...")
    
    # Parallel technician processing
    with ThreadPoolExecutor(max_workers=PARALLEL_TECHNICIANS) as executor:
        future_to_tech = {
            executor.submit(process_single_technician, tech_name, tech_info, graph_token, sdp_token, target_date): tech_name
            for tech_name, tech_info in technicians_info.items()
        }
        
        for future in as_completed(future_to_tech):
            result = future.result()
            tech_data[result['name']] = {
                'meetings': result['meetings'],
                'tickets': result['tickets'],
                'performance': result['performance']
            }
    
    print("\n" + "="*70, flush=True)
    print(f"{target_date.strftime('%B %d, %Y')}", flush=True)
    print("="*70, flush=True)
    
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[1]['performance']['adjusted_score'], reverse=True):
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        print(f"{tech_name:25} | {t['closed_today']:2} closed | {m['meeting_count']:2} mtgs ({m['total_hours']:4.1f}h) | Score: {p['adjusted_score']:5.1f}", flush=True)
    
    save_metrics_to_file(tech_data, target_date)
    upload_to_dust(tech_data, target_date)
    
    logger.info("Complete!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()

SCRIPT_PATH = 'scripts/track_meeting_load.py'
PROGRESS_FILE = 'backfill_progress.json'

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'completed_dates': [], 'failed_dates': [], 'last_processed': None, 'in_progress': []}

def save_progress(progress):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def format_duration(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

def process_single_date(date_str):
    """Process a single date with environment passed"""
    start_time = time.time()
    try:
        env = os.environ.copy()
        
        result = subprocess.run(
            ['python', SCRIPT_PATH, '--date', date_str],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_PER_DAY,
            env=env,
            cwd=os.getcwd()
        )
        
        duration = time.time() - start_time
        success = result.returncode == 0
        
        filename = f"tech_performance_{date_str.replace('-', '')}.json"
        file_size = 0
        if os.path.exists(filename):
            file_size = os.path.getsize(filename) / 1024
        
        return {
            'date': date_str,
            'success': success,
            'returncode': result.returncode,
            'stderr': result.stderr[-300:] if result.stderr else '',
            'filename': filename if success else None,
            'file_size': file_size if success else 0,
            'duration': duration
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return {
            'date': date_str,
            'success': False,
            'returncode': -1,
            'stderr': 'Timeout after 10 minutes',
            'filename': None,
            'file_size': 0,
            'duration': duration
        }
    except Exception as e:
        duration = time.time() - start_time
        return {
            'date': date_str,
            'success': False,
            'returncode': -2,
            'stderr': str(e)[:200],
            'filename': None,
            'file_size': 0,
            'duration': duration
        }

def main():
    # Disable output buffering
    sys.stdout.reconfigure(line_buffering=True)
    
    print("="*70, flush=True)
    print(f"PERFORMANCE DATA BACKFILL - LAST {DAYS_TO_BACKFILL} DAYS", flush=True)
    print(f"   {PARALLEL_WORKERS} WORKERS | v14.2 tracker | Fast & Accurate!", flush=True)
    print("="*70, flush=True)
    print(flush=True)
    
    if not os.path.exists(SCRIPT_PATH):
        print(f"Script not found: {SCRIPT_PATH}", flush=True)
        sys.exit(1)
    
    required_vars = [
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET',
        'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN',
        'DUST_API_KEY', 'DUST_WORKSPACE_ID'
    ]
    
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", flush=True)
        sys.exit(1)
    
    print("All environment variables found", flush=True)
    print(flush=True)
    
    progress = load_progress()
    
    # BUILD DATE RANGE BASED ON DAYS_TO_BACKFILL
    all_dates = []
    for days_ago in range(DAYS_TO_BACKFILL, 0, -1):
        date = datetime.now() - timedelta(days=days_ago)
        all_dates.append(date.strftime('%Y-%m-%d'))
    
    dates_to_process = [
        d for d in all_dates 
        if d not in progress['completed_dates'] 
        and d not in progress.get('in_progress', [])
    ]
    
    if len(dates_to_process) == 0:
        print("All dates already processed!", flush=True)
        print(f"   Completed: {len(progress['completed_dates'])} days", flush=True)
        if progress['failed_dates']:
            print(f"   Failed: {len(progress['failed_dates'])} days", flush=True)
            print(f"\nFailed dates:", flush=True)
            for date in progress['failed_dates'][:10]:
                print(f"      {date}", flush=True)
        return
    
    print(f"Date Range: {all_dates[0]} to {all_dates[-1]}", flush=True)
    print(f"Total: {len(all_dates)} | Completed: {len(progress['completed_dates'])} | Remaining: {len(dates_to_process)}", flush=True)
    print(flush=True)
    
    # Estimate with v14.2 performance (~4 min per date with 4 workers)
    estimated_minutes = (len(dates_to_process) * 4) / PARALLEL_WORKERS
    print(f"Estimated: ~{estimated_minutes:.0f} minutes ({estimated_minutes/60:.1f} hours)", flush=True)
    print(f"   (Based on v14.2 with {PARALLEL_WORKERS} workers + 500 rows + completed_time sort)", flush=True)
    if DELAY_BETWEEN_RUNS > 0:
        print(f"   Delay between submissions: {DELAY_BETWEEN_RUNS} second(s)", flush=True)
    print(flush=True)
    
    if sys.stdin.isatty() and len(dates_to_process) > 10:
        try:
            response = input("Continue? (yes/no): ")
            if response.lower() not in ['yes', 'y']:
                print("Cancelled.", flush=True)
                return
            print(flush=True)
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", flush=True)
            return
    elif len(dates_to_process) > 10:
        print("Automated mode - proceeding...", flush=True)
        print(flush=True)
    
    start_time = time.time()
    successful = 0
    failed = 0
    completed_count = 0
    total_file_size = 0
    
    print(f"{'='*70}", flush=True)
    print(f"PROCESSING {len(dates_to_process)} DAYS WITH {PARALLEL_WORKERS} WORKERS", flush=True)
    print('='*70, flush=True)
    print(flush=True)
    
    # Show which dates are being submitted
    print(f"Submitting first {min(PARALLEL_WORKERS, len(dates_to_process))} dates to workers...", flush=True)
    for i, date in enumerate(dates_to_process[:PARALLEL_WORKERS]):
        print(f"   Worker {i+1}: {date}", flush=True)
    print(flush=True)
    
    try:
        with ProcessPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            future_to_date = {}
            
            # Submit dates with optional delay
            for date_str in dates_to_process:
                future_to_date[executor.submit(process_single_date, date_str)] = date_str
                if DELAY_BETWEEN_RUNS > 0 and len(future_to_date) < len(dates_to_process):
                    time.sleep(DELAY_BETWEEN_RUNS)
            
            progress['in_progress'] = dates_to_process.copy()
            save_progress(progress)
            
            print("Workers running... Results will appear as dates complete", flush=True)
            print(flush=True)
            
            for future in as_completed(future_to_date):
                completed_count += 1
                result = future.result()
                
                elapsed = time.time() - start_time
                remaining = len(dates_to_process) - completed_count
                avg_time_actual = elapsed / completed_count if completed_count > 0 else 0
                eta = remaining * avg_time_actual / PARALLEL_WORKERS
                progress_pct = (completed_count / len(dates_to_process)) * 100
                
                if result['success']:
                    successful += 1
                    total_file_size += result['file_size']
                    progress['completed_dates'].append(result['date'])
                    progress['last_processed'] = result['date']
                    
                    if result['file_size'] > 3:
                        status_icon = "OK"
                        status = f"({result['file_size']:5.1f} KB)"
                    else:
                        status_icon = "WARN"
                        status = "(small)"
                    
                    # REAL-TIME OUTPUT with flush=True
                    print(f"{status_icon} [{completed_count:3d}/{len(dates_to_process)}] {result['date']} {status} in {format_duration(result['duration'])} | Progress: {progress_pct:5.1f}% | ETA: {format_duration(eta)}", flush=True)
                else:
                    failed += 1
                    progress['failed_dates'].append(result['date'])
                    error_msg = result['stderr'][:50] if result['stderr'] else 'Unknown'
                    print(f"FAIL [{completed_count:3d}/{len(dates_to_process)}] {result['date']} FAILED: {error_msg}", flush=True)
                
                if result['date'] in progress.get('in_progress', []):
                    progress['in_progress'].remove(result['date'])
                
                # Save progress every 5 completions
                if completed_count % 5 == 0:
                    save_progress(progress)
                    print(f"Progress saved ({completed_count} completed)", flush=True)
            
            progress['in_progress'] = []
            save_progress(progress)
    
    except KeyboardInterrupt:
        print("\n\nINTERRUPTED", flush=True)
        save_progress(progress)
        sys.exit(0)
    
    total_time = time.time() - start_time
    print(f"\n{'='*70}", flush=True)
    print("BACKFILL COMPLETE", flush=True)
    print('='*70, flush=True)
    print(f"\nSummary:", flush=True)
    print(f"   Total time: {format_duration(total_time)}", flush=True)
    print(f"   Successful: {successful}/{len(dates_to_process)}", flush=True)
    print(f"   Failed: {failed}/{len(dates_to_process)}", flush=True)
    
    if successful + failed > 0:
        success_rate = successful/(successful+failed)*100
        print(f"   Success rate: {success_rate:.1f}%", flush=True)
    
    if successful > 0:
        print(f"   Total data: {total_file_size/1024:.1f} MB", flush=True)
        avg_size = total_file_size / successful if successful > 0 else 0
        print(f"   Avg file size: {avg_size:.1f} KB", flush=True)
        avg_time = total_time / successful if successful > 0 else 0
        print(f"   Avg time/date: {format_duration(avg_time)}", flush=True)
    
    if progress['failed_dates']:
        print(f"\nFailed: {len(progress['failed_dates'])}", flush=True)
        for i, date in enumerate(progress['failed_dates'][:5], 1):
            print(f"   {i}. {date}", flush=True)
        if len(progress['failed_dates']) > 5:
            print(f"   ... and {len(progress['failed_dates']) - 5} more", flush=True)
    
    if failed == 0 and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print(f"\nProgress file removed", flush=True)
    else:
        print(f"\nProgress saved to: {PROGRESS_FILE}", flush=True)
        print(f"   Run again to retry failed dates", flush=True)
    
    print(flush=True)
    print("="*70, flush=True)
    print(f"Configuration:", flush=True)
    print(f"   Days to backfill: {DAYS_TO_BACKFILL}", flush=True)
    print(f"   Parallel workers: {PARALLEL_WORKERS}", flush=True)
    print(f"   Delay between runs: {DELAY_BETWEEN_RUNS}s", flush=True)
    print(f"   To change: Edit lines 18-21 or use workflow inputs", flush=True)
    print("="*70, flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
