#!/usr/bin/env python3
"""
Performance Data Backfill Script v18.0.0 - COMPREHENSIVE PERFORMANCE FILE
Tracks meeting load, Azure AD profile, closed ticket DETAILS (number, SLA, priority),
and role-weighted performance scoring.

v18.0.0 Changes (from v17.1.4):
- MAJOR: count_closed_tickets() replaced with get_closed_tickets_detail()
  - Returns full ticket objects (not just a count)
  - Each ticket includes: ID/number, subject, priority, category, SLA name,
    SLA violated flag, created_time, completed_time, resolution duration (mins),
    due_by_time, first_response_due_by_time
- MAJOR: Dust document now includes per-ticket table with SLA breakdown
- MAJOR: structured_data includes tickets[] array for downstream querying
- MAJOR: SLA summary stats added (total violated, met, avg resolution time)
- Preserved all v17.1.4 fixes: completed_time, pagination, --force flag,
  newest-to-oldest processing, status.name='Closed' filter
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

# ─────────────────────────────────────────────
# Credential helpers
# ─────────────────────────────────────────────

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
    date_str = date_str.replace('\\', '').strip()
    try:
        parts = date_str.split('-')
        if len(parts) == 3:
            dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            return dt.strftime('%Y-%m-%d')
    except Exception:
        pass
    return date_str


# Load credentials
TENANT_ID      = clean_credential(os.environ.get('AZURE_TENANT_ID'))
CLIENT_ID      = clean_credential(os.environ.get('AZURE_CLIENT_ID'))
CLIENT_SECRET  = clean_credential(os.environ.get('AZURE_CLIENT_SECRET'))

SDP_DOMAIN        = clean_credential(os.environ.get('SDP_DOMAIN'))
SDP_CLIENT_ID     = clean_credential(os.environ.get('SDP_CLIENT_ID'))
SDP_CLIENT_SECRET = clean_credential(os.environ.get('SDP_CLIENT_SECRET'))
SDP_REFRESH_TOKEN = clean_credential(os.environ.get('SDP_REFRESH_TOKEN'))

DUST_API_KEY      = clean_credential(os.environ.get('DUST_API_KEY'))
DUST_WORKSPACE_ID = clean_credential(os.environ.get('DUST_WORKSPACE_ID'))

SDP_API_BASE   = f"https://{SDP_DOMAIN}/api/v3"
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Token management
# ─────────────────────────────────────────────

def get_graph_token():
    """Get Microsoft Graph token with caching and rate-limit handling"""
    if TOKEN_CACHE['graph_token'] and TOKEN_CACHE['graph_expires']:
        if datetime.now() < TOKEN_CACHE['graph_expires']:
            return TOKEN_CACHE['graph_token']

    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope':         'https://graph.microsoft.com/.default',
        'grant_type':    'client_credentials'
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, data=data, timeout=30)
            if resp.status_code == 200:
                token = resp.json()['access_token']
                TOKEN_CACHE['graph_token']   = token
                TOKEN_CACHE['graph_expires'] = datetime.now() + timedelta(minutes=45)
                return token
            elif resp.status_code == 429:
                wait = 2 ** attempt * 30
                logger.warning(f"Graph rate-limited. Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise Exception(f"Graph token failed: {resp.text[:200]}")
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 10)
            else:
                raise Exception(f"Graph token error: {e}")

    raise Exception("Failed to get Graph token after max retries")


def get_sdp_token():
    """Get ServiceDesk token with caching and rate-limit handling"""
    if TOKEN_CACHE['sdp_token'] and TOKEN_CACHE['sdp_expires']:
        if datetime.now() < TOKEN_CACHE['sdp_expires']:
            return TOKEN_CACHE['sdp_token']

    payload = {
        'client_id':     SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type':    'refresh_token'
    }

    for attempt in range(4):
        try:
            resp = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=30)
            if resp.status_code == 200:
                token = resp.json()['access_token']
                TOKEN_CACHE['sdp_token']   = token
                TOKEN_CACHE['sdp_expires'] = datetime.now() + timedelta(minutes=45)
                return token
            elif resp.status_code == 429 or 'too many requests' in resp.text.lower():
                wait = 2 ** attempt * 30
                logger.warning(f"SDP rate-limited. Waiting {wait}s... ({attempt+1}/4)")
                time.sleep(wait)
            else:
                raise Exception(f"SDP token failed: {resp.text[:300]}")
        except requests.exceptions.RequestException as e:
            if attempt < 3:
                time.sleep(2 ** attempt * 10)
            else:
                raise Exception(f"SDP token error: {e}")

    raise Exception("Failed to get SDP token after max retries")


# ─────────────────────────────────────────────
# Azure AD profile
# ─────────────────────────────────────────────

def get_azure_profile(email, graph_token):
    """Fetch enhanced Azure AD profile including manager"""
    if not email:
        return None

    headers = {
        'Authorization': f'Bearer {graph_token}',
        'ConsistencyLevel': 'eventual'
    }

    try:
        resp = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{email}",
            headers=headers, timeout=30
        )
        if resp.status_code != 200:
            return None
        profile = resp.json()

        manager_name = manager_email = None
        try:
            m = requests.get(
                f"https://graph.microsoft.com/v1.0/users/{email}/manager",
                headers=headers, timeout=30
            )
            if m.status_code == 200:
                md = m.json()
                manager_name  = md.get('displayName')
                manager_email = md.get('mail')
        except Exception:
            pass

        return {
            'job_title':       profile.get('jobTitle'),
            'department':      profile.get('department'),
            'office_location': profile.get('officeLocation'),
            'city':            profile.get('city'),
            'state':           profile.get('state'),
            'country':         profile.get('country'),
            'manager_name':    manager_name,
            'manager_email':   manager_email,
            'account_enabled': profile.get('accountEnabled'),
            'display_name':    profile.get('displayName'),
            'company_name':    profile.get('companyName'),
            'account_created': profile.get('createdDateTime'),
            'mobile_phone':    profile.get('mobilePhone'),
            'business_phones': profile.get('businessPhones', [])
        }
    except Exception as e:
        logger.warning(f"Error fetching Azure profile for {email}: {e}")
    return None


# ─────────────────────────────────────────────
# Technician discovery
# ─────────────────────────────────────────────

def discover_technicians(sdp_token, graph_token):
    """Find active technicians from open tickets and enrich with Azure AD profiles"""
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
    resp = requests.get(
        f"{SDP_API_BASE}/requests",
        headers=headers,
        params={'input_data': json.dumps(list_info)},
        timeout=30
    )
    if resp.status_code == 200:
        for ticket in resp.json().get('requests', []):
            tech = ticket.get('technician')
            if tech and isinstance(tech, dict):
                name  = tech.get('name')
                email = tech.get('email_id')
                if name and name != 'Unassigned' and name not in technicians:
                    az = get_azure_profile(email, graph_token)
                    technicians[name] = {
                        'email':           email,
                        'job_title':       az.get('job_title')       if az else None,
                        'department':      az.get('department')      if az else None,
                        'office_location': az.get('office_location') if az else None,
                        'city':            az.get('city')            if az else None,
                        'state':           az.get('state')           if az else None,
                        'country':         az.get('country')         if az else None,
                        'manager_name':    az.get('manager_name')    if az else None,
                        'manager_email':   az.get('manager_email')   if az else None,
                        'account_enabled': az.get('account_enabled') if az else None,
                        'display_name':    az.get('display_name')    if az else None,
                        'company_name':    az.get('company_name')    if az else None,
                        'account_created': az.get('account_created') if az else None,
                    }
    return technicians


# ─────────────────────────────────────────────
# Calendar / meetings
# ─────────────────────────────────────────────

def fix_datetime_str(dt_str):
    """Normalise Microsoft Graph datetime strings for fromisoformat()"""
    dt_str = dt_str.replace('Z', '+00:00')
    return re.sub(r'\.(\d{6})\d+', r'.\1', dt_str)


def get_meetings(email, graph_token, date):
    """Return meeting count and total hours for the full 24-hour day"""
    if not email:
        return {'count': 0, 'hours': 0}

    start_time = date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end_time   = date.replace(hour=23, minute=59, second=59, microsecond=0)

    headers = {
        'Authorization': f'Bearer {graph_token}',
        'Prefer': 'outlook.timezone="Central Standard Time"'
    }
    params = {
        'startDateTime': start_time.isoformat(),
        'endDateTime':   end_time.isoformat(),
        '\$select':      'isAllDay,isCancelled,responseStatus,start,end,subject'
    }

    try:
        resp = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{email}/calendar/calendarView",
            headers=headers, params=params, timeout=30
        )
        if resp.status_code == 200:
            total_minutes = 0
            count = 0
            for event in resp.json().get('value', []):
                if event.get('isCancelled'):
                    continue
                if event.get('isAllDay'):
                    duration_minutes = 8 * 60
                else:
                    s = datetime.fromisoformat(fix_datetime_str(event['start']['dateTime']))
                    e = datetime.fromisoformat(fix_datetime_str(event['end']['dateTime']))
                    duration_minutes = (e - s).total_seconds() / 60
                total_minutes += min(duration_minutes, 8 * 60)
                count += 1
            return {'count': count, 'hours': round(min(total_minutes, 24 * 60) / 60, 2)}
    except Exception as ex:
        logger.warning(f"Error fetching meetings for {email}: {ex}")
    return {'count': 0, 'hours': 0}


# ─────────────────────────────────────────────
# Closed ticket DETAIL retrieval  (v18.0.0 core change)
# ─────────────────────────────────────────────

def _epoch_ms_to_iso(ms):
    """Convert ServiceDesk epoch-milliseconds to ISO-8601 string (or None)"""
    if not ms:
        return None
    try:
        return datetime.utcfromtimestamp(int(ms) / 1000).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return None


def _resolution_minutes(created_ms, completed_ms):
    """Return resolution duration in whole minutes, or None"""
    if not created_ms or not completed_ms:
        return None
    try:
        return int((int(completed_ms) - int(created_ms)) / 60000)
    except Exception:
        return None


def get_closed_tickets_detail(tech_email, sdp_token, target_date):
    """
    Return a list of ticket detail dicts for every ticket CLOSED on target_date
    by the given technician.

    Each dict contains:
        ticket_id              - SDP request ID (e.g. "1042")
        subject                - ticket title
        priority               - e.g. "High"
        category               - e.g. "Hardware"
        sla_name               - SLA policy name (e.g. "Standard SLA")
        sla_violated           - bool: True if SLA was breached
        created_time_iso       - ISO-8601 creation timestamp
        completed_time_iso     - ISO-8601 closure timestamp
        due_by_time_iso        - ISO-8601 SLA due timestamp (may be None)
        first_response_due_iso - ISO-8601 first-response SLA due (may be None)
        resolution_minutes     - integer minutes from creation to closure (or None)

    v18.0.0: Full pagination; uses completed_time + status.name='Closed' filter.
    """
    if not tech_email:
        return []

    start_of_day = target_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end_of_day   = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
    start_ms     = int(start_of_day.timestamp() * 1000)
    end_ms       = int(end_of_day.timestamp()   * 1000)

    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }

    all_tickets  = []
    start_index  = 1
    row_count    = 100

    while True:
        list_info = {
            "list_info": {
                "row_count":    row_count,
                "start_index":  start_index,
                "search_criteria": [
                    {
                        "field":     "technician.email_id",
                        "condition": "is",
                        "values":    [tech_email]
                    },
                    {
                        "field":            "completed_time",
                        "condition":        "between",
                        "logical_operator": "AND",
                        "values":           [start_ms, end_ms]
                    },
                    {
                        "field":            "status.name",
                        "condition":        "is",
                        "logical_operator": "AND",
                        "values":           ["Closed"]
                    }
                ]
            }
        }

        page_tickets = []
        success      = False

        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{SDP_API_BASE}/requests",
                    headers=headers,
                    params={'input_data': json.dumps(list_info)},
                    timeout=30
                )
                if resp.status_code == 200:
                    page_tickets = resp.json().get('requests', [])
                    success = True
                    break
                elif resp.status_code == 429:
                    wait = 2 ** attempt * 5
                    logger.warning(f"Rate-limited fetching ticket details. Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    logger.warning(f"API error {resp.status_code} fetching ticket details: {resp.text[:200]}")
                    if attempt < 2:
                        time.sleep(2)
            except Exception as ex:
                logger.warning(f"Exception fetching ticket detail page (attempt {attempt+1}): {ex}")
                if attempt < 2:
                    time.sleep(2)

        if not success:
            logger.error(f"Failed to fetch ticket detail page for {tech_email} starting at index {start_index}")
            break

        # Parse each ticket on this page into a clean dict
        for t in page_tickets:
            ticket_id   = str(t.get('id', ''))
            subject     = t.get('subject', 'N/A')
            priority    = (t.get('priority') or {}).get('name') or 'N/A'
            category    = (t.get('category') or {}).get('name') or 'N/A'

            # SLA fields
            sla_obj      = t.get('sla') or {}
            sla_name     = sla_obj.get('name') or 'N/A'
            sla_violated = bool(t.get('is_overdue', False))

            # Timestamps (epoch ms in SDP)
            created_ms        = (t.get('created_time')               or {}).get('value')
            completed_ms      = (t.get('completed_time')             or {}).get('value')
            due_by_ms         = (t.get('due_by_time')                or {}).get('value')
            first_response_ms = (t.get('first_response_due_by_time') or {}).get('value')

            all_tickets.append({
                'ticket_id':              ticket_id,
                'subject':                subject,
                'priority':               priority,
                'category':               category,
                'sla_name':               sla_name,
                'sla_violated':           sla_violated,
                'created_time_iso':       _epoch_ms_to_iso(created_ms),
                'completed_time_iso':     _epoch_ms_to_iso(completed_ms),
                'due_by_time_iso':        _epoch_ms_to_iso(due_by_ms),
                'first_response_due_iso': _epoch_ms_to_iso(first_response_ms),
                'resolution_minutes':     _resolution_minutes(created_ms, completed_ms)
            })

        if len(page_tickets) < row_count:
            break

        start_index += row_count

    return all_tickets


def build_sla_summary(tickets):
    """
    Aggregate SLA statistics across a list of ticket detail dicts.
    """
    if not tickets:
        return {
            'total': 0, 'sla_met': 0, 'sla_violated': 0,
            'sla_met_pct': 0.0,
            'avg_resolution_minutes': None,
            'min_resolution_minutes': None,
            'max_resolution_minutes': None
        }

    total    = len(tickets)
    violated = sum(1 for t in tickets if t['sla_violated'])
    met      = total - violated

    res_times = [t['resolution_minutes'] for t in tickets if t['resolution_minutes'] is not None]
    avg_res   = round(sum(res_times) / len(res_times)) if res_times else None
    min_res   = min(res_times) if res_times else None
    max_res   = max(res_times) if res_times else None

    return {
        'total':                  total,
        'sla_met':                met,
        'sla_violated':           violated,
        'sla_met_pct':            round(met / total * 100, 1) if total else 0.0,
        'avg_resolution_minutes': avg_res,
        'min_resolution_minutes': min_res,
        'max_resolution_minutes': max_res
    }


# ─────────────────────────────────────────────
# Performance scoring
# ─────────────────────────────────────────────

def calculate_performance(meetings, closed_ticket_count, job_title=None):
    """Role-weighted performance scoring."""
    meeting_hours   = meetings['hours']
    available_hours = max(0.5, WORK_HOURS_PER_DAY - meeting_hours)

    role_expectations = {
        'director':   {'multiplier': 20, 'expected_tickets_per_day': 3,  'normal_meeting_hours': (4, 6),  'title_keywords': ['director', 'application manager']},
        'manager':    {'multiplier': 15, 'expected_tickets_per_day': 5,  'normal_meeting_hours': (3, 5),  'title_keywords': ['manager']},
        'senior':     {'multiplier': 12, 'expected_tickets_per_day': 6,  'normal_meeting_hours': (1, 3),  'title_keywords': ['senior']},
        'engineer':   {'multiplier': 10, 'expected_tickets_per_day': 6,  'normal_meeting_hours': (0, 2),  'title_keywords': ['engineer', 'technician']},
        'specialist': {'multiplier':  8, 'expected_tickets_per_day': 8,  'normal_meeting_hours': (0, 1),  'title_keywords': ['specialist', 'analyst']},
    }

    role             = 'engineer'
    multiplier       = 10
    expected_tickets = 10
    normal_mtg_hours = (0, 2)

    if job_title:
        jt = job_title.lower()
        for rk, rd in role_expectations.items():
            if any(kw in jt for kw in rd['title_keywords']):
                role             = rk
                multiplier       = rd['multiplier']
                expected_tickets = rd['expected_tickets_per_day']
                normal_mtg_hours = rd['normal_meeting_hours']
                break

    mtg_min, mtg_max = normal_mtg_hours
    if meeting_hours <= mtg_max:
        meeting_impact = 0
    else:
        excess = meeting_hours - mtg_max
        caps = {
            'specialist': (0.15, 0.50), 'engineer': (0.12, 0.40),
            'senior':     (0.10, 0.35), 'manager':  (0.08, 0.25),
            'director':   (0.05, 0.15)
        }
        rate, cap = caps.get(role, (0.12, 0.40))
        meeting_impact = min(excess * rate, cap)

    if closed_ticket_count == 0:
        ticket_score = 0.0
    else:
        ratio           = closed_ticket_count / expected_tickets
        effective_ratio = ratio * (1 - meeting_impact)
        ticket_score    = min(effective_ratio * 100, 150)

    weighted_score = ticket_score * (multiplier / 10)

    if   weighted_score >= 100: rating = "Excellent"
    elif weighted_score >= 80:  rating = "Good"
    elif weighted_score >= 60:  rating = "Fair"
    elif weighted_score >= 40:  rating = "Below Expected"
    else:                       rating = "Low"

    return {
        'available_hours':        round(available_hours, 2),
        'meeting_percentage':     round((meeting_hours / WORK_HOURS_PER_DAY) * 100, 1),
        'role_multiplier':        multiplier,
        'role':                   role,
        'expected_tickets':       expected_tickets,
        'closed_tickets':         closed_ticket_count,
        'ticket_ratio':           round(closed_ticket_count / expected_tickets, 2) if expected_tickets else 0,
        'meeting_impact_penalty': round(meeting_impact * 100, 1),
        'ticket_score':           round(ticket_score, 1),
        'weighted_score':         round(weighted_score, 1),
        'performance_rating':     rating,
    }


# ─────────────────────────────────────────────
# Per-technician processor
# ─────────────────────────────────────────────

def process_technician(tech_name, tech_info, graph_token, sdp_token, target_date):
    """Fetch meetings, closed ticket details, and performance score for one technician"""
    if isinstance(tech_info, dict):
        tech_email         = tech_info.get('email')
        tech_job_title     = tech_info.get('job_title')
        tech_department    = tech_info.get('department')
        tech_office        = tech_info.get('office_location')
        tech_city          = tech_info.get('city')
        tech_state         = tech_info.get('state')
        tech_country       = tech_info.get('country')
        tech_manager_name  = tech_info.get('manager_name')
        tech_manager_email = tech_info.get('manager_email')
        tech_acct_enabled  = tech_info.get('account_enabled')
        tech_display_name  = tech_info.get('display_name')
        tech_company       = tech_info.get('company_name')
        tech_acct_created  = tech_info.get('account_created')
    else:
        tech_email = tech_info
        tech_job_title = tech_department = tech_office = None
        tech_city = tech_state = tech_country = None
        tech_manager_name = tech_manager_email = None
        tech_acct_enabled = tech_display_name = tech_company = tech_acct_created = None

    meetings    = get_meetings(tech_email, graph_token, target_date)
    tickets     = get_closed_tickets_detail(tech_email, sdp_token, target_date)
    sla_summary = build_sla_summary(tickets)
    performance = calculate_performance(meetings, len(tickets), tech_job_title)

    return {
        'name':            tech_name,
        'email':           tech_email,
        'job_title':       tech_job_title,
        'department':      tech_department,
        'office_location': tech_office,
        'city':            tech_city,
        'state':           tech_state,
        'country':         tech_country,
        'manager_name':    tech_manager_name,
        'manager_email':   tech_manager_email,
        'account_enabled': tech_acct_enabled,
        'display_name':    tech_display_name,
        'company_name':    tech_company,
        'account_created': tech_acct_created,
        'meetings':        meetings,
        'tickets':         tickets,
        'sla_summary':     sla_summary,
        'performance':     performance,
    }


# ─────────────────────────────────────────────
# File output
# ─────────────────────────────────────────────

def save_to_file(tech_data, date):
    """Save full metrics (including ticket detail) to JSON file"""
    filename = f"tech_performance_{date.strftime('%Y%m%d')}.json"
    output = {}
    for tech_name, data in tech_data.items():
        output[tech_name] = {
            'date':        date.isoformat(),
            'profile': {
                'email':           data.get('email'),
                'display_name':    data.get('display_name'),
                'job_title':       data.get('job_title'),
                'department':      data.get('department'),
                'office_location': data.get('office_location'),
                'city':            data.get('city'),
                'state':           data.get('state'),
                'country':         data.get('country'),
                'manager_name':    data.get('manager_name'),
                'manager_email':   data.get('manager_email'),
                'company_name':    data.get('company_name'),
                'account_enabled': data.get('account_enabled'),
                'account_created': data.get('account_created'),
            },
            'meetings':    data['meetings'],
            'tickets':     data['tickets'],
            'sla_summary': data['sla_summary'],
            'performance': data['performance'],
        }
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    return filename


# ─────────────────────────────────────────────
# Dust upload
# ─────────────────────────────────────────────

def _fmt_mins(minutes):
    """Format minutes as Xh Ym string"""
    if minutes is None:
        return 'N/A'
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def upload_to_dust(tech_data, date):
    """Upload comprehensive performance document per technician to Dust"""
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        return 0

    base_url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    headers  = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type':  'application/json'
    }
    date_str = date.strftime('%Y-%m-%d')
    uploaded = 0

    for tech_name, data in tech_data.items():
        m       = data['meetings']
        p       = data['performance']
        sla     = data['sla_summary']
        tickets = data['tickets']

        loc_parts = [x for x in [data.get('city'), data.get('state'), data.get('country')] if x]
        location_string = ', '.join(loc_parts) if loc_parts else (data.get('office_location') or 'N/A')

        # Tags
        tags = [
            "performance-metrics",
            "meeting-load",
            f"technician:{tech_name.lower().replace(' ', '-')}",
            f"date:{date_str}",
            f"role:{p['role']}",
            f"performance:{p['performance_rating'].lower().replace(' ', '-')}",
        ]
        if data.get('job_title'):
            tags.append(f"job-title:{data['job_title'].lower().replace(' ', '-')}")
        if data.get('department'):
            tags.append(f"department:{data['department'].lower().replace(' ', '-')}")
        if data.get('manager_name'):
            tags.append(f"manager:{data['manager_name'].lower().replace(' ', '-')}")
        if data.get('city'):
            tags.append(f"city:{data['city'].lower().replace(' ', '-')}")
        if data.get('account_enabled') == False:
            tags.append("account-disabled")
        if sla['total'] > 0:
            if sla['sla_met_pct'] == 100:
                tags.append("sla:all-met")
            elif sla['sla_met_pct'] >= 80:
                tags.append("sla:mostly-met")
            else:
                tags.append("sla:breaches-detected")
        n = len(tickets)
        if   n >= 10: tags.append("high-productivity")
        elif n >= 5:  tags.append("moderate-productivity")
        elif n >  0:  tags.append("active-productivity")
        else:         tags.append("no-closures")

        # Ticket table
        if tickets:
            ticket_rows = []
            for t in tickets:
                sla_flag = "VIOLATED" if t['sla_violated'] else "Met"
                res_str  = _fmt_mins(t['resolution_minutes'])
                ticket_rows.append(
                    f"| {t['ticket_id']:>8} | {t['subject'][:55]:<55} | {t['priority']:<8} "
                    f"| {t['category']:<20} | {t['sla_name']:<20} | {sla_flag:<10} "
                    f"| {res_str:<10} | {t['completed_time_iso'] or 'N/A'} |"
                )
            ticket_header = (
                "| Ticket # | Subject                                                 | Priority "
                "| Category             | SLA Policy           | SLA Status "
                "| Resolution | Closed At           |\n"
                "|----------|----------------------------------------------------------|----------"
                "|----------------------|----------------------|------------"
                "|------------|---------------------|"
            )
            ticket_table = ticket_header + "\n" + "\n".join(ticket_rows)
        else:
            ticket_table = "_No tickets closed on this date._"

        # Document text
        doc_text = f"""# {tech_name} - {date.strftime('%B %d, %Y')}

