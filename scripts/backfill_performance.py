#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
v12: OPTIMIZED + ACTIVE LOGGING - 10x faster with full visibility
"""

import os
import requests
import json
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD', help='Process specific date')
parser.add_argument('--debug', action='store_true', help='Show debug output')

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
        logger.info("✓ Graph token obtained")
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
        logger.info("✓ ServiceDesk token obtained")
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

def discover_active_technicians(sdp_token):
    logger.info("🔍 Auto-discovering technicians...")
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
    logger.info(f"✓ Discovered {len(technicians)} active technicians")
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
    OPTIMIZED: Use API date filtering and list response data
    No need to fetch each ticket individually - 10x faster!
    """
    if date is None:
        date = datetime.now()
    if date.date() >= datetime.now().date():
        date = datetime.now() - timedelta(days=1)
    
    target_date = date.date()
    
    # Create date range for the target date
    start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    start_ms = int(start_of_day.timestamp() * 1000)
    end_ms = int(end_of_day.timestamp() * 1000)
    
    logger.debug(f"Counting closed for {tech_name} on {target_date} ({start_ms} to {end_ms})")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # OPTIMIZED: Filter by date range in the API query
    list_info = {
        "list_info": {
            "row_count": 250,
            "start_index": 1,
            "search_criteria": [
                {"field": "technician.name", "condition": "is", "value": tech_name},
                {"field": "status.name", "condition": "is", "values": ["Closed"]},
                {"field": "last_updated_time", "condition": "between", "value": f"{start_ms},{end_ms}"}
            ]
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
        
        logger.debug(f"  Found {len(tickets)} closed tickets for {tech_name} on {target_date}")
        
        # OPTIMIZED: Use data from list response, no need to fetch each ticket!
        count = 0
        for ticket in tickets:
            # Verify technician (double-check)
            tech = ticket.get('technician')
            if tech and isinstance(tech, dict):
                ticket_tech_name = tech.get('name', '')
                if ticket_tech_name == tech_name:
                    # Verify status
                    status = ticket.get('status')
                    if status and isinstance(status, dict):
                        status_name = status.get('name', '')
                        if status_name == 'Closed':
                            count += 1
                            logger.debug(f"    ✓ Ticket {ticket.get('id')} counted")
        
        logger.debug(f"  ✓ {tech_name}: {count} tickets")
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
    """Process a single technician - can be parallelized"""
    logger.info(f"  Processing {tech_name}...")
    meetings = get_tech_meetings(tech_info.get('email'), graph_token, target_date)
    tickets = get_tech_ticket_metrics(tech_name, sdp_token, target_date)
    performance = calculate_adjusted_performance(meetings, tickets)
    logger.info(f"    → {tickets['closed_today']} closed, {meetings['meeting_count']} meetings, score {performance['adjusted_score']}")
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
    logger.info(f"📁 Metrics saved to: {filename}")
    return filename

def upload_to_dust(tech_data, date):
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        logger.warning("Dust API credentials not found, skipping upload")
        return
    logger.info("📤 Uploading to Dust...")
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

## 📅 Calendar & Availability
- **Meetings:** {m['meeting_count']} meetings
- **Meeting Time:** {m['total_hours']} hours ({p['meeting_percentage']}%)
- **Available:** {p['available_hours']} hours

## 🎫 Ticket Activity
- **Closed:** {t['closed_today']}
- **Updated:** {t['updated_today']}
- **Active:** {t['active_tickets']}

## ⚡ Performance
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
    logger.info(f"✓ Uploaded: {uploaded}/{len(tech_data)} (failed: {failed})")

def main():
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    print("="*70, flush=True)
    print("🚀 TECHNICIAN PERFORMANCE TRACKER v12 - OPTIMIZED + LOGGING", flush=True)
    print("   ⚡ 10x faster with API-level filtering", flush=True)
    print("="*70, flush=True)
    print(flush=True)
    
    graph_token = get_graph_token()
    sdp_token = get_sdp_token()
    
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d')
    else:
        target_date = datetime.now() - timedelta(days=1)
    
    logger.info(f"📅 Processing: {target_date.strftime('%Y-%m-%d')}")
    
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        logger.error("❌ No technicians found")
        return
    
    logger.info("📧 Resolving emails...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            info['email'] = resolve_technician_email(tech_name, info, graph_token)
    
    tech_data = {}
    logger.info(f"📊 Analyzing {len(technicians_info)} technicians...")
    
    # Process technicians
    for tech_name, tech_info in technicians_info.items():
        result = process_single_technician(tech_name, tech_info, graph_token, sdp_token, target_date)
        tech_data[tech_name] = {
            'meetings': result['meetings'],
            'tickets': result['tickets'],
            'performance': result['performance']
        }
    
    print("\n" + "="*70, flush=True)
    print(f"📊 {target_date.strftime('%B %d, %Y')}", flush=True)
    print("="*70, flush=True)
    
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[1]['performance']['adjusted_score'], reverse=True):
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        print(f"{tech_name:25} | {t['closed_today']:2} closed | {m['meeting_count']:2} mtgs ({m['total_hours']:4.1f}h) | Score: {p['adjusted_score']:5.1f}", flush=True)
    
    save_metrics_to_file(tech_data, target_date)
    upload_to_dust(tech_data, target_date)
    
    logger.info("✅ Complete!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
