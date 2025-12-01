#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
FIXED: Fetches individual tickets to get date fields
"""

import os
import requests
import json
import argparse
from datetime import datetime, timedelta

parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--backfill', type=int, metavar='DAYS', help='Backfill historical data')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD', help='Process specific date')
parser.add_argument('--yesterday', action='store_true', help='Process yesterday')
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
    raise Exception(f"Failed to get Graph token: {response.status_code}")

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
    raise Exception(f"Failed to get SDP token: {response.status_code}")

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
    except Exception as e:
        debug_print(f"Error fetching ticket {ticket_id}: {e}")
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
                {
                    "field": "status.name",
                    "condition": "is not",
                    "values": ["Closed", "Cancelled"]
                }
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
                                technicians[tech_name] = {
                                    'email': tech_email,
                                    'id': tech.get('id'),
                                    'ticket_count': 0
                                }
                            technicians[tech_name]['ticket_count'] += 1
                if not result.get('list_info', {}).get('has_more_rows', False):
                    break
                start_index += 100
                pages_checked += 1
            else:
                break
        except:
            break
    
    print(f"✓ Discovered {len(technicians)} technicians")
    return technicians

def resolve_technician_email(tech_name, tech_info, graph_token):
    if tech_info.get('email'):
        return tech_info['email']
    try:
        headers = {
            'Authorization': f'Bearer {graph_token}',
            'ConsistencyLevel': 'eventual'
        }
        url = "https://graph.microsoft.com/v1.0/users"
        params = {
            '$filter': f"displayName eq '{tech_name}'",
            '$select': 'mail,userPrincipalName'
        }
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
        return {'meeting_count': 0, 'total_hours': 0, 'meetings': []}
    if date is None:
        date = datetime.now()
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
        '$select': 'subject,start,end,isAllDay,isCancelled,responseStatus'
    }
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            events = response.json().get('value', [])
            valid_meetings = []
            total_minutes = 0
            for event in events:
                if event.get('isCancelled'):
                    continue
                if event.get('responseStatus', {}).get('response') == 'declined':
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
                valid_meetings.append({
                    'subject': 'Busy',
                    'duration_minutes': duration_minutes
                })
                total_minutes += duration_minutes
            total_minutes = min(total_minutes, WORK_HOURS_PER_DAY * 60)
            return {
                'meeting_count': len(valid_meetings),
                'total_hours': round(total_minutes / 60, 2),
                'meetings': valid_meetings
            }
    except:
        pass
    return {'meeting_count': 0, 'total_hours': 0, 'meetings': []}

def get_tech_ticket_metrics(tech_name, sdp_token, date=None):
    """FIXED: Fetches individual tickets to get date fields"""
    if date is None:
        date = datetime.now()
    
    start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    debug_print(f"Searching tickets for {tech_name} on {date.strftime('%Y-%m-%d')}")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    metrics = {
        'active_tickets': 0,
        'closed_today': 0,
        'updated_today': 0,
        'overdue_count': 0
    }
    
    # Get active tickets
    active_list_info = {
        "list_info": {
            "row_count": 250,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "technician.name",
                    "condition": "is",
                    "value": tech_name
                },
                {
                    "field": "status.name",
                    "condition": "is not",
                    "values": ["Closed", "Cancelled"]
                }
            ]
        }
    }
    
    try:
        params = {'input_data': json.dumps(active_list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 200:
            result = response.json()
            active_tickets = result.get('requests', [])
            metrics['active_tickets'] = len(active_tickets)
            debug_print(f"Found {len(active_tickets)} active tickets")
    except:
        pass
    
    # Get closed tickets - fetch individually to get date fields
    closed_list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "technician.name",
                    "condition": "is",
                    "value": tech_name
                },
                {
                    "field": "status.name",
                    "condition": "is",
                    "values": ["Closed"]
                }
            ],
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    try:
        debug_print(f"Searching for closed tickets...")
        params = {'input_data': json.dumps(closed_list_info)}
        url = f"{SDP_API_BASE}/requests"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            closed_ticket_ids = [t['id'] for t in result.get('requests', [])]
            debug_print(f"Found {len(closed_ticket_ids)} closed tickets, fetching details...")
            
            closed_count = 0
            
            # Fetch each ticket individually (this is slow but necessary)
            for ticket_id in closed_ticket_ids[:50]:  # Limit to 50 most recent
                ticket = get_ticket_details(ticket_id, sdp_token)
                if not ticket:
                    continue
                
                # Check completed_time (preferred for closed tickets)
                completed_time = ticket.get('completed_time')
                completed_date = safe_timestamp_to_date(completed_time)
                
                if completed_date and start_of_day <= completed_date <= end_of_day:
                    closed_count += 1
                    debug_print(f"  ✓ Ticket #{ticket_id} completed on {completed_date.strftime('%Y-%m-%d %H:%M')}")
                    continue
                
                # Fallback to resolved_time
                resolved_time = ticket.get('resolved_time')
                resolved_date = safe_timestamp_to_date(resolved_time)
                
                if resolved_date and start_of_day <= resolved_date <= end_of_day:
                    closed_count += 1
                    debug_print(f"  ✓ Ticket #{ticket_id} resolved on {resolved_date.strftime('%Y-%m-%d %H:%M')}")
            
            metrics['closed_today'] = closed_count
            debug_print(f"✅ TOTAL CLOSED: {closed_count} tickets")
    
    except Exception as e:
        debug_print(f"Error getting closed tickets: {e}")
    
    return metrics

def calculate_adjusted_performance(meeting_data, ticket_metrics):
    meeting_hours = meeting_data['total_hours']
    available_hours = max(0.5, WORK_HOURS_PER_DAY - meeting_hours)
    tickets_closed = ticket_metrics['closed_today']
    tickets_updated = ticket_metrics['updated_today']
    velocity_closed = tickets_closed / available_hours if available_hours > 0 else 0
    velocity_updated = tickets_updated / available_hours if available_hours > 0 else 0
    score = 0
    score += min(40, velocity_closed * 10)
    score += min(30, velocity_updated * 5)
    active = ticket_metrics['active_tickets']
    if 5 <= active <= 15:
        score += 15
    elif active < 5:
        score += active * 3
    else:
        score += max(0, 15 - (active - 15) * 0.5)
    score -= ticket_metrics['overdue_count'] * 5
    score = max(0, min(100, score))
    return {
        'available_hours': round(available_hours, 2),
        'velocity_closed': round(velocity_closed, 2),
        'velocity_updated': round(velocity_updated, 2),
        'adjusted_score': round(score, 1),
        'meeting_percentage': round((meeting_hours / WORK_HOURS_PER_DAY) * 100, 1)
    }

def upload_to_dust(tech_data, date):
    print("\n📤 Uploading to Dust...")
    DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    date_str = date.strftime('%Y-%m-%d')
    uploaded = 0
    for tech_name, data in tech_data.items():
        meetings = data['meetings']
        tickets = data['tickets']
        perf = data['performance']
        document = {
            "document_id": f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp": int(date.timestamp() * 1000),
            "tags": [
                "performance-metrics",
                f"technician:{tech_name.lower().replace(' ', '-')}",
                f"date:{date_str}",
                f"meeting-load:{meetings['meeting_count']}"
            ],
            "text": f"""# {tech_name} - {date.strftime('%B %d, %Y')}

