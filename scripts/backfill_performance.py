#!/usr/bin/env python3
"""
Performance Data Backfill Script v17.1.0 - MEETINGS & PROFILE (Enhanced Azure Fields)
Simplified: Only tracks meeting load and enhanced Azure AD profile
Ticket data handled by ServiceDesk Intelligence script
Enhanced: Manager, location details, account status, tenure
Processes historical dates sequentially
Optimized: Fast parallel processing of meetings
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
    
    raise Exception("Failed to get SDP token after max retries.")

def get_azure_profile(email, graph_token):
    """
    Fetch enhanced user profile from Azure AD
    v17.1.0: Added manager, city/state/country, display name, company, account status, tenure
    """
    if not email:
        return None
    
    url = f"https://graph.microsoft.com/v1.0/users/{email}"
    headers = {
        'Authorization': f'Bearer {graph_token}',
        'ConsistencyLevel': 'eventual'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            profile = response.json()
            
            # Fetch manager separately (requires additional API call)
            manager_name = None
            manager_email = None
            try:
                manager_url = f"https://graph.microsoft.com/v1.0/users/{email}/manager"
                manager_response = requests.get(manager_url, headers=headers, timeout=30)
                if manager_response.status_code == 200:
                    manager_data = manager_response.json()
                    manager_name = manager_data.get('displayName')
                    manager_email = manager_data.get('mail')
            except:
                # Manager not found or not set - this is OK
                pass
            
            return {
                # Core fields
                'job_title': profile.get('jobTitle'),
                'department': profile.get('department'),
                'office_location': profile.get('officeLocation'),
                
                # Enhanced location (Phase 1)
                'city': profile.get('city'),
                'state': profile.get('state'),
                'country': profile.get('country'),
                
                # Organizational hierarchy (Phase 1)
                'manager_name': manager_name,
                'manager_email': manager_email,
                
                # Account status (Phase 1)
                'account_enabled': profile.get('accountEnabled'),
                
                # Identity (Phase 2)
                'display_name': profile.get('displayName'),
                'company_name': profile.get('companyName'),
                
                # Tenure (Phase 2)
                'account_created': profile.get('createdDateTime'),
                
                # Contact info
                'mobile_phone': profile.get('mobilePhone'),
                'business_phones': profile.get('businessPhones', [])
            }
    except Exception as e:
        logger.warning(f"Error fetching Azure profile for {email}: {e}")
    
    return None

def discover_technicians(sdp_token, graph_token):
    """Find active technicians and enrich with enhanced Azure AD profiles"""
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
                    azure_profile = get_azure_profile(email, graph_token)
                    
                    if azure_profile:
                        technicians[name] = {
                            'email': email,
                            'job_title': azure_profile.get('job_title'),
                            'department': azure_profile.get('department'),
                            'office_location': azure_profile.get('office_location'),
                            'city': azure_profile.get('city'),
                            'state': azure_profile.get('state'),
                            'country': azure_profile.get('country'),
                            'manager_name': azure_profile.get('manager_name'),
                            'manager_email': azure_profile.get('manager_email'),
                            'account_enabled': azure_profile.get('account_enabled'),
                            'display_name': azure_profile.get('display_name'),
                            'company_name': azure_profile.get('company_name'),
                            'account_created': azure_profile.get('account_created')
                        }
                    else:
                        # Fallback if Azure profile fetch fails
                        technicians[name] = {
                            'email': email,
                            'job_title': None,
                            'department': None,
                            'office_location': None,
                            'city': None,
                            'state': None,
                            'country': None,
                            'manager_name': None,
                            'manager_email': None,
                            'account_enabled': None,
                            'display_name': None,
                            'company_name': None,
                            'account_created': None
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
def calculate_performance(meetings, job_title=None):
    """
    Calculate performance metrics based on meeting load only
    v17.0.0: Meetings and availability only - tickets from intelligence script
    """
    meeting_hours = meetings['hours']
    available_hours = max(0.5, WORK_HOURS_PER_DAY - meeting_hours)
    
    # Role-based multipliers for reference
    role_multipliers = {
        'director': 20,
        'application manager': 20,
        'manager': 15,
        'senior': 12,
        'engineer': 10,
        'technician': 10,
        'specialist': 8
    }
    
    # Determine role multiplier from job title
    multiplier = 10  # Default
    if job_title:
        job_title_lower = job_title.lower()
        if 'director' in job_title_lower or 'application manager' in job_title_lower:
            multiplier = 20
        elif 'manager' in job_title_lower:
            multiplier = 15
        elif 'senior' in job_title_lower:
            multiplier = 12
        elif 'engineer' in job_title_lower or 'technician' in job_title_lower:
            multiplier = 10
        elif 'specialist' in job_title_lower:
            multiplier = 8
    
    return {
        'available_hours': round(available_hours, 2),
        'meeting_percentage': round((meeting_hours / WORK_HOURS_PER_DAY) * 100, 1),
        'role_multiplier': multiplier
    }


def process_technician(tech_name, tech_info, graph_token, sdp_token, target_date):
    """
    Process single technician's calendar and profile data only
    v17.1.0: Enhanced with additional Azure AD fields
    """
    if isinstance(tech_info, dict):
        tech_email = tech_info['email']
        tech_job_title = tech_info.get('job_title')
        tech_department = tech_info.get('department')
        tech_office = tech_info.get('office_location')
        tech_city = tech_info.get('city')
        tech_state = tech_info.get('state')
        tech_country = tech_info.get('country')
        tech_manager_name = tech_info.get('manager_name')
        tech_manager_email = tech_info.get('manager_email')
        tech_account_enabled = tech_info.get('account_enabled')
        tech_display_name = tech_info.get('display_name')
        tech_company_name = tech_info.get('company_name')
        tech_account_created = tech_info.get('account_created')
    else:
        tech_email = tech_info
        tech_job_title = None
        tech_department = None
        tech_office = None
        tech_city = None
        tech_state = None
        tech_country = None
        tech_manager_name = None
        tech_manager_email = None
        tech_account_enabled = None
        tech_display_name = None
        tech_company_name = None
        tech_account_created = None
    
    # Only fetch meetings - no ticket data
    meetings = get_meetings(tech_email, graph_token, target_date)
    
    # Simplified performance calculation (meetings only)
    performance = calculate_performance(meetings, tech_job_title)
    
    return {
        'name': tech_name,
        'email': tech_email,
        'job_title': tech_job_title,
        'department': tech_department,
        'office_location': tech_office,
        'city': tech_city,
        'state': tech_state,
        'country': tech_country,
        'manager_name': tech_manager_name,
        'manager_email': tech_manager_email,
        'account_enabled': tech_account_enabled,
        'display_name': tech_display_name,
        'company_name': tech_company_name,
        'account_created': tech_account_created,
        'meetings': meetings,
        'performance': performance
    }


def save_to_file(tech_data, date):
    """Save metrics to JSON file - v17.1.0 with enhanced Azure fields"""
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    
    output = {}
    for tech_name, data in tech_data.items():
        output[tech_name] = {
            'date': date.isoformat(),
            'profile': {
                'email': data.get('email'),
                'display_name': data.get('display_name'),
                'job_title': data.get('job_title'),
                'department': data.get('department'),
                'office_location': data.get('office_location'),
                'city': data.get('city'),
                'state': data.get('state'),
                'country': data.get('country'),
                'manager_name': data.get('manager_name'),
                'manager_email': data.get('manager_email'),
                'company_name': data.get('company_name'),
                'account_enabled': data.get('account_enabled'),
                'account_created': data.get('account_created')
            },
            'meetings': {
                'count': data['meetings']['count'],
                'hours': data['meetings']['hours']
            },
            'performance': data['performance']
        }
    
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    return filename


def upload_to_dust(tech_data, date):
    """Upload metrics to Dust - v17.1.0 with enhanced Azure fields"""
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
        p = data['performance']
        
        # Build location string
        location_parts = []
        if data.get('city'):
            location_parts.append(data.get('city'))
        if data.get('state'):
            location_parts.append(data.get('state'))
        if data.get('country'):
            location_parts.append(data.get('country'))
        location_string = ', '.join(location_parts) if location_parts else (data.get('office_location') or 'N/A')
        
        # Build tags - safely handle None values
        tags = [
            "performance-metrics",
            "meeting-load",
            f"technician:{tech_name.lower().replace(' ', '-')}",
            f"date:{date_str}"
        ]
        
        if data.get('job_title'):
            tags.append(f"job-title:{data.get('job_title').lower().replace(' ', '-')}")
        
        if data.get('department'):
            tags.append(f"department:{data.get('department').lower().replace(' ', '-')}")
        
        if data.get('manager_name'):
            tags.append(f"manager:{data.get('manager_name').lower().replace(' ', '-')}")
        
        if data.get('city'):
            tags.append(f"city:{data.get('city').lower().replace(' ', '-')}")
        
        if data.get('account_enabled') == False:
            tags.append("account-disabled")
        
        document = {
            "document_id": f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp": int(date.timestamp() * 1000),
            "tags": tags,
            "text": f"""# {tech_name} - {date.strftime('%B %d, %Y')}

