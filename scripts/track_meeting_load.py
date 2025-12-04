#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
v14.5.10: CREDENTIAL CLEANING - Handles malformed secrets
- Fixed: Strips backslashes, quotes, whitespace from credentials
- Fixed: Exits with error code on failure (not silent return)
- Same reliable pagination from v14.5.9
"""

import os
import sys
import requests
import json
import argparse
import logging
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD', help='Process specific date')
parser.add_argument('--debug', action='store_true', help='Show debug output')

# CREDENTIAL CLEANING FUNCTION
def clean_credential(value):
    """Remove common problematic characters from credentials"""
    if not value:
        return value
    # Remove quotes, backslashes, and strip whitespace
    value = value.strip()
    value = value.strip('"').strip("'")  # Remove surrounding quotes
    value = value.replace('\\', '')  # Remove backslashes
    value = value.strip()  # Strip again after cleaning
    return value

# Load and CLEAN all credentials
TENANT_ID = clean_credential(os.environ.get('AZURE_TENANT_ID'))
CLIENT_ID = clean_credential(os.environ.get('AZURE_CLIENT_ID'))
CLIENT_SECRET = clean_credential(os.environ.get('AZURE_CLIENT_SECRET'))

SDP_DOMAIN = clean_credential(os.environ.get('SDP_DOMAIN'))
SDP_CLIENT_ID = clean_credential(os.environ.get('SDP_CLIENT_ID'))
SDP_CLIENT_SECRET = clean_credential(os.environ.get('SDP_CLIENT_SECRET'))
SDP_REFRESH_TOKEN = clean_credential(os.environ.get('SDP_REFRESH_TOKEN'))

DUST_API_KEY = clean_credential(os.environ.get('DUST_API_KEY'))
DUST_WORKSPACE_ID = clean_credential(os.environ.get('DUST_WORKSPACE_ID'))

WORK_HOURS_PER_DAY = 8
PARALLEL_TECHNICIANS = 8
PARALLEL_TICKETS = 10
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
EMAIL_DOMAIN = clean_credential(os.environ.get('EMAIL_DOMAIN', 'motorad.com'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

def get_graph_token():
    """Get Microsoft Graph token with retry logic and rate limit handling"""
    logger.info("Getting Microsoft Graph token...")
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, data=data, timeout=30)
            
            logger.debug(f"Graph token response status: {response.status_code} (attempt {attempt})")
            
            if response.status_code == 200:
                logger.info(f"OK Graph token obtained (attempt {attempt})")
                return response.json()['access_token']
            elif response.status_code == 429:
                wait_time = 2 ** attempt
                logger.warning(f"Rate limited (429), waiting {wait_time}s before retry {attempt}/{max_retries}")
                time.sleep(wait_time)
                continue
            else:
                error_text = response.text[:300] if response.text else "No error message"
                logger.error(f"Graph token failed: {response.status_code} - {error_text}")
                if attempt < max_retries:
                    logger.info(f"Retrying in 2s ({attempt}/{max_retries})...")
                    time.sleep(2)
                    continue
                else:
                    raise Exception(f"Failed to get Graph token after {max_retries} attempts: {response.status_code} - {error_text}")
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error getting Graph token (attempt {attempt}): {e}")
            if attempt < max_retries:
                logger.info(f"Retrying in 2s ({attempt}/{max_retries})...")
                time.sleep(2)
                continue
            else:
                raise Exception(f"Failed to get Graph token after {max_retries} attempts: Network error - {str(e)}")
    
    raise Exception("Failed to get Graph token: Max retries exceeded")

def get_sdp_token():
    """Get ServiceDesk token with retry logic"""
    logger.info("Getting ServiceDesk token...")
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
            
            if response.status_code == 200:
                logger.info(f"OK ServiceDesk token obtained (attempt {attempt})")
                return response.json()['access_token']
            else:
                error_text = response.text[:300] if response.text else "No error message"
                logger.error(f"SDP token failed: {response.status_code} - {error_text}")
                if attempt < max_retries:
                    logger.info(f"Retrying in 2s ({attempt}/{max_retries})...")
                    time.sleep(2)
                    continue
                else:
                    raise Exception(f"Failed to get SDP token after {max_retries} attempts: {response.status_code}")
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error getting SDP token (attempt {attempt}): {e}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            else:
                raise Exception(f"Failed to get SDP token: {str(e)}")
    
    raise Exception("Failed to get SDP token: Max retries exceeded")

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

def discover_active_technicians(sdp_token):
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
    params = {'startDateTime': start_time.isoformat(), 'endDateTime': end_time.isoformat(), '\$select': 'isAllDay,isCancelled,responseStatus,start,end'}
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
    except Exception as e:
        logger.debug(f"Error fetching meetings for {email}: {e}")
    return {'meeting_count': 0, 'total_hours': 0}

def count_closed_tickets_reliable(tech_name, sdp_token, date=None):
    """
    v14.5.9: PROPER PAGINATION - Simple and reliable
    - Fetches enough pages to cover the date range
    - NO early breaking - collects all needed pages first
    - Filters client-side after collecting data
    - Validates results (rejects absurd counts)
    """
    if date is None:
        date = datetime.now()
    if date.date() >= datetime.now().date():
        date = datetime.now() - timedelta(days=1)
    
    target_date = date.date()
    days_ago = (datetime.now().date() - target_date).days
    
    logger.debug(f"Counting closed for {tech_name} on {target_date} ({days_ago} days ago)")
    
    headers = {'Authorization': f'Zoho-oauthtoken {sdp_token}', 'Accept': 'application/vnd.manageengine.sdp.v3+json'}
    
    # GENEROUS PAGE CALCULATION - Better to fetch too many than miss tickets
    if days_ago <= 7:
        max_pages = 2
    elif days_ago <= 14:
        max_pages = 4
    elif days_ago <= 30:
        max_pages = 8
    elif days_ago <= 60:
        max_pages = 12
    else:
        max_pages = 15
    
    logger.debug(f"  Fetching up to {max_pages} pages for {days_ago} day old date")
    
    all_tickets = []
    start_index = 1
    pages_fetched = 0
    
    # FETCH ALL PAGES UP TO LIMIT - NO EARLY BREAKING
    while pages_fetched < max_pages:
        list_info = {
            "list_info": {
                "row_count": 1000,
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
                logger.warning(f"Ticket search failed for {tech_name}: {response.status_code}")
                break
            
            result = response.json()
            tickets = result.get('requests', [])
            
            if not tickets:
                logger.debug(f"  Page {pages_fetched + 1}: No more tickets (end of data)")
                break
            
            pages_fetched += 1
            logger.debug(f"  Page {pages_fetched}: Fetched {len(tickets)} tickets")
            
            all_tickets.extend(tickets)
            
            # Check if more pages exist
            has_more = result.get('list_info', {}).get('has_more_rows', False)
            
            if not has_more or len(tickets) < 1000:
                logger.debug(f"  No more pages available (got {len(all_tickets)} total tickets)")
                break
            
            # Continue to next page
            start_index += 1000
            
        except Exception as e:
            logger.warning(f"Error fetching page {pages_fetched + 1} for {tech_name}: {e}")
            break
    
    # NOW FILTER CLIENT-SIDE TO TARGET DATE
    count = 0
    tickets_on_date = []
    
    for ticket in all_tickets:
        resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
        if resolved_time and resolved_time.date() == target_date:
            count += 1
            tickets_on_date.append(ticket.get('id'))
    
    # VALIDATE: Reject absurd counts (likely data error)
    if count > 100:
        logger.warning(f"  SUSPICIOUS: {tech_name} shows {count} tickets on {target_date} (too many!)")
        logger.warning(f"    This is likely a data issue. Validating with slow path...")
    
    if count > 0:
        logger.info(f"  {tech_name}: {count} tickets on {target_date} (from {len(all_tickets)} total, {pages_fetched} pages)")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"    Ticket IDs: {tickets_on_date[:10]}")
    else:
        logger.debug(f"  {tech_name}: 0 tickets on {target_date}")
    
    return count

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
    except Exception as e:
        logger.debug(f"Error getting active tickets for {tech_name}: {e}")
    
    metrics['closed_today'] = count_closed_tickets_reliable(tech_name, sdp_token, date)
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
    logger.info(f"  Processing {tech_name}...")
    meetings = get_tech_meetings(tech_info.get('email'), graph_token, target_date)
    tickets = get_tech_ticket_metrics(tech_name, sdp_token, target_date)
    performance = calculate_adjusted_performance(meetings, tickets)
    logger.info(f"    -> {tickets['closed_today']} closed, {meetings['meeting_count']} meetings, score {performance['adjusted_score']}")
    return {'name': tech_name, 'meetings': meetings, 'tickets': tickets, 'performance': performance}

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
        except Exception as e:
            failed += 1
    logger.info(f"OK Uploaded: {uploaded}/{len(tech_data)} (failed: {failed})")

def main():
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    print("="*70, flush=True)
    print("TECHNICIAN PERFORMANCE TRACKER v14.5.10 - CLEAN CREDS!", flush=True)
    print(f"   {PARALLEL_TECHNICIANS} parallel technicians | Strips \\, quotes", flush=True)
    print("   Proper pagination | Reliable | Better error handling", flush=True)
    print("="*70, flush=True)
    print(flush=True)
    
    # Get tokens with retry logic - EXIT WITH ERROR CODE IF FAILED
    try:
        graph_token = get_graph_token()
    except Exception as e:
        logger.error(f"Failed to obtain Graph token: {e}")
        sys.exit(1)  # ✅ Exit with error code instead of silent return
    
    try:
        sdp_token = get_sdp_token()
    except Exception as e:
        logger.error(f"Failed to obtain SDP token: {e}")
        sys.exit(1)  # ✅ Exit with error code instead of silent return
    
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d')
    else:
        target_date = datetime.now() - timedelta(days=1)
    
    logger.info(f"Processing: {target_date.strftime('%Y-%m-%d')}")
    
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        logger.error("No technicians found")
        sys.exit(1)
    
    logger.info("Resolving emails...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            info['email'] = resolve_technician_email(tech_name, info, graph_token)
    
    tech_data = {}
    logger.info(f"Analyzing {len(technicians_info)} technicians with parallel processing...")
    
    with ThreadPoolExecutor(max_workers=PARALLEL_TECHNICIANS) as executor:
        future_to_tech = {
            executor.submit(process_single_technician, tech_name, tech_info, graph_token, sdp_token, target_date): tech_name
            for tech_name, tech_info in technicians_info.items()
        }
        
        for future in as_completed(future_to_tech):
            result = future.result()
            tech_data[result['name']] = {'meetings': result['meetings'], 'tickets': result['tickets'], 'performance': result['performance']}
    
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
        sys.exit(1)
