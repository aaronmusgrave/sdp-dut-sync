#!/usr/bin/env python3
"""
Track technician meeting load and correlate with ticket performance
AUTO-DISCOVERS technicians from ServiceDesk ticket assignments
PRIVACY MODE: Meeting titles hidden
SUPPORTS: 365-day historical backfill
"""

import os
import sys
import argparse
import requests
import json
import time
from datetime import datetime, timedelta
from collections import defaultdict

# Parse arguments first
parser = argparse.ArgumentParser(description='Track technician meeting load and performance')
parser.add_argument('--backfill', type=int, metavar='DAYS', 
                    help='Backfill historical data for the last N days (max 365)')
parser.add_argument('--date', type=str, metavar='YYYY-MM-DD',
                    help='Process specific date (format: 2025-11-25)')

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

# Email domain for your organization
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'motorad.com')

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
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        print("✓ ServiceDesk token obtained")
        return response.json()['access_token']
    else:
        raise Exception(f"Failed to get SDP token: {response.status_code}")

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
    
    if technicians:
        print("\n📋 Active Technicians Found:")
        for tech_name, info in sorted(technicians.items(), key=lambda x: x[1]['ticket_count'], reverse=True):
            email_status = "✓" if info['email'] else "⚠️"
            print(f"  {email_status} {tech_name}: {info['ticket_count']} active tickets | Email: {info['email'] or 'Not found'}")
    
    return technicians

def resolve_technician_email(tech_name, tech_info, graph_token):
    """Resolve technician email address"""
    
    if tech_info.get('email'):
        return tech_info['email']
    
    print(f"  🔍 Resolving email for {tech_name}...")
    
    # Try Azure AD
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
                print(f"    ✓ Found in Azure AD: {email}")
                return email
    except Exception as e:
        print(f"    ⚠️ Azure AD lookup failed: {e}")
    
    # Fallback: Generate email
    name_parts = tech_name.lower().split()
    if len(name_parts) >= 2:
        generated_email = f"{name_parts[0]}.{name_parts[-1]}@{EMAIL_DOMAIN}"
        print(f"    ⚠️ Generated email: {generated_email}")
        return generated_email
    
    print(f"    ❌ Could not resolve email for {tech_name}")
    return None

def get_tech_meetings(email, graph_token, date=None):
    """Get meetings for a technician on a specific date (privacy mode)"""
    
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
                
                response_status = event.get('responseStatus', {}).get('response', 'none')
                if response_status == 'declined':
                    continue
                
                start = datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00'))
                end = datetime.fromisoformat(event['end']['dateTime'].replace('Z', '+00:00'))
                duration_minutes = (end - start).total_seconds() / 60
                
                valid_meetings.append({
                    'subject': 'Busy',  # Privacy mode
                    'start': start,
                    'end': end,
                    'duration_minutes': duration_minutes
                })
                
                total_minutes += duration_minutes
            
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
        return {'meeting_count': 0, 'total_hours': 0, 'meetings': []}

def get_tech_ticket_metrics(tech_name, sdp_token, date=None):
    """Get ticket metrics for a technician on a specific date"""
    
    if date is None:
        date = datetime.now()
    
    start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    start_ms = int(start_of_day.timestamp() * 1000)
    end_ms = int(end_of_day.timestamp() * 1000)
    
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
                    "field": "technician.name",
                    "condition": "is",
                    "value": tech_name
                }
            ]
        }
    }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_API_BASE}/requests"
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            tickets = result.get('requests', [])
            
            metrics = {
                'active_tickets': 0,
                'closed_today': 0,
                'updated_today': 0,
                'overdue_count': 0
            }
            
            for ticket in tickets:
                status = ticket.get('status', {}).get('name', '').lower()
                
                if status in ['open', 'in progress', 'on hold']:
                    metrics['active_tickets'] += 1
                
                resolved_time = ticket.get('resolved_time', {})
                if isinstance(resolved_time, dict):
                    resolved_ms = resolved_time.get('value')
                    if resolved_ms and start_ms <= resolved_ms <= end_ms:
                        metrics['closed_today'] += 1
                
                updated_time = ticket.get('last_updated_time', {})
                if isinstance(updated_time, dict):
                    updated_ms = updated_time.get('value')
                    if updated_ms and start_ms <= updated_ms <= end_ms:
                        metrics['updated_today'] += 1
                
                if ticket.get('is_overdue'):
                    metrics['overdue_count'] += 1
            
            return metrics
        else:
            return None
    
    except Exception as e:
        return None

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
    
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        return
    
    DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/tech-performance/documents"
    
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
🔒 **Privacy Mode:** Meeting details hidden for confidentiality

