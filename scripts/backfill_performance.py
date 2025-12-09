#!/usr/bin/env python3
"""
Performance Data Backfill Script v15.2.2
Uses the same working logic as track_meeting_load.py
Processes historical dates sequentially
Azure AD profile integration (job title, department, location)
Optimized: Smart pagination + parallel processing
Fixed: Proper None handling for Azure profile fields
"""

import os
import sys
import requests
import json
import logging
import time
import argparse
import re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
WORK_HOURS_PER_DAY = 8
PARALLEL_TECHNICIANS = 8
PARALLEL_TICKET_FETCH = 10
EMAIL_DOMAIN = 'motorad.com'
PROGRESS_FILE = 'backfill_progress.json'

# Token cache (to avoid rate limiting)
TOKEN_CACHE = {
    'graph_token': None,
    'graph_expires': None,
    'sdp_token': None,
    'sdp_expires': None
}

# Credential cleaning
def clean_credential(value):
    if not value:
        return value
    value = value.strip().strip('"').strip("'")
    value = value.replace('\\', '')
    return value.strip()

def clean_date_string(date_str):
    """Clean and validate date string"""
    if not date_str:
        return date_str
    
    # Remove backslashes
    date_str = date_str.replace('\\', '').strip()
    
    # Parse and reformat to ensure proper format
    try:
        # Try parsing with single digit month/day
        parts = date_str.split('-')
        if len(parts) == 3:
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            dt = datetime(year, month, day)
            return dt.strftime('%Y-%m-%d')
    except:
        pass
    
    return date_str

# Load credentials
TENANT_ID = clean_credential(os.environ.get('AZURE_TENANT_ID'))
CLIENT_ID = clean_credential(os.environ.get('AZURE_CLIENT_ID'))
CLIENT_SECRET = clean_credential(os.environ.get('AZURE_CLIENT_SECRET'))

SDP_DOMAIN = clean_credential(os.environ.get('SDP_DOMAIN'))
SDP_CLIENT_ID = clean_credential(os.environ.get('SDP_CLIENT_ID'))
SDP_CLIENT_SECRET = clean_credential(os.environ.get('SDP_CLIENT_SECRET'))
SDP_REFRESH_TOKEN = clean_credential(os.environ.get('SDP_REFRESH_TOKEN'))

DUST_API_KEY = clean_credential(os.environ.get('DUST_API_KEY'))
DUST_WORKSPACE_ID = clean_credential(os.environ.get('DUST_WORKSPACE_ID'))

SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