## Profile
- **Name:** {data.get('display_name') or tech_name}
- **Job Title:** {data.get('job_title') or 'N/A'}
- **Department:** {data.get('department') or 'N/A'}
- **Location:** {location_string}
- **Company:** {data.get('company_name') or 'N/A'}
- **Email:** {data.get('email') or 'N/A'}

## Organizational Structure
- **Reports To:** {data.get('manager_name') or 'N/A'}
- **Manager Email:** {data.get('manager_email') or 'N/A'}

## Calendar & Availability
- **Meetings:** {m['count']} meetings
- **Meeting Time:** {m['hours']} hours ({p['meeting_percentage']}%)
- **Available Hours:** {p['available_hours']} hours

## Role Information
- **Role Multiplier:** {p['role_multiplier']}x

## Capacity Status
- **Meeting Load:** {p['meeting_percentage']}% of workday
- **Available for Tickets:** {p['available_hours']} hours

## Account Status
- **Account Enabled:** {'Yes' if data.get('account_enabled') else 'No' if data.get('account_enabled') == False else 'Unknown'}
- **Account Created:** {data.get('account_created') or 'N/A'}

## Notes
- Ticket statistics are tracked in the ServiceDesk Intelligence data source
- This file tracks meeting load and availability for capacity planning
- Use this data to identify technicians with high meeting loads impacting ticket work
- Cross-reference with intelligence data for complete workload picture
""",
            "structured_data": {
                "technician_name": tech_name,
                "display_name": data.get('display_name'),
                "job_title": data.get('job_title'),
                "department": data.get('department'),
                "office_location": data.get('office_location'),
                "city": data.get('city'),
                "state": data.get('state'),
                "country": data.get('country'),
                "location_string": location_string,
                "manager_name": data.get('manager_name'),
                "manager_email": data.get('manager_email'),
                "company_name": data.get('company_name'),
                "account_enabled": data.get('account_enabled'),
                "account_created": data.get('account_created'),
                "email": data.get('email'),
                "date": date_str,
                "meeting_count": m['count'],
                "meeting_hours": m['hours'],
                "meeting_percentage": p['meeting_percentage'],
                "available_hours": p['available_hours'],
                "role_multiplier": p['role_multiplier']
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
    """Process all technicians for a single date - v17.1.0 meetings only"""
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
                    'display_name': result['display_name'],
                    'job_title': result['job_title'],
                    'department': result['department'],
                    'office_location': result['office_location'],
                    'city': result['city'],
                    'state': result['state'],
                    'country': result['country'],
                    'manager_name': result['manager_name'],
                    'manager_email': result['manager_email'],
                    'company_name': result['company_name'],
                    'account_enabled': result['account_enabled'],
                    'account_created': result['account_created'],
                    'meetings': result['meetings'],
                    'performance': result['performance']
                }
            except Exception as e:
                logger.error(f"Failed to process {tech_name}: {e}")
    
    # Display results - v17.1.0 simplified (meetings only)
    print()
    print("="*100)
    print(f"{target_date.strftime('%B %d, %Y')}")
    print("="*100)
    
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[0]):
        m = data['meetings']
        p = data['performance']
        job_title = (data.get('job_title') or 'N/A')[:30]
        manager = (data.get('manager_name') or 'N/A')[:20]
        
        # Simple display with meetings, availability, and manager
        print(f"{tech_name:25} | {job_title:30} | Mgr:{manager:20} | Mtgs:{m['count']:2} ({m['hours']:4.1f}h) | Avail:{p['available_hours']:4.1f}h")
    
    print()
    
    filename = save_to_file(tech_data, target_date)
    uploaded = upload_to_dust(tech_data, target_date)
    
    logger.info(f"✓ Saved: {filename}, Uploaded to Dust: {uploaded}/{len(tech_data)}")
    
    return filename
def main():
    """Main backfill execution - v17.1.0 meetings & enhanced profile"""
    parser = argparse.ArgumentParser(description='Backfill meeting and enhanced profile data for date range')
    parser.add_argument('--days', type=int, help='Number of days to backfill')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (YYYY-MM-DD)')
    args = parser.parse_args()
    
    print("="*100)
    print("PERFORMANCE DATA BACKFILL v17.1.0 (Meetings & Enhanced Profile - NO TICKETS)")
    print("="*100)
    print()
    print("Note: Ticket data is handled by the ServiceDesk Intelligence script")
    print("      This script tracks meeting load and enhanced Azure AD profile")
    print()
    print("Enhanced Azure Fields:")
    print("  ✓ Manager (organizational hierarchy)")
    print("  ✓ City, State, Country (detailed location)")
    print("  ✓ Display Name (full proper name)")
    print("  ✓ Company Name")
    print("  ✓ Account Status (enabled/disabled)")
    print("  ✓ Account Created (tenure tracking)")
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
        logger.info("Discovering technicians and fetching enhanced Azure profiles...")
        logger.info("(This includes manager lookup - may take slightly longer)")
        technicians = discover_technicians(sdp_token, graph_token)
        logger.info(f"✓ Found {len(technicians)} technicians with enhanced profiles")
        
        # Display profile summary
        with_managers = sum(1 for t in technicians.values() if t.get('manager_name'))
        with_location = sum(1 for t in technicians.values() if t.get('city'))
        print()
        print(f"Profile enrichment: {with_managers}/{len(technicians)} with managers, {with_location}/{len(technicians)} with detailed location")
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
    
    logger.info(f"Processing {len(dates_to_process)} dates (meetings + enhanced profiles - FAST!)")
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
                time.sleep(2)  # Reduced from 3s since we're faster now
            
        except Exception as e:
            logger.error(f"Failed to process {date.strftime('%Y-%m-%d')}: {e}")
            progress['failed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
    
    total_time = time.time() - start_time
    print()
    print("="*100)
    print("BACKFILL COMPLETE - v17.1.0")
    print("="*100)
    print(f"Total time: {total_time/60:.1f} minutes")
    print(f"Avg time per date: {total_time/len(dates_to_process):.1f} seconds")
    print(f"Processed: {len(dates_to_process)} dates")
    if progress['failed']:
        print(f"Failed: {len(progress['failed'])} dates: {', '.join(progress['failed'])}")
    else:
        print("Failed: 0 dates")
    print()
    print("v17.1.0 Features:")
    print("  ✓ Meeting load tracking (count, hours, percentage)")
    print("  ✓ Job title and department from Azure AD")
    print("  ✓ Manager information (organizational hierarchy)")
    print("  ✓ Detailed location (city, state, country)")
    print("  ✓ Display name and company")
    print("  ✓ Account status (enabled/disabled)")
    print("  ✓ Account creation date (tenure)")
    print("  ✓ Available hours calculation")
    print("  ✓ Role multiplier identification")
    print("  ✓ NO ticket data (handled by intelligence script)")
    print("  ✓ 3-4x faster than previous versions!")
    print()
    print("Data Usage:")
    print("  - Meeting data: Use for capacity planning")
    print("  - Profile data: Use for org structure and routing")
    print("  - Ticket data: Query intelligence data source")
    print("  - Combined view: Cross-reference both sources")
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
