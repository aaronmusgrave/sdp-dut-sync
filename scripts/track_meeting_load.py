#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
AUTO-DISCOVERS technicians from ServiceDesk ticket assignments
PRIVACY MODE: Meeting titles are hidden, only shows "Busy"
SAVES TO: servicedesk data source (unified with ticket data)
FIXED v3: 
- Manual date validation for closed tickets
- Fixed meeting hour calculation (handles all-day events)
- Defaults to yesterday's closed tickets (more reliable)
- Added debugging output
"""

import os
import requests
import json
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

# Argument parser
parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--backfill', type=int, metavar='DAYS', 
                    help='Backfill historical data for the last N days (max 365)')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD',
                    help='Process specific date (format: 2025-11-25)')
parser.add_argument('--today', action='store_true',
                    help='Process today (default is yesterday for complete data)')
parser.add_argument('--debug', action='store_true',
                    help='Show debug output')

# Microsoft Graph API credentials
TENANT_ID = os.environ.get('AZURE_TENANT_ID')
CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

# ServiceDesk credentials
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')

WORK_HOURS_PER_DAY = 8
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

# Email domain for your organization (fallback if email not in ServiceDesk)
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'motorad.com')

DEBUG = False

def debug_print(message):
    """Print debug messages if debug mode is on"""
    if DEBUG:
        print(f"  [DEBUG] {message}")

def get_graph_token():
    """Get Microsoft Graph API access token"""
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
    else:
        raise Exception(f"Failed to get Graph token: {response.status_code}")

def get_sdp_token():
    """Get ServiceDesk Plus access token"""
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
            print("✓ ServiceDesk token obtained")
            return token_data['access_token']
        else:
            print(f"❌ Token request failed: {response.status_code}")
            print(f"Response: {response.text}")
            raise Exception(f"Failed to get SDP token: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error: {e}")
        raise Exception(f"Failed to connect to Zoho: {e}")

def discover_active_technicians(sdp_token):
    """Auto-discover technicians from ServiceDesk by finding all unique technicians assigned to active tickets"""
    print("\n🔍 Auto-discovering technicians from ServiceDesk...")
    
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
                    "values": ["Closed", "Resolved", "Cancelled"]
                }
            ]
        }
    }
    
    technicians = {}
    start_index = 1
    pages_checked = 0
    max_pages = 20
    
    while pages_checked < max_pages:
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
                
                has_more = result.get('list_info', {}).get('has_more_rows', False)
                if not has_more:
                    break
                
                start_index += 100
                pages_checked += 1
                
            else:
                print(f"  ⚠️  API error {response.status_code}")
                break
                
        except Exception as e:
            print(f"  ⚠️  Error: {e}")
            break
    
    print(f"✓ Discovered {len(technicians)} active technicians")
    
    return technicians

def resolve_technician_email(tech_name, tech_info, graph_token):
    """Resolve technician email address"""
    
    if tech_info.get('email'):
        return tech_info['email']
    
    debug_print(f"Resolving email for {tech_name}...")
    
    try:
        headers = {
            'Authorization': f'Bearer {graph_token}',
            'ConsistencyLevel': 'eventual'
        }
        
        url = "https://graph.microsoft.com/v1.0/users"
        params = {
            '$filter': f"displayName eq '{tech_name}'",
            '$select': 'mail,userPrincipalName,displayName'
        }
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            users = response.json().get('value', [])
            if users:
                email = users[0].get('mail') or users[0].get('userPrincipalName')
                return email
    except Exception as e:
        debug_print(f"Azure AD lookup failed: {e}")
    
    name_parts = tech_name.lower().split()
    if len(name_parts) >= 2:
        generated_email = f"{name_parts[0]}.{name_parts[-1]}@{EMAIL_DOMAIN}"
        return generated_email
    
    return None

def get_tech_meetings(email, graph_token, date=None):
    """Get meetings for a technician on a specific date - FIXED: Handles all-day events correctly"""
    
    if not email:
        return {'meeting_count': 0, 'total_hours': 0, 'meetings': []}
    
    if date is None:
        date = datetime.now()
    
    # Set time range for the day (8 AM - 5 PM)
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
                
                response_status = event.get('responseStatus', {}).get('response', 'none')
                if response_status == 'declined':
                    continue
                
                # Handle all-day events
                if event.get('isAllDay'):
                    # All-day events count as full work day
                    duration_minutes = WORK_HOURS_PER_DAY * 60
                    debug_print(f"All-day event detected, counting as {WORK_HOURS_PER_DAY}h")
                else:
                    # Calculate duration for regular events
                    start = datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(event['end']['dateTime'].replace('Z', '+00:00'))
                    
                    # Clamp to work hours (8 AM - 5 PM)
                    work_start = start_time
                    work_end = end_time
                    
                    meeting_start = max(start, work_start)
                    meeting_end = min(end, work_end)
                    
                    if meeting_end > meeting_start:
                        duration_minutes = (meeting_end - meeting_start).total_seconds() / 60
                    else:
                        continue  # Meeting outside work hours
                
                # Cap individual meetings at work day length
                duration_minutes = min(duration_minutes, WORK_HOURS_PER_DAY * 60)
                
                valid_meetings.append({
                    'subject': 'Busy',
                    'start': start if not event.get('isAllDay') else start_time,
                    'end': end if not event.get('isAllDay') else end_time,
                    'duration_minutes': duration_minutes,
                    'is_all_day': event.get('isAllDay', False)
                })
                
                total_minutes += duration_minutes
            
            # Cap total at work day length
            total_minutes = min(total_minutes, WORK_HOURS_PER_DAY * 60)
            
            return {
                'meeting_count': len(valid_meetings),
                'total_hours': round(total_minutes / 60, 2),
                'meetings': valid_meetings
            }
        elif response.status_code == 404:
            return {'meeting_count': 0, 'total_hours': 0, 'meetings': [], 'error': 'user_not_found'}
        else:
            return {'meeting_count': 0, 'total_hours': 0, 'meetings': []}
    
    except Exception as e:
        debug_print(f"Error getting meetings: {e}")
        return {'meeting_count': 0, 'total_hours': 0, 'meetings': []}

def get_tech_ticket_metrics(tech_name, sdp_token, date=None):
    """Get ticket metrics for a technician on a specific date - WITH DEBUGGING"""
    
    if date is None:
        date = datetime.now() - timedelta(days=1)  # Default to yesterday
    
    start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    start_ms = int(start_of_day.timestamp() * 1000)
    end_ms = int(end_of_day.timestamp() * 1000)
    
    debug_print(f"Searching tickets for {tech_name} on {date.strftime('%Y-%m-%d')}")
    debug_print(f"Date range: {start_ms} to {end_ms}")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    metrics = {
        'active_tickets': 0,
        'closed_today': 0,
        'updated_today': 0,
        'avg_response_time_hours': None,
        'overdue_count': 0,
        'high_complexity_count': 0
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
                    "values": ["Closed", "Resolved", "Cancelled"]
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
            
            for ticket in active_tickets:
                if ticket.get('is_overdue'):
                    metrics['overdue_count'] += 1
                    
                updated_time = ticket.get('last_updated_time', {})
                if isinstance(updated_time, dict):
                    updated_ms = updated_time.get('value')
                    if updated_ms and start_ms <= updated_ms <= end_ms:
                        metrics['updated_today'] += 1
    
    except Exception as e:
        debug_print(f"Error getting active tickets: {e}")
    
    # Get closed tickets with manual date validation
    closed_list_info = {
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
                    "condition": "is",
                    "values": ["Closed", "Resolved"]
                }
            ],
            "sort_field": "resolved_time",
            "sort_order": "desc"
        }
    }
    
    try:
        closed_count = 0
        start_index = 1
        pages_checked = 0
        max_pages = 5
        tickets_examined = 0
        
        debug_print(f"Searching for closed tickets...")
        
        while pages_checked < max_pages:
            closed_list_info['list_info']['start_index'] = start_index
            params = {'input_data': json.dumps(closed_list_info)}
            url = f"{SDP_API_BASE}/requests"
            
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                closed_tickets = result.get('requests', [])
                
                if not closed_tickets:
                    debug_print(f"No more closed tickets on page {pages_checked + 1}")
                    break
                
                found_tickets_in_range = False
                
                for ticket in closed_tickets:
                    tickets_examined += 1
                    resolved_time = ticket.get('resolved_time', {})
                    
                    if isinstance(resolved_time, dict):
                        resolved_ms = resolved_time.get('value')
                        
                        if resolved_ms:
                            resolved_date = datetime.fromtimestamp(resolved_ms / 1000)
                            
                            if start_ms <= resolved_ms <= end_ms:
                                closed_count += 1
                                found_tickets_in_range = True
                                debug_print(f"  Found closed ticket #{ticket.get('id')} resolved on {resolved_date}")
                            elif resolved_ms < start_ms:
                                debug_print(f"  Ticket #{ticket.get('id')} too old ({resolved_date}), stopping search")
                                break
                
                if not found_tickets_in_range and pages_checked > 0:
                    debug_print(f"No tickets in date range on page {pages_checked + 1}, stopping")
                    break
                
                has_more = result.get('list_info', {}).get('has_more_rows', False)
                if not has_more:
                    debug_print(f"No more pages")
                    break
                
                start_index += 250
                pages_checked += 1
            else:
                debug_print(f"API error: {response.status_code}")
                break
        
        metrics['closed_today'] = closed_count
        debug_print(f"Total closed tickets found: {closed_count} (examined {tickets_examined} total tickets)")
    
    except Exception as e:
        debug_print(f"Error getting closed tickets: {e}")
    
    return metrics

def calculate_adjusted_performance(meeting_data, ticket_metrics):
    """Calculate performance adjusted for meeting load"""
    
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

def upload_to_dust_historical(tech_data, date):
    """Upload historical performance data to Dust"""
    
    DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    date_str = date.strftime('%Y-%m-%d')
    
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
                f"meeting-load:{meetings['meeting_count']}",
                "historical-backfill"
            ],
            "text": f"""# Technician Performance - {tech_name}