## Calendar
- Meetings: {meetings['meeting_count']} ({meetings['total_hours']}h, {perf['meeting_percentage']}%)
- Available: {perf['available_hours']}h

## Tickets
- Closed: {tickets['closed_today']}
- Updated: {tickets['updated_today']}
- Active: {tickets['active_tickets']}
- Overdue: {tickets['overdue_count']}

## Performance
- Velocity: {perf['velocity_closed']} tickets/hour
- Score: {perf['adjusted_score']}/100
""",
            "structured_data": {
                "technician_name": tech_name,
                "date": date_str,
                "meeting_count": meetings['meeting_count'],
                "meeting_hours": meetings['total_hours'],
                "available_hours": perf['available_hours'],
                "tickets_closed_today": tickets['closed_today'],
                "tickets_updated_today": tickets['updated_today'],
                "active_tickets": tickets['active_tickets'],
                "overdue_tickets": tickets['overdue_count'],
                "velocity_closed": perf['velocity_closed'],
                "adjusted_score": perf['adjusted_score']
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
    print("🚀 TECHNICIAN PERFORMANCE TRACKER v4")
    print("   ✨ FIXED: Fetches individual tickets for accurate dates")
    print("="*70)
    print()
    
    graph_token = get_graph_token()
    sdp_token = get_sdp_token()
    
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d')
    elif args.yesterday:
        target_date = datetime.now() - timedelta(days=1)
    else:
        target_date = datetime.now()
    
    print(f"📅 Processing: {target_date.strftime('%Y-%m-%d')}\n")
    
    technicians_info = discover_active_technicians(sdp_token)
    
    print("\n📧 Resolving emails...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            info['email'] = resolve_technician_email(tech_name, info, graph_token)
    
    tech_data = {}
    print(f"\n📊 Analyzing {len(technicians_info)} technicians...\n")
    
    for tech_name, tech_info in technicians_info.items():
        print(f"Processing {tech_name}...")
        meetings = get_tech_meetings(tech_info.get('email'), graph_token, target_date)
        print(f"  ✓ Meetings: {meetings['meeting_count']} ({meetings['total_hours']}h)")
        tickets = get_tech_ticket_metrics(tech_name, sdp_token, target_date)
        print(f"  ✓ Tickets: {tickets['closed_today']} closed")
        performance = calculate_adjusted_performance(meetings, tickets)
        print(f"  ✓ Score: {performance['adjusted_score']}/100\n")
        tech_data[tech_name] = {
            'email': tech_info.get('email'),
            'meetings': meetings,
            'tickets': tickets,
            'performance': performance
        }
    
    print("\n" + "="*70)
    print(f"📊 PERFORMANCE REPORT - {target_date.strftime('%B %d, %Y')}")
    print("="*70)
    
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[1]['performance']['adjusted_score'], reverse=True):
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        print(f"\n👤 {tech_name}")
        print(f"   📅 {m['meeting_count']} meetings ({m['total_hours']}h, {p['meeting_percentage']}%)")
        print(f"   🎫 {t['closed_today']} closed | {t['active_tickets']} active")
        print(f"   ⚡ {p['velocity_closed']} tickets/hour | ⭐ {p['adjusted_score']}/100")
    
    print()
    
    if DUST_API_KEY and DUST_WORKSPACE_ID:
        upload_to_dust(tech_data, target_date)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