## Profile
- **Name:** {data.get('display_name') or tech_name}
- **Job Title:** {data.get('job_title') or 'N/A'}
- **Role Category:** {p['role'].title()}
- **Department:** {data.get('department') or 'N/A'}
- **Location:** {location_string}
- **Company:** {data.get('company_name') or 'N/A'}
- **Email:** {data.get('email') or 'N/A'}

## Organizational Structure
- **Reports To:** {data.get('manager_name') or 'N/A'}
- **Manager Email:** {data.get('manager_email') or 'N/A'}

## Calendar & Availability
- **Meetings:** {m['count']} meetings
- **Meeting Time:** {m['hours']} hours ({p['meeting_percentage']}% of workday)
- **Available Hours:** {p['available_hours']} hours
- **Meeting Impact Penalty:** -{p['meeting_impact_penalty']}%

## Closed Tickets — {date_str}

{ticket_table}

## SLA Summary
- **Total Tickets Closed:** {sla['total']}
- **SLA Met:** {sla['sla_met']} ({sla['sla_met_pct']}%)
- **SLA Violated:** {sla['sla_violated']}
- **Avg Resolution Time:** {_fmt_mins(sla['avg_resolution_minutes'])}
- **Fastest Resolution:** {_fmt_mins(sla['min_resolution_minutes'])}
- **Slowest Resolution:** {_fmt_mins(sla['max_resolution_minutes'])}