**Date:** {date.strftime('%B %d, %Y')}

## 📅 Calendar & Availability
- **Meetings:** {meetings['meeting_count']} meetings
- **Meeting Time:** {meetings['total_hours']} hours ({perf['meeting_percentage']}%)
- **Available:** {perf['available_hours']} hours

## 🎫 Ticket Activity
- **Closed:** {tickets['closed_today']}
- **Updated:** {tickets['updated_today']}
- **Active:** {tickets['active_tickets']}
- **Overdue:** {tickets['overdue_count']}

## ⚡ Performance
- **Velocity:** {perf['velocity_closed']} tickets/hour
- **Score:** {perf['adjusted_score']}/100
""",
            "structured_data": {
                "technician_name": tech_name,
                "date": date_str,
                "timestamp_ms": int(date.timestamp() * 1000),
                "meeting_count": meetings['meeting_count'],
                "meeting_hours": meetings['total_hours'],
                "meeting_percentage": perf['meeting_percentage'],
                "available_hours": perf['available_hours'],
                "work_hours_total": WORK_HOURS_PER_DAY,
                "tickets_closed_today": tickets['closed_today'],
                "tickets_updated_today": tickets['updated_today'],
                "active_tickets": tickets['active_tickets'],
                "overdue_tickets": tickets['overdue_count'],
                "velocity_closed": perf['velocity_closed'],
                "velocity_updated": perf['velocity_updated'],
                "adjusted_score": perf['adjusted_score'],
                "is_historical": True
            }
        }
        
        url = f"{DUST_API_BASE}/{document['document_id']}"
        
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code not in [200, 201]:
                debug_print(f"Failed to upload {tech_name}: {response.status_code}")
        except Exception as e:
            debug_print(f"Error uploading {tech_name}: {e}")

def process_single_day(date, graph_token, sdp_token):
    """Process meeting and performance data for a specific date"""
    
    if date.weekday() >= 5:
        return None
    
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        return None
    
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            email = resolve_technician_email(tech_name, info, graph_token)
            info['email'] = email
    
    tech_data = {}
    meeting_total = 0
    ticket_total = 0
    
    for tech_name, tech_info in technicians_info.items():
        tech_email = tech_info.get('email')
        
        meetings = get_tech_meetings(tech_email, graph_token, date)
        meeting_total += meetings['meeting_count']
        
        tickets = get_tech_ticket_metrics(tech_name, sdp_token, date)
        if not tickets:
            tickets = {'active_tickets': 0, 'closed_today': 0, 'updated_today': 0, 'overdue_count': 0}
        
        ticket_total += tickets['closed_today']
        
        performance = calculate_adjusted_performance(meetings, tickets)
        
        tech_data[tech_name] = {
            'email': tech_email,
            'meetings': meetings,
            'tickets': tickets,
            'performance': performance
        }
    
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    
    output = {}
    for tech, data in tech_data.items():
        output[tech] = {
            'date': date.isoformat(),
            'email': data.get('email'),
            'meetings': {
                'count': data['meetings']['meeting_count'],
                'hours': data['meetings']['total_hours'],
                'error': data['meetings'].get('error')
            },
            'tickets': data['tickets'],
            'performance': data['performance']
        }
    
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    if DUST_API_KEY and DUST_WORKSPACE_ID:
        upload_to_dust_historical(tech_data, date)
    
    print(f" ✓ {len(technicians_info)} techs | {meeting_total} meetings | {ticket_total} tickets closed")
    
    return tech_data

def run_backfill(days_back, graph_token, sdp_token):
    """Backfill historical performance data"""
    
    print("="*70)
    print(f"📅 HISTORICAL BACKFILL MODE")
    print(f"   Capturing last {days_back} calendar days")
    print("="*70)
    print()
    
    if days_back > 365:
        print("⚠️  Maximum backfill is 365 days. Using 365 instead.")
        days_back = 365
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print()
    
    checkpoint_file = 'backfill_checkpoint.json'
    processed_dates = set()
    
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file, 'r') as f:
            checkpoint = json.load(f)
            processed_dates = set(checkpoint.get('processed_dates', []))
            print(f"📋 Resuming: {len(processed_dates)} days already processed\n")
    
    processed_days = len(processed_dates)
    skipped_days = 0
    failed_days = []
    
    for days_ago in range(days_back, -1, -1):
        date = datetime.now() - timedelta(days=days_ago)
        date_str = date.strftime('%Y-%m-%d')
        
        if date_str in processed_dates:
            continue
        
        if date.weekday() >= 5:
            skipped_days += 1
            continue
        
        try:
            business_day_num = processed_days + 1
            print(f"{business_day_num} {date_str}", end=' ')
            
            result = process_single_day(date, graph_token, sdp_token)
            
            if result:
                processed_days += 1
                processed_dates.add(date_str)
                
                if processed_days % 10 == 0:
                    with open(checkpoint_file, 'w') as f:
                        json.dump({
                            'processed_dates': list(processed_dates),
                            'last_updated': datetime.now().isoformat()
                        }, f)
            else:
                skipped_days += 1
                print(" ⏭️  Skipped")
            
            if days_ago > 0:
                import time
                time.sleep(3)
                
                if processed_days % 50 == 0 and processed_days > 0:
                    print(f"  ⏸️  Cooling down...")
                    time.sleep(30)
        
        except Exception as e:
            print(f" ❌ Error: {str(e)[:50]}")
            failed_days.append(date_str)
            continue
    
    with open(checkpoint_file, 'w') as f:
        json.dump({
            'processed_dates': list(processed_dates),
            'completed': True,
            'last_updated': datetime.now().isoformat()
        }, f)
    
    print("\n" + "="*70)
    print(f"✅ BACKFILL COMPLETE")
    print(f"   Processed: {processed_days} business days")
    print(f"   Skipped: {skipped_days} days")
    if failed_days:
        print(f"   Failed: {len(failed_days)} days")
    print("="*70)
    
    if not failed_days and os.path.exists(checkpoint_file):
        os.remove(checkpoint_file)

def generate_performance_report(tech_data, date):
    """Generate formatted performance report"""
    
    print("\n" + "="*70)
    print(f"📊 TECHNICIAN PERFORMANCE REPORT - {date.strftime('%B %d, %Y')}")
    print("="*70)
    
    sorted_techs = sorted(tech_data.items(), key=lambda x: x[1]['performance']['adjusted_score'], reverse=True)
    
    for tech_name, data in sorted_techs:
        meetings = data['meetings']
        tickets = data['tickets']
        perf = data['performance']
        
        print(f"\n👤 {tech_name}")
        print(f"   📅 Meetings: {meetings['meeting_count']} ({meetings['total_hours']}h = {perf['meeting_percentage']}%)")
        print(f"   ⏰ Available: {perf['available_hours']}h")
        print(f"   🎫 Closed: {tickets['closed_today']} | Updated: {tickets['updated_today']}")
        print(f"   ⚡ Velocity: {perf['velocity_closed']} tickets/hour")
        print(f"   ⭐ Score: {perf['adjusted_score']}/100")
    
    print()

def save_metrics_to_file(tech_data, date):
    """Save daily metrics to JSON"""
    
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    
    output = {}
    for tech, data in tech_data.items():
        output[tech] = {
            'date': date.isoformat(),
            'email': data.get('email'),
            'meetings': {
                'count': data['meetings']['meeting_count'],
                'hours': data['meetings']['total_hours'],
                'error': data['meetings'].get('error')
            },
            'tickets': data['tickets'],
            'performance': data['performance']
        }
    
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"📁 Metrics saved to: {filename}")

def upload_to_dust(tech_data, date):
    """Upload performance metrics to Dust"""
    
    print("\n📤 Uploading performance data to Dust...")
    
    DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    date_str = date.strftime('%Y-%m-%d')
    
    uploaded = 0
    failed = 0
    
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
            "text": f"""# Technician Performance - {tech_name}