def get_graph_token():
    """Get Microsoft Graph token with caching and rate limit handling"""
    if TOKEN_CACHE['graph_token'] and TOKEN_CACHE['graph_expires']:
        if datetime.now() < TOKEN_CACHE['graph_expires']:
            return TOKEN_CACHE['graph_token']
    
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=data, timeout=30)
            if response.status_code == 200:
                token = response.json()['access_token']
                TOKEN_CACHE['graph_token'] = token
                TOKEN_CACHE['graph_expires'] = datetime.now() + timedelta(minutes=45)
                return token
            elif response.status_code == 429:
                wait_time = 2 ** attempt * 30
                logger.warning(f"Rate limited by Graph API. Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                raise Exception(f"Graph token failed: {response.text[:200]}")
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt * 10
                logger.warning(f"Graph token request error. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            raise Exception(f"Failed to get Graph token: {e}")
    
    raise Exception("Failed to get Graph token after max retries")

def get_sdp_token():
    """Get ServiceDesk token with caching and rate limit handling"""
    if TOKEN_CACHE['sdp_token'] and TOKEN_CACHE['sdp_expires']:
        if datetime.now() < TOKEN_CACHE['sdp_expires']:
            return TOKEN_CACHE['sdp_token']
    
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    
    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
            if response.status_code == 200:
                token = response.json()['access_token']
                TOKEN_CACHE['sdp_token'] = token
                TOKEN_CACHE['sdp_expires'] = datetime.now() + timedelta(minutes=45)
                return token
            elif response.status_code == 429 or 'too many requests' in response.text.lower():
                wait_time = 2 ** attempt * 30
                logger.warning(f"Rate limited by ServiceDesk API. Waiting {wait_time}s... (attempt {attempt+1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                error_text = response.text[:300]
                if 'too many requests' in error_text.lower():
                    wait_time = 2 ** attempt * 30
                    logger.warning(f"Rate limited. Waiting {wait_time}s... (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                raise Exception(f"SDP token failed: {error_text}")
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt * 10
                logger.warning(f"SDP token request error. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            raise Exception(f"Failed to get SDP token: {e}")
    
    raise Exception("Failed to get SDP token after max retries. Please wait 10 minutes and try again.")

def get_azure_profile(email, graph_token):
    """Fetch user profile from Azure AD including job title"""
    if not email:
        return None
    
    url = f"https://graph.microsoft.com/v1.0/users/{email}"
    headers = {
        'Authorization': f'Bearer {graph_token}',
        'ConsistencyLevel': 'eventual'
    }
    params = {
        '\$select': 'displayName,jobTitle,department,officeLocation,mail,mobilePhone,businessPhones'
    }
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 200:
            profile = response.json()
            return {
                'job_title': profile.get('jobTitle'),
                'department': profile.get('department'),
                'office_location': profile.get('officeLocation'),
                'mobile_phone': profile.get('mobilePhone'),
                'business_phones': profile.get('businessPhones', [])
            }
    except Exception as e:
        logger.warning(f"Error fetching Azure profile for {email}: {e}")
    
    return None

def discover_technicians(sdp_token, graph_token):
    """Find active technicians and enrich with Azure AD profiles"""
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
    url = f"{SDP_API_BASE}/requests"
    params = {'input_data': json.dumps(list_info)}
    
    response = requests.get(url, headers=headers, params=params, timeout=30)
    if response.status_code == 200:
        tickets = response.json().get('requests', [])
        for ticket in tickets:
            tech = ticket.get('technician')
            if tech and isinstance(tech, dict):
                name = tech.get('name')
                email = tech.get('email_id')
                
                if name and name != 'Unassigned' and name not in technicians:
                    # Fetch Azure AD profile for job title
                    azure_profile = get_azure_profile(email, graph_token)
                    
                    technicians[name] = {
                        'email': email,
                        'job_title': azure_profile.get('job_title') if azure_profile else None,
                        'department': azure_profile.get('department') if azure_profile else None,
                        'office_location': azure_profile.get('office_location') if azure_profile else None
                    }
    
    return technicians

def fix_datetime_str(dt_str):
    """Fix Microsoft Graph datetime strings"""
    dt_str = dt_str.replace('Z', '+00:00')
    return re.sub(r'\.(\d{6})\d+', r'.\1', dt_str)

def get_meetings(email, graph_token, date):
    """Get meetings for technician on specific date"""
    if not email:
        return {'count': 0, 'hours': 0}
    
    start_time = date.replace(hour=8, minute=0, second=0, microsecond=0)
    end_time = date.replace(hour=17, minute=0, second=0, microsecond=0)
    
    url = f"https://graph.microsoft.com/v1.0/users/{email}/calendar/calendarView"
    headers = {
        'Authorization': f'Bearer {graph_token}',
        'Prefer': 'outlook.timezone="Central Standard Time"'
    }
    params = {
        'startDateTime': start_time.isoformat(),
        'endDateTime': end_time.isoformat(),
        '\$select': 'isAllDay,isCancelled,responseStatus,start,end'
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
                    start = datetime.fromisoformat(fix_datetime_str(event['start']['dateTime']))
                    end = datetime.fromisoformat(fix_datetime_str(event['end']['dateTime']))
                    
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
            return {'count': count, 'hours': round(total_minutes / 60, 2)}
    except Exception as e:
        logger.warning(f"Error fetching meetings for {email}: {e}")
        return {'count': 0, 'hours': 0}

def safe_timestamp_to_date(value):
    """Convert ServiceDesk timestamp to datetime (CST)"""
    if not value:
        return None
    try:
        if isinstance(value, dict):
            value = value.get('value')
        if isinstance(value, str):
            value = int(value)
        
        # Convert milliseconds to datetime (UTC)
        dt_utc = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        
        # Convert to CST
        central_offset = timedelta(hours=-6)
        dt_local = dt_utc + central_offset
        
        return dt_local
    except:
        return None

def get_ticket_details(ticket_id, sdp_token):
    """Fetch individual ticket details"""
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

def verify_ticket_belongs_to_tech(ticket, tech_name):
    """Verify ticket belongs to this technician"""
    tech = ticket.get('technician')
    if not tech or not isinstance(tech, dict):
        return False
    return tech.get('name', '') == tech_name

def count_closed_tickets(tech_name, sdp_token, target_date):
    """Count tickets closed by technician on specific date"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Fetch closed tickets with SMART PAGINATION
    all_tickets = []
    start_index = 1
    page_size = 100
    max_pages = 50  # Safety limit
    
    # Buffer zone: fetch 2 days before target to be safe
    date_buffer = target_date - timedelta(days=2)
    
    for page_num in range(1, max_pages + 1):
        list_info = {
            "list_info": {
                "row_count": page_size,
                "start_index": start_index,
                "search_criteria": [
                    {"field": "technician.name", "condition": "is", "value": tech_name},
                    {"field": "status.name", "condition": "is", "values": ["Closed"]}
                ],
                "sort_field": "resolved_time",
                "sort_order": "desc"
            }
        }
        
        try:
            params = {'input_data': json.dumps(list_info)}
            url = f"{SDP_API_BASE}/requests"
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code != 200:
                break
            
            tickets = response.json().get('requests', [])
            
            if not tickets:
                break
            
            # Validate tickets belong to this technician
            validated = [t for t in tickets if verify_ticket_belongs_to_tech(t, tech_name)]
            all_tickets.extend(validated)
            
            # SMART STOP: Check if we've gone back far enough
            oldest_in_page = None
            for ticket in validated:
                if 'resolved_time' in ticket:
                    resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
                    if resolved_time:
                        if oldest_in_page is None or resolved_time < oldest_in_page:
                            oldest_in_page = resolved_time
            
            # Stop if oldest ticket is before our buffer zone
            if oldest_in_page and oldest_in_page.date() < date_buffer:
                break
            
            # If we got less than page_size, we're at the end
            if len(tickets) < page_size:
                break
            
            # Move to next page
            start_index += page_size
            
        except:
            break
    
    if not all_tickets:
        return 0
    
    # Check if resolved_time is in the response
    if all_tickets and 'resolved_time' in all_tickets[0]:
        # FAST PATH: resolved_time in list - filter by date in Python
        count = 0
        
        for ticket in all_tickets:
            resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
            
            if resolved_time and resolved_time.date() == target_date:
                count += 1
        
        return count
    else:
        # SLOW PATH: resolved_time NOT in list - fetch each ticket individually
        def check_ticket(ticket_id):
            details = get_ticket_details(ticket_id, sdp_token)
            if not details:
                return False
            if not verify_ticket_belongs_to_tech(details, tech_name):
                return False
            status = details.get('status')
            if not status or not isinstance(status, dict):
                return False
            if status.get('name', '') != 'Closed':
                return False
            
            resolved_time = safe_timestamp_to_date(details.get('resolved_time'))
            
            if resolved_time and resolved_time.date() == target_date:
                return True
            return False
        
        ticket_ids = [t['id'] for t in all_tickets]
        count = 0
        
        with ThreadPoolExecutor(max_workers=PARALLEL_TICKET_FETCH) as executor:
            futures = {executor.submit(check_ticket, tid): tid for tid in ticket_ids}
            for future in as_completed(futures):
                if future.result():
                    count += 1
        
        return count

def get_active_tickets(tech_name, sdp_token):
    """Get count of active tickets"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
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
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            tickets = response.json().get('requests', [])
            validated = [t for t in tickets if verify_ticket_belongs_to_tech(t, tech_name)]
            return len(validated)
    except:
        pass
    
    return 0

def calculate_performance(meetings, tickets_closed, active_tickets):
    """Calculate performance metrics"""
    meeting_hours = meetings['hours']
    available_hours = max(0.5, WORK_HOURS_PER_DAY - meeting_hours)
    velocity = tickets_closed / available_hours if available_hours > 0 else 0
    score = min(40, velocity * 10)
    
    if 5 <= active_tickets <= 15:
        score += 15
    elif active_tickets < 5:
        score += active_tickets * 3
    else:
        score += max(0, 15 - (active_tickets - 15) * 0.5)
    
    score = max(0, min(100, score))
    
    return {
        'available_hours': round(available_hours, 2),
        'velocity': round(velocity, 2),
        'score': round(score, 1),
        'meeting_percentage': round((meeting_hours / WORK_HOURS_PER_DAY) * 100, 1)
    }

def process_technician(tech_name, tech_info, graph_token, sdp_token, target_date):
    """Process single technician's data"""
    # Handle both dict and string formats
    if isinstance(tech_info, dict):
        tech_email = tech_info['email']
        tech_job_title = tech_info.get('job_title')
        tech_department = tech_info.get('department')
        tech_office = tech_info.get('office_location')
    else:
        tech_email = tech_info
        tech_job_title = None
        tech_department = None
        tech_office = None
    
    meetings = get_meetings(tech_email, graph_token, target_date)
    closed = count_closed_tickets(tech_name, sdp_token, target_date.date())
    active = get_active_tickets(tech_name, sdp_token)
    performance = calculate_performance(meetings, closed, active)
    
    return {
        'name': tech_name,
        'email': tech_email,
        'job_title': tech_job_title,
        'department': tech_department,
        'office_location': tech_office,
        'meetings': meetings,
        'tickets': {
            'closed_today': closed,
            'active_tickets': active
        },
        'performance': performance
    }

def save_to_file(tech_data, date):
    """Save metrics to JSON file"""
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    
    output = {}
    for tech_name, data in tech_data.items():
        output[tech_name] = {
            'date': date.isoformat(),
            'profile': {
                'email': data.get('email'),
                'job_title': data.get('job_title'),
                'department': data.get('department'),
                'office_location': data.get('office_location')
            },
            'meetings': {
                'count': data['meetings']['count'],
                'hours': data['meetings']['hours']
            },
            'tickets': data['tickets'],
            'performance': data['performance']
        }
    
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    return filename

def upload_to_dust(tech_data, date):
    """Upload metrics to Dust"""
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        return 0
    
    base_url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    date_str = date.strftime('%Y-%m-%d')
    uploaded = 0
    
    for tech_name, data in tech_data.items():
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        
        # Build tags - safely handle None values
        tags = [
            "performance-metrics",
            f"technician:{tech_name.lower().replace(' ', '-')}",
            f"date:{date_str}"
        ]
        
        if data.get('job_title'):
            tags.append(f"job-title:{data.get('job_title').lower().replace(' ', '-')}")
        
        if data.get('department'):
            tags.append(f"department:{data.get('department').lower().replace(' ', '-')}")
        
        document = {
            "document_id": f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp": int(date.timestamp() * 1000),
            "tags": tags,
            "text": f"""# {tech_name} - {date.strftime('%B %d, %Y')}

## Profile
- **Name:** {tech_name}
- **Job Title:** {data.get('job_title') or 'N/A'}
- **Department:** {data.get('department') or 'N/A'}
- **Location:** {data.get('office_location') or 'N/A'}
- **Email:** {data.get('email') or 'N/A'}

## Calendar & Availability
- **Meetings:** {m['count']} meetings
- **Meeting Time:** {m['hours']} hours ({p['meeting_percentage']}%)
- **Available:** {p['available_hours']} hours

## Ticket Activity
- **Closed:** {t['closed_today']}
- **Active:** {t['active_tickets']}

## Performance
- **Velocity:** {p['velocity']} tickets/hour
- **Score:** {p['score']}/100
""",
            "structured_data": {
                "technician_name": tech_name,
                "job_title": data.get('job_title'),
                "department": data.get('department'),
                "office_location": data.get('office_location'),
                "email": data.get('email'),
                "date": date_str,
                "meeting_count": m['count'],
                "meeting_hours": m['hours'],
                "meeting_percentage": p['meeting_percentage'],
                "available_hours": p['available_hours'],
                "tickets_closed_today": t['closed_today'],
                "active_tickets": t['active_tickets'],
                "velocity": p['velocity'],
                "score": p['score']
            }
        }
        
        url = f"{base_url}/{document['document_id']}"
        
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code in [200, 201]:
                uploaded += 1
        except:
            pass
    
    return uploaded

def load_progress():
    """Load progress from file"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'completed': [], 'failed': []}

def save_progress(progress):
    """Save progress to file"""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def process_single_date(target_date, technicians):
    """Process all technicians for a single date"""
    logger.info(f"Processing: {target_date.strftime('%Y-%m-%d')}")
    
    try:
        graph_token = get_graph_token()
        sdp_token = get_sdp_token()
    except Exception as e:
        logger.error(f"Failed to get tokens for {target_date.strftime('%Y-%m-%d')}: {e}")
        raise
    
    tech_data = {}
    
    # PARALLEL PROCESSING: Process technicians simultaneously
    tech_list = list(technicians.items())
    
    with ThreadPoolExecutor(max_workers=PARALLEL_TECHNICIANS) as executor:
        # Submit all technicians for parallel processing
        futures = {}
        for tech_name, tech_info in tech_list:
            future = executor.submit(
                process_technician,
                tech_name,
                tech_info,
                graph_token,
                sdp_token,
                target_date
            )
            futures[future] = tech_name
        
        # Collect results as they complete
        for future in as_completed(futures):
            tech_name = futures[future]
            try:
                result = future.result()
                tech_data[result['name']] = {
                    'email': result['email'],
                    'job_title': result['job_title'],
                    'department': result['department'],
                    'office_location': result['office_location'],
                    'meetings': result['meetings'],
                    'tickets': result['tickets'],
                    'performance': result['performance']
                }
            except Exception as e:
                logger.error(f"Failed to process {tech_name}: {e}")
    
    # Display results
    print()
    print("="*70)
    print(f"{target_date.strftime('%B %d, %Y')}")
    print("="*70)
    
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[1]['performance']['score'], reverse=True):
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        # Safely truncate job title - handle None
        job_title = (data.get('job_title') or 'N/A')[:20]
        print(f"{tech_name:25} | {job_title:20} | {t['closed_today']:2} closed | {m['count']:2} mtgs | Score: {p['score']:5.1f}")
    
    print()
    
    filename = save_to_file(tech_data, target_date)
    uploaded = upload_to_dust(tech_data, target_date)
    
    logger.info(f"✓ Saved: {filename}, Uploaded to Dust: {uploaded}/{len(tech_data)}")
    
    return filename

def main():
    """Main backfill execution"""
    parser = argparse.ArgumentParser(description='Backfill performance data for date range')
    parser.add_argument('--days', type=int, help='Number of days to backfill')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (YYYY-MM-DD)')
    args = parser.parse_args()
    
    print("="*70)
    print("PERFORMANCE DATA BACKFILL v15.2.2 (Azure AD Profiles)")
    print("="*70)
    print()
    
    if not args.days and not args.start_date:
        print("Choose backfill mode:")
        print("  1. Backfill last N days")
        print("  2. Backfill specific date range")
        print()
        
        choice = input("Enter choice (1 or 2): ").strip()
        
        if choice == "1":
            days_input = input("How many days to backfill? (default: 30): ").strip()
            args.days = int(days_input) if days_input else 30
        elif choice == "2":
            args.start_date = input("Start date (YYYY-MM-DD): ").strip()
            args.end_date = input("End date (YYYY-MM-DD): ").strip()
        else:
            print("Invalid choice. Exiting.")
            sys.exit(1)
        
        print()
    
    try:
        logger.info("Getting initial tokens...")
        graph_token = get_graph_token()
        sdp_token = get_sdp_token()
        logger.info("✓ Tokens obtained")
    except Exception as e:
        logger.error(f"Failed to get tokens: {e}")
        logger.error("If rate limited, please wait 10 minutes and try again.")
        sys.exit(1)
    
    try:
        logger.info("Discovering technicians and fetching Azure profiles...")
        technicians = discover_technicians(sdp_token, graph_token)
        logger.info(f"✓ Found {len(technicians)} technicians")
        print()
    except Exception as e:
        logger.error(f"Failed to discover technicians: {e}")
        sys.exit(1)
    
    if not technicians:
        logger.error("No technicians found")
        sys.exit(1)
    
    if args.start_date:
        args.start_date = clean_date_string(args.start_date)
        args.end_date = clean_date_string(args.end_date)
        
        logger.info(f"Date range: {args.start_date} to {args.end_date}")
        
        start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
        if args.end_date:
            end_date = datetime.strptime(args.end_date, '%Y-%m-%d')
        else:
            logger.error("--end-date required when using --start-date")
            sys.exit(1)
        
        dates = []
        current = start_date
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=1)
    else:
        days = args.days if args.days else 30
        dates = [(datetime.now() - timedelta(days=i)) for i in range(1, days + 1)]
        dates.reverse()
    
    progress = load_progress()
    completed_dates = set(progress['completed'])
    dates_to_process = [d for d in dates if d.strftime('%Y-%m-%d') not in completed_dates]
    
    if not dates_to_process:
        logger.info("All dates already processed!")
        return
    
    logger.info(f"Processing {len(dates_to_process)} dates")
    if completed_dates:
        logger.info(f"(Skipping {len(completed_dates)} already completed dates)")
    
    if len(dates_to_process) > 5:
        if sys.stdin.isatty():
            confirm = input(f"\nProcess {len(dates_to_process)} dates? (yes/no): ").strip().lower()
            if confirm not in ['yes', 'y']:
                print("Cancelled.")
                return
        else:
            logger.info(f"Automated mode: Proceeding with {len(dates_to_process)} dates")
    
    print()
    
    start_time = time.time()
    
    for i, date in enumerate(dates_to_process, 1):
        try:
            process_single_date(date, technicians)
            progress['completed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
            
            elapsed = time.time() - start_time
            avg_time = elapsed / i
            remaining = len(dates_to_process) - i
            eta = remaining * avg_time
            
            print(f"Progress: [{i}/{len(dates_to_process)}] ({i/len(dates_to_process)*100:.0f}%) | Elapsed: {elapsed/60:.1f} min | ETA: {eta/60:.1f} min")
            print()
            
            if i < len(dates_to_process):
                time.sleep(3)
            
        except Exception as e:
            logger.error(f"Failed to process {date.strftime('%Y-%m-%d')}: {e}")
            progress['failed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
    
    total_time = time.time() - start_time
    print()
    print("="*70)
    print("BACKFILL COMPLETE")
    print("="*70)
    print(f"Total time: {total_time/60:.1f} minutes")
    print(f"Processed: {len(dates_to_process)} dates")
    if progress['failed']:
        print(f"Failed: {len(progress['failed'])} dates: {', '.join(progress['failed'])}")
    else:
        print("Failed: 0 dates")
    print()
    
    if not progress['failed']:
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
            logger.info("✓ Progress file removed")
    else:
        logger.info(f"✓ Progress saved to {PROGRESS_FILE}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted - progress saved")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
