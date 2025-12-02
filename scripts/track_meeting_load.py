#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
FINAL v9: Uses last_updated_time for closed tickets (correct field for your ServiceDesk)
"""

import os
import requests
import json
import argparse
from datetime import datetime, timedelta

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

DEBUG = False

def debug_print(message):
    if DEBUG:
        print(f"  [DEBUG] {message}")

def get_graph_token():
    print("Getting Microsoft Graph token...")
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    response = requests.post(url, data=data)
    if response.status_code == 200:
        print("✓ Graph token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get Graph token")

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
        print("✓ ServiceDesk token obtained")
        return response.json()['access_token']
    raise Exception(f"Failed to get SDP token")

def safe_timestamp_to_date(value):
    """Convert timestamp (string or int) to datetime"""
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

def discover_active_technicians(sdp_token):
    print("\n🔍 Auto-discovering technicians...")
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
        except:
            break
    print(f"✓ Discovered {len(technicians)} active technicians")
    return technicians

def resolve_technician_email(tech_name, tech_info, graph_token):
    if tech_info.get('email'):
        return tech_info['email']
    try:
        headers = {'Authorization': f'Bearer {graph_token}', 'ConsistencyLevel': 'eventual'}
        url = "https://graph.microsoft.com/v1.0/users"
        params = {'$filter': f"displayName eq '{tech_name}'", '$select': 'mail,userPrincipalName'}
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            users = response.json().get('value', [])
            if users:
                return users[0].get('mail') or users[0].get('userPrincipalName')
    except:
        pass
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
        response = requests.get(url, headers=headers, params=params)
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
    except:
        pass
    return {'meeting_count': 0, 'total_hours': 0}

def count_closed_tickets_accurate(tech_name, sdp_token, date=None):
    """
    FINAL ACCURATE VERSION: Uses last_updated_time as primary field
    This is what your ServiceDesk actually updates when tickets are closed
    """
    if date is None:
        date = datetime.now()
    if date.date() >= datetime.now().date():
        date = datetime.now() - timedelta(days=1)
    
    target_date = date.date()
    
    debug_print(f"Counting closed for {tech_name} on {target_date}")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Get ALL closed tickets for this tech, sorted by last_updated_time
    list_info = {
        "list_info": {
            "row_count": 100,
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
            debug_print(f"Search failed: {response.status_code}")
            return 0
        
        result = response.json()
        ticket_ids = [t['id'] for t in result.get('requests', [])]
        
        debug_print(f"Found {len(ticket_ids)} total closed tickets, checking dates...")
        
        count = 0
        
        for ticket_id in ticket_ids:
            ticket = get_ticket_details(ticket_id, sdp_token)
            if not ticket:
                continue
            
            # Verify technician name
            tech_obj = ticket.get('technician')
            if isinstance(tech_obj, dict):
                ticket_tech_name = tech_obj.get('name', '')
            else:
                ticket_tech_name = ''
            
            if ticket_tech_name != tech_name:
                debug_print(f"  ⏭️  Ticket {ticket_id} belongs to {ticket_tech_name}, skipping")
                continue
            
            # Verify status is Closed
            status_obj = ticket.get('status')
            if isinstance(status_obj, dict):
                status_name = status_obj.get('name', '')
            else:
                status_name = ''
            
            if status_name != 'Closed':
                debug_print(f"  ⏭️  Ticket {ticket_id} status is {status_name}, skipping")
                continue
            
            # PRIMARY: Check last_updated_time (this is what ServiceDesk updates!)
            updated_time = safe_timestamp_to_date(ticket.get('last_updated_time'))
            if updated_time and updated_time.date() == target_date:
                count += 1
                debug_print(f"  ✓ Ticket {ticket_id} updated on {updated_time.strftime('%Y-%m-%d %H:%M')}")
                continue
            
            # Fallback 1: completed_time
            completed_time = safe_timestamp_to_date(ticket.get('completed_time'))
            if completed_time and completed_time.date() == target_date:
                count += 1
                debug_print(f"  ✓ Ticket {ticket_id} completed on {completed_time.strftime('%Y-%m-%d %H:%M')}")
                continue
            
            # Fallback 2: resolved_time
            resolved_time = safe_timestamp_to_date(ticket.get('resolved_time'))
            if resolved_time and resolved_time.date() == target_date:
                count += 1
                debug_print(f"  ✓ Ticket {ticket_id} resolved on {resolved_time.strftime('%Y-%m-%d %H:%M')}")
                continue
            
            # Fallback 3: closed_time
            closed_time = safe_timestamp_to_date(ticket.get('closed_time'))
            if closed_time and closed_time.date() == target_date:
                count += 1
                debug_print(f"  ✓ Ticket {ticket_id} closed on {closed_time.strftime('%Y-%m-%d %H:%M')}")
        
        debug_print(f"✅ {tech_name}: {count} tickets closed on {target_date}")
        return count
        
    except Exception as e:
        debug_print(f"Error: {e}")
        return 0

def get_tech_ticket_metrics(tech_name, sdp_token, date=None):
    if date is None:
        date = datetime.now()
    headers = {'Authorization': f'Zoho-oauthtoken {sdp_token}', 'Accept': 'application/vnd.manageengine.sdp.v3+json'}
    metrics = {'active_tickets': 0, 'closed_today': 0}
    
    # Get active tickets
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
    except:
        pass
    
    # Count closed tickets ACCURATELY
    metrics['closed_today'] = count_closed_tickets_accurate(tech_name, sdp_token, date)
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

def save_metrics_to_file(tech_data, date):
    """Save daily metrics to JSON file"""
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    output = {}
    for tech, data in tech_data.items():
        output[tech] = {
            'date': date.isoformat(),
            'meetings': {
                'count': data['meetings']['meeting_count'],
                'hours': data['meetings']['total_hours']
            },
            'tickets': data['tickets'],
            'performance': data['performance']
        }
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n📁 Metrics saved to: {filename}")
    return filename

def upload_to_dust(tech_data, date):
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        return
    print("\n📤 Uploading to Dust...")
    DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    headers = {'Authorization': f'Bearer {DUST_API_KEY}', 'Content-Type': 'application/json'}
    date_str = date.strftime('%Y-%m-%d')
    uploaded = 0
    for tech_name, data in tech_data.items():
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        document = {
            "document_id": f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp": int(date.timestamp() * 1000),
            "tags": ["performance-metrics", f"technician:{tech_name.lower().replace(' ', '-')}", f"date:{date_str}"],
            "text": f"# {tech_name} - {date.strftime('%B %d, %Y')}\nMeetings: {m['meeting_count']} ({m['total_hours']}h)\nClosed: {t['closed_today']} tickets\nActive: {t['active_tickets']} tickets\nScore: {p['adjusted_score']}/100",
            "structured_data": {
                "technician_name": tech_name, "date": date_str, "meeting_count": m['meeting_count'],
                "meeting_hours": m['total_hours'], "tickets_closed_today": t['closed_today'],
                "active_tickets": t['active_tickets'], "velocity_closed": p['velocity_closed'], "adjusted_score": p['adjusted_score']
            }
        }
        url = f"{DUST_API_BASE}/{document['document_id']}"
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code in [200, 201]:
                uploaded += 1
        except:
            pass
    print(f"✓ Uploaded: {uploaded}")

def main():
    global DEBUG
    args = parser.parse_args()
    DEBUG = args.debug
    print("="*70)
    print("🚀 TECHNICIAN PERFORMANCE TRACKER v9 FINAL")
    print("   ✨ Uses last_updated_time for closed tickets")
    print("="*70)
    print()
    graph_token = get_graph_token()
    sdp_token = get_sdp_token()
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d')
    else:
        target_date = datetime.now() - timedelta(days=1)
    print(f"📅 Processing: {target_date.strftime('%Y-%m-%d')}")
    print(f"   (May take 1-2 min)\n")
    technicians_info = discover_active_technicians(sdp_token)
    if not technicians_info:
        print("❌ No technicians found")
        return
    print("\n📧 Resolving emails...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            info['email'] = resolve_technician_email(tech_name, info, graph_token)
    tech_data = {}
    print(f"\n📊 Analyzing {len(technicians_info)} technicians...\n")
    for tech_name, tech_info in technicians_info.items():
        print(f"{tech_name}...", end=' ', flush=True)
        meetings = get_tech_meetings(tech_info.get('email'), graph_token, target_date)
        tickets = get_tech_ticket_metrics(tech_name, sdp_token, target_date)
        performance = calculate_adjusted_performance(meetings, tickets)
        print(f"{tickets['closed_today']} closed, score {performance['adjusted_score']}")
        tech_data[tech_name] = {'meetings': meetings, 'tickets': tickets, 'performance': performance}
    print("\n" + "="*70)
    print(f"📊 {target_date.strftime('%B %d, %Y')}")
    print("="*70)
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[1]['performance']['adjusted_score'], reverse=True):
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        print(f"{tech_name:25} | {t['closed_today']:2} closed | {m['meeting_count']:2} mtgs ({m['total_hours']:4.1f}h) | Score: {p['adjusted_score']:5.1f}")
    
    # Save JSON file
    save_metrics_to_file(tech_data, target_date)
    
    # Upload to Dust
    upload_to_dust(tech_data, target_date)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