**Date:** {date.strftime('%B %d, %Y')}

## 📅 Calendar & Availability
- **Meetings:** {meetings['meeting_count']} meetings
- **Meeting Time:** {meetings['total_hours']} hours ({perf['meeting_percentage']}%)
- **Available:** {perf['available_hours']} hours

## 🎫 Ticket Activity
- **Closed:** {tickets['closed_today']}
- **Updated:** {tickets['updated_today']}
- **Active:** {tickets['active_tickets']}
- **Overdue:** {tickets['overdue_count']}

## ⚡ Performance
- **Velocity:** {perf['velocity_closed']} tickets/hour
- **Score:** {perf['adjusted_score']}/100
""",
            "structured_data": {
                "technician_name": tech_name,
                "date": date_str,
                "timestamp_ms": int(date.timestamp() * 1000),
                "meeting_count": meetings['meeting_count'],
                "meeting_hours": meetings['total_hours'],
                "meeting_percentage": perf['meeting_percentage'],
                "available_hours": perf['available_hours'],
                "tickets_closed_today": tickets['closed_today'],
                "tickets_updated_today": tickets['updated_today'],
                "active_tickets": tickets['active_tickets'],
                "overdue_tickets": tickets['overdue_count'],
                "velocity_closed": perf['velocity_closed'],
                "velocity_updated": perf['velocity_updated'],
                "adjusted_score": perf['adjusted_score']
            }
        }
        
        url = f"{DUST_API_BASE}/{document['document_id']}"
        
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code in [200, 201]:
                uploaded += 1
            else:
                failed += 1
        except:
            failed += 1
    
    print(f"✓ Uploaded: {uploaded} | Failed: {failed}")

def main():
    global DEBUG
    args = parser.parse_args()
    
    if args.debug:
        DEBUG = True
    
    print("="*70)
    print("🚀 TECHNICIAN MEETING LOAD & PERFORMANCE TRACKER")
    print("   ✨ FIXED v3: Accurate counts + meeting hour fix + debug mode")
    print("="*70)
    print()
    
    graph_token = get_graph_token()
    sdp_token = get_sdp_token()
    
    if args.backfill:
        if args.backfill > 365:
            args.backfill = 365
        run_backfill(args.backfill, graph_token, sdp_token)
        return
    
    if args.date:
        try:
            date = datetime.strptime(args.date, '%Y-%m-%d')
            print(f"📅 Processing: {date.strftime('%Y-%m-%d')}")
            result = process_single_day(date, graph_token, sdp_token)
            if result:
                generate_performance_report(result, date)
            return
        except ValueError:
            print("❌ Invalid date format. Use YYYY-MM-DD")
            return
    
    # Default: Process yesterday (more reliable than today)
    if args.today:
        target_date = datetime.now()
        print("📅 Processing today's data (may be incomplete)...")
    else:
        target_date = datetime.now() - timedelta(days=1)
        print(f"📅 Processing yesterday's data ({target_date.strftime('%Y-%m-%d')}) for complete metrics...")
        print("    (Use --today flag to process today instead)\n")
    
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        print("\n❌ No technicians found!")
        return
    
    print("\n📧 Resolving emails...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            email = resolve_technician_email(tech_name, info, graph_token)
            info['email'] = email
    
    tech_data = {}
    
    print(f"\n📊 Analyzing {len(technicians_info)} technicians...\n")
    
    for tech_name, tech_info in technicians_info.items():
        print(f"Processing {tech_name}...")
        
        tech_email = tech_info.get('email')
        meetings = get_tech_meetings(tech_email, graph_token, target_date)
        
        print(f"  ✓ Meetings: {meetings['meeting_count']} ({meetings['total_hours']}h)")
        
        tickets = get_tech_ticket_metrics(tech_name, sdp_token, target_date)
        if tickets:
            print(f"  ✓ Tickets: {tickets['closed_today']} closed")
        else:
            tickets = {'active_tickets': 0, 'closed_today': 0, 'updated_today': 0, 'overdue_count': 0}
        
        performance = calculate_adjusted_performance(meetings, tickets)
        print(f"  ✓ Score: {performance['adjusted_score']}/100")
        
        tech_data[tech_name] = {
            'email': tech_email,
            'meetings': meetings,
            'tickets': tickets,
            'performance': performance
        }
        
        print()
    
    generate_performance_report(tech_data, target_date)
    save_metrics_to_file(tech_data, target_date)
    
    if DUST_API_KEY and DUST_WORKSPACE_ID:
        upload_to_dust(tech_data, target_date)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