## Performance Score
- **Tickets Closed:** {len(tickets)} vs {p['expected_tickets']} expected for {p['role']} role
- **Ticket Ratio:** {p['ticket_ratio']}
- **Base Ticket Score:** {p['ticket_score']}/100
- **Meeting Impact Penalty:** -{p['meeting_impact_penalty']}%
- **Role Multiplier:** {p['role_multiplier']}x
- **Weighted Score:** {p['weighted_score']}
- **Performance Rating:** {p['performance_rating']}

## Account Status
- **Account Enabled:** {'Yes' if data.get('account_enabled') else 'No' if data.get('account_enabled') == False else 'Unknown'}
- **Account Created:** {data.get('account_created') or 'N/A'}

## Notes
- Ticket details sourced from ServiceDesk Plus (completed_time filter + status=Closed)
- SLA violated flag reflects is_overdue at time of closure
- Performance scoring weighted by role; directors penalised less for high meeting load
- Meeting tracking: full 24-hour day, excludes only cancelled events
- v18.0.0: Full ticket detail (number, SLA, priority, resolution time) per technician per day
"""

        # Structured data
        structured = {
            "technician_name":        tech_name,
            "display_name":           data.get('display_name'),
            "job_title":              data.get('job_title'),
            "department":             data.get('department'),
            "office_location":        data.get('office_location'),
            "city":                   data.get('city'),
            "state":                  data.get('state'),
            "country":                data.get('country'),
            "location_string":        location_string,
            "manager_name":           data.get('manager_name'),
            "manager_email":          data.get('manager_email'),
            "company_name":           data.get('company_name'),
            "account_enabled":        data.get('account_enabled'),
            "account_created":        data.get('account_created'),
            "email":                  data.get('email'),
            "date":                   date_str,
            "meeting_count":          m['count'],
            "meeting_hours":          m['hours'],
            "meeting_percentage":     p['meeting_percentage'],
            "available_hours":        p['available_hours'],
            "closed_ticket_count":    len(tickets),
            "sla_met":                sla['sla_met'],
            "sla_violated":           sla['sla_violated'],
            "sla_met_pct":            sla['sla_met_pct'],
            "avg_resolution_minutes": sla['avg_resolution_minutes'],
            "min_resolution_minutes": sla['min_resolution_minutes'],
            "max_resolution_minutes": sla['max_resolution_minutes'],
            "tickets": [
                {
                    "ticket_id":              t['ticket_id'],
                    "subject":                t['subject'],
                    "priority":               t['priority'],
                    "category":               t['category'],
                    "sla_name":               t['sla_name'],
                    "sla_violated":           t['sla_violated'],
                    "created_time_iso":       t['created_time_iso'],
                    "completed_time_iso":     t['completed_time_iso'],
                    "due_by_time_iso":        t['due_by_time_iso'],
                    "first_response_due_iso": t['first_response_due_iso'],
                    "resolution_minutes":     t['resolution_minutes'],
                }
                for t in tickets
            ],
            "role_multiplier":        p['role_multiplier'],
            "role":                   p['role'],
            "expected_tickets":       p['expected_tickets'],
            "ticket_ratio":           p['ticket_ratio'],
            "ticket_score":           p['ticket_score'],
            "meeting_impact_penalty": p['meeting_impact_penalty'],
            "weighted_score":         p['weighted_score'],
            "performance_rating":     p['performance_rating'],
        }

        document = {
            "document_id":     f"perf-{tech_name.lower().replace(' ', '-')}-{date_str}",
            "timestamp":       int(date.timestamp() * 1000),
            "tags":            tags,
            "text":            doc_text,
            "structured_data": structured,
        }

        url = f"{base_url}/{document['document_id']}"
        try:
            resp = requests.post(url, headers=headers, json=document, timeout=30)
            if resp.status_code in [200, 201]:
                uploaded += 1
            else:
                logger.warning(f"Dust upload failed for {tech_name}: {resp.status_code} {resp.text[:150]}")
        except Exception as ex:
            logger.warning(f"Dust upload exception for {tech_name}: {ex}")

    return uploaded


# ─────────────────────────────────────────────
# Progress tracking
# ─────────────────────────────────────────────

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {'completed': [], 'failed': []}


def save_progress(progress):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)


# ─────────────────────────────────────────────
# Single-date processor
# ─────────────────────────────────────────────

def process_single_date(target_date, technicians):
    """Process all technicians for one date - parallel"""
    logger.info(f"Processing: {target_date.strftime('%Y-%m-%d')}")

    graph_token = get_graph_token()
    sdp_token   = get_sdp_token()

    tech_data = {}

    with ThreadPoolExecutor(max_workers=PARALLEL_TECHNICIANS) as executor:
        futures = {
            executor.submit(
                process_technician,
                name, info, graph_token, sdp_token, target_date
            ): name
            for name, info in technicians.items()
        }
        for future in as_completed(futures):
            tech_name = futures[future]
            try:
                r = future.result()
                tech_data[r['name']] = {
                    'email':           r['email'],
                    'display_name':    r['display_name'],
                    'job_title':       r['job_title'],
                    'department':      r['department'],
                    'office_location': r['office_location'],
                    'city':            r['city'],
                    'state':           r['state'],
                    'country':         r['country'],
                    'manager_name':    r['manager_name'],
                    'manager_email':   r['manager_email'],
                    'company_name':    r['company_name'],
                    'account_enabled': r['account_enabled'],
                    'account_created': r['account_created'],
                    'meetings':        r['meetings'],
                    'tickets':         r['tickets'],
                    'sla_summary':     r['sla_summary'],
                    'performance':     r['performance'],
                }
            except Exception as e:
                logger.error(f"Failed to process {tech_name}: {e}")

    # Console summary
    print()
    print("=" * 150)
    print(f"  {target_date.strftime('%B %d, %Y')}")
    print("=" * 150)
    print(f"  {'Name':<25} {'Title':<28} {'Mgr':<15} {'Mtgs':>5} {'MtgH':>5} {'Closed':>7} {'SLA%':>6} {'AvgRes':>8} {'Score':>7} {'Rating':<16}")
    print("-" * 150)

    for name, d in sorted(tech_data.items()):
        m    = d['meetings']
        p    = d['performance']
        sla  = d['sla_summary']
        title   = (d.get('job_title') or 'N/A')[:27]
        mgr     = (d.get('manager_name') or 'N/A')[:14]
        avg_res = _fmt_mins(sla['avg_resolution_minutes'])
        sla_pct = f"{sla['sla_met_pct']}%" if sla['total'] > 0 else 'N/A'
        print(
            f"  {name:<25} {title:<28} {mgr:<15} {m['count']:>5} {m['hours']:>5.1f} "
            f"{len(d['tickets']):>7} {sla_pct:>6} {avg_res:>8} {p['weighted_score']:>7.1f} {p['performance_rating']:<16}"
        )

    print()

    filename = save_to_file(tech_data, target_date)
    uploaded = upload_to_dust(tech_data, target_date)
    logger.info(f"✓ Saved: {filename}  |  Uploaded to Dust: {uploaded}/{len(tech_data)}")
    return filename


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Comprehensive performance backfill v18.0.0 — meetings, profile, ticket detail, SLA'
    )
    parser.add_argument('--days',       type=int, help='Number of days to backfill')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date',   type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--force',      action='store_true', help='Re-process all dates (ignore progress file)')
    args = parser.parse_args()

    print("=" * 130)
    print("  PERFORMANCE DATA BACKFILL v18.0.0")
    print("  Meetings · Azure AD Profile · Ticket Detail · SLA Breakdown · Performance Scoring")
    print("=" * 130)

    if not args.days and not args.start_date:
        choice = input("\nBackfill mode — (1) Last N days  (2) Date range: ").strip()
        if choice == "1":
            n = input("  Days to backfill [30]: ").strip()
            args.days = int(n) if n else 30
        elif choice == "2":
            args.start_date = input("  Start date (YYYY-MM-DD): ").strip()
            args.end_date   = input("  End date   (YYYY-MM-DD): ").strip()
        else:
            print("Invalid choice. Exiting.")
            sys.exit(1)
        if input("  Force re-process all dates? [no]: ").strip().lower() in ('y', 'yes'):
            args.force = True

    # Tokens
    logger.info("Obtaining tokens...")
    try:
        graph_token = get_graph_token()
        sdp_token   = get_sdp_token()
    except Exception as e:
        logger.error(f"Token error: {e}")
        sys.exit(1)
    logger.info("✓ Tokens obtained")

    # Technicians
    logger.info("Discovering technicians + Azure AD profiles (manager lookup included)...")
    try:
        technicians = discover_technicians(sdp_token, graph_token)
    except Exception as e:
        logger.error(f"Technician discovery failed: {e}")
        sys.exit(1)

    if not technicians:
        logger.error("No technicians found. Exiting.")
        sys.exit(1)

    with_mgr = sum(1 for t in technicians.values() if t.get('manager_name'))
    with_loc = sum(1 for t in technicians.values() if t.get('city'))
    logger.info(f"✓ {len(technicians)} technicians | {with_mgr} with managers | {with_loc} with location data")

    # Date list (newest to oldest)
    if args.start_date:
        start_date = datetime.strptime(clean_date_string(args.start_date), '%Y-%m-%d')
        end_date   = datetime.strptime(clean_date_string(args.end_date),   '%Y-%m-%d')
        dates = []
        cur = end_date
        while cur >= start_date:
            dates.append(cur)
            cur -= timedelta(days=1)
    else:
        days  = args.days or 30
        dates = [datetime.now() - timedelta(days=i) for i in range(1, days + 1)]

    progress        = load_progress()
    completed_dates = set(progress['completed'])
    dates_to_process = dates if args.force else [d for d in dates if d.strftime('%Y-%m-%d') not in completed_dates]

    if not dates_to_process:
        logger.info("All dates already processed!")
        return

    logger.info(f"Processing {len(dates_to_process)} dates  [{dates_to_process[0].strftime('%Y-%m-%d')} → {dates_to_process[-1].strftime('%Y-%m-%d')}]")

    if len(dates_to_process) > 5 and sys.stdin.isatty():
        if input(f"\nProceed with {len(dates_to_process)} dates? [yes/no]: ").strip().lower() not in ('y', 'yes'):
            print("Cancelled.")
            return

    start_wall = time.time()

    for i, date in enumerate(dates_to_process, 1):
        try:
            process_single_date(date, technicians)
            progress['completed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
        except Exception as e:
            logger.error(f"Failed {date.strftime('%Y-%m-%d')}: {e}")
            progress['failed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
            continue

        elapsed  = time.time() - start_wall
        avg_time = elapsed / i
        eta      = (len(dates_to_process) - i) * avg_time
        print(f"  [{i}/{len(dates_to_process)}] ({i/len(dates_to_process)*100:.0f}%)  elapsed: {elapsed/60:.1f}m  ETA: {eta/60:.1f}m\n")

        if i < len(dates_to_process):
            time.sleep(2)

    total_time = time.time() - start_wall
    print()
    print("=" * 130)
    print(f"  BACKFILL COMPLETE — {len(dates_to_process)} dates in {total_time/60:.1f} min")
    if progress['failed']:
        print(f"  Failed dates: {', '.join(progress['failed'])}")
    print("=" * 130)

    if not progress['failed'] and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted — progress saved.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
