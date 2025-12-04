#!/usr/bin/env python3
"""
Daily Technician Performance Tracker v15.0.0
Simple, reliable, daily execution only
- Processes yesterday's data ONLY
- Gets meetings from Microsoft Graph
- Gets closed tickets from ServiceDesk
- Saves JSON and uploads to Dust
"""

import os
import sys
import requests
import json
import logging
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
WORK_HOURS_PER_DAY = 8
PARALLEL_TECHNICIANS = 8
EMAIL_DOMAIN = 'motorad.com'

# Credential cleaning
def clean_credential(value):
    """Remove problematic characters from credentials"""
    if not value:
        return value
    value = value.strip().strip('"').strip("'")
    value = value.replace('\\', '')
    return value.strip()

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

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

def get_graph_token():
    """Get Microsoft Graph token"""
    logger.info("Getting Graph token...")
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    
    try:
        response = requests.post(url, data=data, timeout=30)
        if response.status_code == 200:
            logger.info("✓ Graph token obtained")
            return response.json()['access_token']
        else:
            logger.error(f"Graph token failed: {response.status_code}")
            raise Exception(f"Graph token failed: {response.text[:200]}")
    except Exception as e:
        logger.error(f"Failed to get Graph token: {e}")
        raise

def get_sdp_token():
    """Get ServiceDesk token"""
    logger.info("Getting ServiceDesk token...")
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    
    try:
        response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
        if response.status_code == 200:
            logger.info("✓ ServiceDesk token obtained")
            return response.json()['access_token']
        else:
            logger.error(f"SDP token failed: {response.status_code}")
            raise Exception(f"SDP token failed: {response.text[:200]}")
    except Exception as e:
        logger.error(f"Failed to get SDP token: {e}")
        raise

def discover_technicians(sdp_token):
    """Find active technicians from open tickets"""
    logger.info("Discovering technicians...")
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
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 200:
            tickets = response.json().get('requests', [])
            for ticket in tickets:
                tech = ticket.get('technician')
                if tech and isinstance(tech, dict):
                    name = tech.get('name')
                    email = tech.get('email_id')
                    if name and name != 'Unassigned' and name not in technicians:
                        technicians[name] = email
            
            logger.info(f"✓ Found {len(technicians)} technicians")
            return technicians
    except Exception as e:
        logger.error(f"Failed to discover technicians: {e}")
        raise

def get_meetings(email, graph_token, date):
    """Get meetings for a technician on specific date"""
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
            return {'count': count, 'hours': round(total_minutes / 60, 2)}
    except Exception as e:
        logger.debug(f"Error fetching meetings for {email}: {e}")
        return {'count': 0, 'hours': 0}

def safe_timestamp_to_date(value):
    """Convert ServiceDesk timestamp to datetime"""
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

def count_closed_tickets(tech_name, sdp_token, target_date):
    """
    Count tickets closed by technician on specific date
    Simple approach: Fetch recent closed tickets, filter to target date
    """
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Fetch closed tickets for technician (recent, sorted by resolved_time desc)
    list_info = {
        "list_info": {
            "row_count": 1000,
            "start_index": 1,
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
            logger.warning(f"Failed to get tickets for {tech_name}: {response.status_code}")
            return 0
        
        tickets = response.json().get('requests', [])
        
        # Filter to target date
        count = 0
        for ticket in tickets:
            resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
            if resolved_time and resolved_time.date() == target_date:
                count += 1
        
        return count
        
    except Exception as e:
        logger.warning(f"Error counting tickets for {tech_name}: {e}")
        return 0

def get_active_tickets(tech_name, sdp_token):
    """Get count of active (non-closed) tickets"""
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
            return len(response.json().get('requests', []))
    except Exception as e:
        logger.debug(f"Error getting active tickets for {tech_name}: {e}")
    
    return 0

def calculate_performance(meetings, tickets_closed, active_tickets):
    """Calculate performance metrics"""
    meeting_hours = meetings['hours']
    available_hours = max(0.5, WORK_HOURS_PER_DAY - meeting_hours)
    
    velocity = tickets_closed / available_hours if available_hours > 0 else 0
    
    # Score based on velocity and workload
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

def process_technician(tech_name, tech_email, graph_token, sdp_token, target_date):
    """Process single technician's data"""
    logger.info(f"  Processing {tech_name}...")
    
    meetings = get_meetings(tech_email, graph_token, target_date)
    closed = count_closed_tickets(tech_name, sdp_token, target_date.date())
    active = get_active_tickets(tech_name, sdp_token)
    performance = calculate_performance(meetings, closed, active)
    
    logger.info(f"    → {closed} closed, {meetings['count']} meetings, {active} active, score {performance['score']}")
    
    return {
        'name': tech_name,
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
            'meetings': {
                'count': data['meetings']['count'],
                'hours': data['meetings']['hours']
            },
            'tickets': data['tickets'],
            'performance': data['performance']
        }
    
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    logger.info(f"✓ Saved: {filename}")
    return filename

def upload_to_dust(tech_data, date):
    """Upload metrics to Dust"""
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        logger.warning("Dust credentials missing, skipping upload")
        return
    
    logger.info("Uploading to Dust...")
    
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
        
        document = {
            "document_id": f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp": int(date.timestamp() * 1000),
            "tags": [
                "performance-metrics",
                f"technician:{tech_name.lower().replace(' ', '-')}",
                f"date:{date_str}"
            ],
            "text": f"""# {tech_name} - {date.strftime('%B %d, %Y')}

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
        except Exception as e:
            logger.debug(f"Failed to upload {tech_name}: {e}")
    
    logger.info(f"✓ Uploaded {uploaded}/{len(tech_data)} to Dust")

def main():
    """Main execution"""
    print("="*70)
    print("DAILY PERFORMANCE TRACKER v15.0.0")
    print("="*70)
    print()
    
    # Get tokens
    try:
        graph_token = get_graph_token()
        sdp_token = get_sdp_token()
    except Exception as e:
        logger.error(f"Failed to get tokens: {e}")
        sys.exit(1)
    
    # Process yesterday
    target_date = datetime.now() - timedelta(days=1)
    logger.info(f"Processing: {target_date.strftime('%Y-%m-%d')}")
    
    # Discover technicians
    try:
        technicians = discover_technicians(sdp_token)
    except Exception as e:
        logger.error(f"Failed to discover technicians: {e}")
        sys.exit(1)
    
    if not technicians:
        logger.error("No technicians found")
        sys.exit(1)
    
    # Process all technicians in parallel
    tech_data = {}
    logger.info(f"Processing {len(technicians)} technicians...")
    
    with ThreadPoolExecutor(max_workers=PARALLEL_TECHNICIANS) as executor:
        futures = {
            executor.submit(
                process_technician,
                tech_name,
                tech_email,
                graph_token,
                sdp_token,
                target_date
            ): tech_name
            for tech_name, tech_email in technicians.items()
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                tech_data[result['name']] = {
                    'meetings': result['meetings'],
                    'tickets': result['tickets'],
                    'performance': result['performance']
                }
            except Exception as e:
                tech_name = futures[future]
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
        print(f"{tech_name:25} | {t['closed_today']:2} closed | {m['count']:2} mtgs | Score: {p['score']:5.1f}")
    
    # Save and upload
    save_to_file(tech_data, target_date)
    upload_to_dust(tech_data, target_date)
    
    print()
    logger.info("✓ Complete!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