## 📅 Calendar & Availability
- **Meetings:** {meetings['meeting_count']} meetings
- **Meeting Time:** {meetings['total_hours']} hours ({perf['meeting_percentage']}% of workday)
- **Available for Tickets:** {perf['available_hours']} hours

## 🎫 Ticket Activity
- **Tickets Closed:** {tickets['closed_today']}
- **Tickets Updated:** {tickets['updated_today']}
- **Active Workload:** {tickets['active_tickets']} tickets
- **Overdue Tickets:** {tickets['overdue_count']}

## ⚡ Performance Metrics
- **Velocity (Closed):** {perf['velocity_closed']} tickets per available hour
- **Velocity (Updated):** {perf['velocity_updated']} tickets per available hour
- **Adjusted Performance Score:** {perf['adjusted_score']}/100
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
                print(f"    ⚠️ Failed to upload {tech_name}")
        except:
            pass

def process_single_day(date, graph_token, sdp_token, show_progress=True):
    """Process meeting and performance data for a specific date"""
    
    date_str = date.strftime('%Y-%m-%d')
    day_name = date.strftime('%A')
    
    # Skip weekends
    if date.weekday() >= 5:
        if show_progress:
            print(f"  ⏭️  Skipping weekend: {date_str} ({day_name})")
        return None
    
    if show_progress:
        print(f"📅 Processing: {date_str} ({day_name})")
    
    # Discover technicians
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        if show_progress:
            print(f"  ❌ No technicians found")
        return None
    
    # Resolve emails
    if show_progress:
        print("\n📧 Resolving email addresses...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            email = resolve_technician_email(tech_name, info, graph_token)
            info['email'] = email
    
    # Track all technicians for this date
    tech_data = {}
    
    if show_progress:
        print(f"\n📊 Analyzing {len(technicians_info)} technicians...")
    
    for tech_name, tech_info in technicians_info.items():
        tech_email = tech_info.get('email')
        
        meetings = get_tech_meetings(tech_email, graph_token, date)
        tickets = get_tech_ticket_metrics(tech_name, sdp_token, date)
        
        if not tickets:
            tickets = {'active_tickets': 0, 'closed_today': 0, 'updated_today': 0, 'overdue_count': 0}
        
        performance = calculate_adjusted_performance(meetings, tickets)
        
        tech_data[tech_name] = {
            'email': tech_email,
            'meetings': meetings,
            'tickets': tickets,
            'performance': performance
        }
        
        if show_progress:
            print(f"  ✓ {tech_name}: {meetings['meeting_count']} meetings, {tickets['closed_today']} closed")
    
    # Save to dated file
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
    
    if show_progress:
        print(f"  ✓ Saved: {filename}")
    
    # Upload to Dust
    upload_to_dust_historical(tech_data, date)
    
    return tech_data

def run_backfill(days_back, graph_token, sdp_token):
    """Backfill historical performance data with robust error handling"""
    
    print("="*70)
    print(f"📅 HISTORICAL BACKFILL MODE")
    print(f"   Capturing last {days_back} calendar days")
    print(f"   (Weekends will be skipped)")
    print("="*70)
    print()
    
    if days_back > 365:
        print("⚠️  Maximum backfill is 365 days. Using 365 instead.")
        days_back = 365
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Estimated business days: ~{days_back * 5 // 7}")
    print()
    
    # Load checkpoint if exists
    checkpoint_file = 'backfill_checkpoint.json'
    processed_dates = set()
    
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file, 'r') as f:
            checkpoint = json.load(f)
            processed_dates = set(checkpoint.get('processed_dates', []))
            print(f"📋 Resuming: {len(processed_dates)} days already processed")
            print()
    
    processed_days = len(processed_dates)
    skipped_days = 0
    failed_days = []
    
    start_time = time.time()
    
    # Process each day
    for days_ago in range(days_back, -1, -1):
        date = datetime.now() - timedelta(days=days_ago)
        date_str = date.strftime('%Y-%m-%d')
        
        # Skip if already processed
        if date_str in processed_dates:
            continue
        
        # Skip weekends
        if date.weekday() >= 5:
            skipped_days += 1
            continue
        
        # Process day
        try:
            business_days_processed = processed_days - len([d for d in processed_dates if datetime.strptime(d, '%Y-%m-%d').weekday() >= 5])
            total_business_days = days_back * 5 // 7
            
            print(f"\n[{business_days_processed + 1}/{total_business_days}] {date_str}")
            
            result = process_single_day(date, graph_token, sdp_token, show_progress=False)
            
            if result:
                processed_days += 1
                processed_dates.add(date_str)
                
                # Progress indicator
                total_techs = len(result)
                total_meetings = sum(d['meetings']['meeting_count'] for d in result.values())
                total_closed = sum(d['tickets']['closed_today'] for d in result.values())
                print(f"  ✓ {total_techs} techs | {total_meetings} meetings | {total_closed} tickets closed")
                
                # Save checkpoint every 10 days
                if processed_days % 10 == 0:
                    with open(checkpoint_file, 'w') as f:
                        json.dump({
                            'processed_dates': list(processed_dates),
                            'last_updated': datetime.now().isoformat()
                        }, f)
                    elapsed = time.time() - start_time
                    remaining = (elapsed / processed_days) * (total_business_days - business_days_processed)
                    print(f"  💾 Checkpoint: {processed_days} days | ETA: {remaining/60:.0f} min")
            else:
                skipped_days += 1
            
            # RATE LIMITING
            if days_ago > 0:
                time.sleep(3)
                
                # Extra cooldown every 50 days
                if processed_days % 50 == 0 and processed_days > 0:
                    print(f"  ⏸️  Cooling down (30s)...")
                    time.sleep(30)
        
        except Exception as e:
            print(f"  ❌ Error: {str(e)[:100]}")
            failed_days.append(date_str)
            continue
    
    # Save final checkpoint
    with open(checkpoint_file, 'w') as f:
        json.dump({
            'processed_dates': list(processed_dates),
            'completed': True,
            'last_updated': datetime.now().isoformat()
        }, f)
    
    # Final summary
    total_time = time.time() - start_time
    
    print("\n" + "="*70)
    print(f"✅ BACKFILL COMPLETE")
    print(f"   Processed: {processed_days} business days")
    print(f"   Skipped: {skipped_days} days (weekends)")
    if failed_days:
        print(f"   Failed: {len(failed_days)} days")
        print(f"   Failed dates: {', '.join(failed_days[:5])}" + 
              (f" and {len(failed_days)-5} more" if len(failed_days) > 5 else ""))
    print(f"   Files created: {processed_days} JSON files")
    print(f"   Total time: {total_time/60:.1f} minutes")
    print("="*70)
    
    # Cleanup checkpoint
    if not failed_days and os.path.exists(checkpoint_file):
        os.remove(checkpoint_file)
        print("\n✓ Checkpoint file removed (backfill complete)")

def upload_to_dust(tech_data):
    """Upload today's performance data to Dust"""
    
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        print("\n⚠️  Dust credentials not configured - skipping upload")
        return
    
    print("\n📤 Uploading performance data to Dust...")
    
    upload_to_dust_historical(tech_data, datetime.now())
    
    print("✓ Performance data sync to Dust complete")

def generate_performance_report(tech_data):
    """Generate formatted performance report"""
    
    print("\n" + "="*70)
    print(f"📊 TECHNICIAN PERFORMANCE REPORT - {datetime.now().strftime('%B %d, %Y')}")
    print("="*70)
    print(f"🔒 Privacy Mode: Meeting details hidden")
    
    sorted_techs = sorted(tech_data.items(), key=lambda x: x[1]['performance']['adjusted_score'], reverse=True)
    
    for tech_name, data in sorted_techs:
        meetings = data['meetings']
        tickets = data['tickets']
        perf = data['performance']
        
        print(f"\n👤 {tech_name}")
        
        if meetings.get('error') == 'user_not_found':
            print(f"   ⚠️  Calendar: Not accessible")
            print(f"   📧 Email: {data.get('email', 'unknown')}")
        else:
            print(f"   📅 Meetings: {meetings['meeting_count']} ({meetings['total_hours']}h = {perf['meeting_percentage']}% of day)")
        
        print(f"   ⏰ Available: {perf['available_hours']}h")
        print(f"   🎫 Closed: {tickets['closed_today']} | Updated: {tickets['updated_today']}")
        print(f"   📊 Active: {tickets['active_tickets']} | Overdue: {tickets['overdue_count']}")
        print(f"   ⚡ Velocity: {perf['velocity_closed']} tickets/hour")
        print(f"   ⭐ Score: {perf['adjusted_score']}/100")
        
        if meetings['meeting_count'] > 0 and not meetings.get('error'):
            print(f"   📋 Calendar:")
            for idx, meeting in enumerate(meetings['meetings'][:3], 1):
                start_time = meeting['start'].strftime('%I:%M %p')
                print(f"      • Busy: {meeting['duration_minutes']:.0f} min at {start_time}")
    
    print(f"\n{'='*70}")
    print("💡 INSIGHTS")
    print("="*70)
    
    accessible_techs = [t for t, d in tech_data.items() if not d['meetings'].get('error')]
    if accessible_techs:
        high_meeting = [t for t in accessible_techs if tech_data[t]['performance']['meeting_percentage'] > 40]
        if high_meeting:
            print(f"• High meeting load (>40%): {', '.join(high_meeting)}")
    
    top = sorted_techs[0]
    print(f"• Top performer: {top[0]} ({top[1]['performance']['adjusted_score']}/100)")
    
    velocities = [(t, d['performance']['velocity_closed']) for t, d in tech_data.items()]
    top_vel = max(velocities, key=lambda x: x[1])
    print(f"• Highest velocity: {top_vel[0]} ({top_vel[1]:.2f} tickets/hour)")
    
    print()

def save_metrics_to_file(tech_data):
    """Save daily metrics to JSON"""
    
    filename = f"tech_performance_{datetime.now().strftime('%Y%m%d')}.json"
    
    output = {}
    for tech, data in tech_data.items():
        output[tech] = {
            'date': datetime.now().isoformat(),
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

def main():
    args = parser.parse_args()
    
    print("="*70)
    print("🚀 TECHNICIAN MEETING LOAD & PERFORMANCE TRACKER")
    print("   (AUTO-DISCOVERY + PRIVACY MODE + 365-DAY BACKFILL)")
    print("="*70)
    print()
    
    # Get tokens
    graph_token = get_graph_token()
    sdp_token = get_sdp_token()
    
    # BACKFILL MODE
    if args.backfill:
        run_backfill(args.backfill, graph_token, sdp_token)
        return
    
    # SPECIFIC DATE MODE
    if args.date:
        try:
            date = datetime.strptime(args.date, '%Y-%m-%d')
            print(f"📅 Processing specific date: {date.strftime('%Y-%m-%d')}\n")
            process_single_day(date, graph_token, sdp_token)
            return
        except ValueError:
            print("❌ Invalid date format. Use YYYY-MM-DD")
            return
    
    # DEFAULT MODE - TODAY
    print("📅 Processing today's data...\n")
    
    technicians_info = discover_active_technicians(sdp_token)
    
    if not technicians_info:
        print("\n❌ No active technicians found!")
        return
    
    print("\n📧 Resolving email addresses...")
    for tech_name, info in technicians_info.items():
        if not info.get('email'):
            email = resolve_technician_email(tech_name, info, graph_token)
            info['email'] = email
    
    tech_data = {}
    
    print(f"\n📊 Analyzing {len(technicians_info)} technicians...\n")
    
    for tech_name, tech_info in technicians_info.items():
        print(f"Processing {tech_name}...")
        
        tech_email = tech_info.get('email')
        meetings = get_tech_meetings(tech_email, graph_token)
        
        if meetings.get('error'):
            print(f"  ⚠️  Calendar: {meetings.get('error')}")
        else:
            print(f"  ✓ Meetings: {meetings['meeting_count']} ({meetings['total_hours']}h)")
        
        tickets = get_tech_ticket_metrics(tech_name, sdp_token)
        if tickets:
            print(f"  ✓ Tickets: {tickets['closed_today']} closed, {tickets['active_tickets']} active")
        else:
            print(f"  ⚠️  Could not fetch tickets")
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
    
    generate_performance_report(tech_data)
    save_metrics_to_file(tech_data)
    upload_to_dust(tech_data)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
