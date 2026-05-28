#!/usr/bin/env python3
"""
Performance Data Backfill Script v19.5.8
Upserts only new/changed rows using the Dust rows endpoint.
Historical data is never re-uploaded — scale stays constant regardless of history size.

v19.5.8 Changes (from v19.5.7):
- Removed debug logging (clean production version)
- KEEPS: fields_required in list_info (fixes AvgRes N/A — forces SDP to return
  created_time, completed_time, due_by_time, first_response_due_by_time, is_overdue)
- KEEPS: SLA calc from timestamps (completed_time > due_by_time) instead of is_overdue
- KEEPS: _extract_time_ms() handles all SDP timestamp formats
"""

import os
import sys
import requests
import json
import logging
import time
import argparse
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
WORK_HOURS_PER_DAY = 8
PARALLEL_TECHNICIANS = 8
EMAIL_DOMAIN = 'motorad.com'
PROGRESS_FILE = 'backfill_progress.json'

# Token cache
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
DUST_SPACE_ID     = clean_credential(os.environ.get('DUST_SPACE_ID'))
DUST_DS_ID        = clean_credential(os.environ.get('DUST_DS_ID'))
DUST_TABLE_ID     = clean_credential(os.environ.get('DUST_TABLE_ID', 'tech_performance'))

SDP_API_BASE   = f"https://{SDP_DOMAIN}/api/v3"
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Token management
# ─────────────────────────────────────────────

def get_graph_token():
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
# Technician helpers
# ─────────────────────────────────────────────

def _build_tech_profile(email, az):
    return {
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


# ─────────────────────────────────────────────
# Technician discovery
# ─────────────────────────────────────────────

def discover_technicians(sdp_token, graph_token):
    headers = {
        'Authorization': f'Zoho-oauthtoken {sdp_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }

    technicians = {}

    # Attempt 1: /technicians endpoint (requires admin scope)
    logger.info("Attempting technician discovery via /technicians endpoint...")
    tech_endpoint_ok = False
    start_index = 1
    row_count   = 100

    while True:
        list_info = {"list_info": {"row_count": row_count, "start_index": start_index}}
        try:
            resp = requests.get(
                f"{SDP_API_BASE}/technicians",
                headers=headers,
                params={'input_data': json.dumps(list_info)},
                timeout=30
            )
        except Exception as e:
            logger.warning(f"Technician endpoint request failed: {e}")
            break

        if resp.status_code in [401, 403]:
            logger.warning(
                f"/technicians returned {resp.status_code} — "
                "token lacks admin scope. Falling back to request scan."
            )
            break

        if resp.status_code != 200:
            logger.warning(f"Technician list failed: {resp.status_code} {resp.text[:200]}")
            break

        page_techs = resp.json().get('technicians', [])
        tech_endpoint_ok = True

        for tech in page_techs:
            name  = tech.get('name')
            email = tech.get('email_id')
            if not name or name == 'Unassigned' or name in technicians:
                continue
            az = get_azure_profile(email, graph_token)
            technicians[name] = _build_tech_profile(email, az)

        if len(page_techs) < row_count:
            break
        start_index += row_count

    if tech_endpoint_ok and technicians:
        logger.info(f"✓ Discovered {len(technicians)} technicians via /technicians endpoint")
        return technicians

    # Attempt 2: Scan /requests for all statuses
    logger.info("Falling back to request scan for technician discovery...")

    for status_filter in [None, "Closed", "Resolved"]:
        start_index = 1
        while True:
            criteria = [{"field": "status.name", "condition": "is", "values": [status_filter]}] if status_filter else []
            list_info = {
                "list_info": {
                    "row_count":   100,
                    "start_index": start_index,
                    **({"search_criteria": criteria} if criteria else {})
                }
            }
            try:
                resp = requests.get(
                    f"{SDP_API_BASE}/requests",
                    headers=headers,
                    params={'input_data': json.dumps(list_info)},
                    timeout=60
                )
            except Exception as e:
                logger.warning(f"Request scan failed ({status_filter}): {e}")
                break

            if resp.status_code != 200:
                logger.warning(f"Request scan error {resp.status_code} ({status_filter})")
                break

            page = resp.json().get('requests', [])

            for ticket in page:
                tech = ticket.get('technician')
                if tech and isinstance(tech, dict):
                    name  = tech.get('name')
                    email = tech.get('email_id')
                    if name and name != 'Unassigned' and name not in technicians:
                        az = get_azure_profile(email, graph_token)
                        technicians[name] = _build_tech_profile(email, az)

            if len(page) < 100:
                break
            start_index += 100

    logger.info(f"✓ Discovered {len(technicians)} technicians via request scan")
    return technicians


# ─────────────────────────────────────────────
# Calendar / meetings
# ─────────────────────────────────────────────

def fix_datetime_str(dt_str):
    dt_str = dt_str.replace('Z', '+00:00')
    return re.sub(r'\.(\d{6})\d+', r'.\1', dt_str)


def get_meetings(email, graph_token, date):
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
# Closed ticket detail retrieval
# ─────────────────────────────────────────────

def _extract_time_ms(field_value):
    """
    Extract epoch milliseconds from SDP time field.
    Handles all formats SDP may return:
      - Object:      {"value": "1748433600000"} or {"value": 1748433600000}
      - Flat string: "1748433600000"
      - Flat int:    1748433600000
      - Missing:     None
    """
    if field_value is None:
        return None
    if isinstance(field_value, dict):
        val = field_value.get('value')
    else:
        val = field_value
    if val is None:
        return None
    try:
        return int(str(val).replace(',', ''))
    except Exception:
        return None


def _epoch_ms_to_iso(ms):
    if not ms:
        return None
    try:
        return datetime.utcfromtimestamp(int(ms) / 1000).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return None


def _resolution_minutes(created_ms, completed_ms):
    if not created_ms or not completed_ms:
        return None
    try:
        return int((int(completed_ms) - int(created_ms)) / 60000)
    except Exception:
        return None


def get_closed_tickets_detail(tech_email, sdp_token, target_date):
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

    def fetch_pages(endpoint):
        results     = []
        start_index = 1
        row_count   = 100

        while True:
            list_info = {
                "list_info": {
                    "row_count":   row_count,
                    "start_index": start_index,
                    # Explicitly request timestamp fields — SDP v3 omits them
                    # from bulk list responses unless asked.
                    "fields_required": [
                        "id",
                        "display_id",
                        "subject",
                        "status",
                        "priority",
                        "category",
                        "technician",
                        "sla",
                        "created_time",
                        "completed_time",
                        "due_by_time",
                        "first_response_due_by_time",
                        "is_overdue"
                    ],
                    "search_criteria": [
                        {"field": "technician.email_id", "condition": "is", "values": [tech_email]},
                        {"field": "completed_time", "condition": "between", "logical_operator": "AND", "values": [start_ms, end_ms]},
                        {"field": "status.name", "condition": "is", "logical_operator": "AND", "values": ["Closed"]}
                    ]
                }
            }

            page_tickets = []
            success      = False

            for attempt in range(3):
                try:
                    resp = requests.get(
                        endpoint,
                        headers=headers,
                        params={'input_data': json.dumps(list_info)},
                        timeout=30
                    )
                   if resp.status_code == 200:
                        data = resp.json()
                        page_tickets = data.get('requests', data.get('archive_requests', []))
                        success = True
                        break
                    elif resp.status_code == 404:
                        logger.debug(f"Archive endpoint not available: {endpoint}")
                        return results
                    elif resp.status_code == 429:
                        wait = 2 ** attempt * 5
                        logger.warning(f"Rate-limited. Waiting {wait}s...")
                        time.sleep(wait)
                    else:
                        logger.warning(f"API error {resp.status_code} [{endpoint.split('/')[-1]}]: {resp.text[:200]}")
                        if attempt < 2:
                            time.sleep(2)
                except Exception as ex:
                    logger.warning(f"Exception (attempt {attempt+1}): {ex}")
                    if attempt < 2:
                        time.sleep(2)

            if not success:
                break

            results.extend(page_tickets)

            if len(page_tickets) < row_count:
                break

            start_index += row_count

        return results

    active_tickets   = fetch_pages(f"{SDP_API_BASE}/requests")
    archived_tickets = fetch_pages(f"{SDP_API_BASE}/archive_requests")

    if archived_tickets:
        logger.info(f"  {tech_email}: {len(active_tickets)} active + {len(archived_tickets)} archived")

    seen_ids    = set()
    all_tickets = []

    for t in active_tickets + archived_tickets:
        internal_id = str(t.get('id', ''))
        display_id  = str(t.get('display_id', '') or t.get('displayId', '') or internal_id)

        if not internal_id or internal_id in seen_ids:
            continue
        seen_ids.add(internal_id)

        if (t.get('status') or {}).get('name') == 'Converted to Project':
            continue

        priority = (t.get('priority') or {}).get('name') or 'N/A'
        category = (t.get('category') or {}).get('name') or 'N/A'
        sla_obj  = t.get('sla') or {}
        sla_name = sla_obj.get('name') or 'N/A'

        # SLA from timestamps — more reliable than is_overdue
        due_ms_val       = _extract_time_ms(t.get('due_by_time'))
        completed_ms_val = _extract_time_ms(t.get('completed_time'))

        if due_ms_val and completed_ms_val:
            sla_violated = completed_ms_val > due_ms_val
        else:
            sla_violated = bool(t.get('is_overdue', False))

        created_ms        = _extract_time_ms(t.get('created_time'))
        completed_ms      = _extract_time_ms(t.get('completed_time'))
        due_by_ms         = _extract_time_ms(t.get('due_by_time'))
        first_response_ms = _extract_time_ms(t.get('first_response_due_by_time'))

        all_tickets.append({
            'ticket_id':              display_id,
            'subject':                t.get('subject', 'N/A'),
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

    return all_tickets


def build_sla_summary(tickets):
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
# Table row builder
# ─────────────────────────────────────────────

def _fmt_mins(minutes):
    if minutes is None:
        return 'N/A'
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _build_table_rows(tech_data, date):
    date_str  = date.strftime('%Y-%m-%d')
    month_str = date.strftime('%Y-%m')
    rows = []

    for tech_name, data in tech_data.items():
        tech_slug = tech_name.lower().replace(' ', '-')
        p   = data['performance']
        m   = data['meetings']
        sla = data['sla_summary']

        tech_ctx = {
            "technician":                   tech_name,
            "tech_slug":                    tech_slug,
            "email":                        data.get('email')            or '',
            "display_name":                 data.get('display_name')    or '',
            "job_title":                    data.get('job_title')       or '',
            "department":                   data.get('department')      or '',
            "office_location":              data.get('office_location') or '',
            "city":                         data.get('city')            or '',
            "state":                        data.get('state')           or '',
            "country":                      data.get('country')         or '',
            "manager_name":                 data.get('manager_name')    or '',
            "manager_email":                data.get('manager_email')   or '',
            "company_name":                 data.get('company_name')    or '',
            "date":                         date_str,
            "month":                        month_str,
            "meeting_count":                m['count'],
            "meeting_hours":                m['hours'],
            "meeting_percentage":           p['meeting_percentage'],
            "available_hours":              p['available_hours'],
            "daily_sla_met_pct":            sla['sla_met_pct'],
            "daily_avg_resolution_minutes": sla['avg_resolution_minutes'],
            "role":                         p['role'],
            "expected_tickets":             p['expected_tickets'],
            "daily_closed_count":           len(data['tickets']),
            "weighted_score":               p['weighted_score'],
            "performance_rating":           p['performance_rating'],
        }

        if data['tickets']:
            for t in data['tickets']:
                rows.append({
                    "row_id": f"{tech_slug}-{t['ticket_id']}",
                    "value": {
                        **tech_ctx,
                        "ticket_id":              t['ticket_id'],
                        "subject":                t['subject'],
                        "priority":               t['priority'],
                        "category":               t['category'],
                        "sla_name":               t['sla_name'],
                        "sla_violated":           t['sla_violated'],
                        "created_time_iso":       t['created_time_iso']       or '',
                        "completed_time_iso":     t['completed_time_iso']     or '',
                        "due_by_time_iso":        t['due_by_time_iso']        or '',
                        "first_response_due_iso": t['first_response_due_iso'] or '',
                        "resolution_minutes":     t['resolution_minutes'],
                    }
                })
        else:
            rows.append({
                "row_id": f"{tech_slug}-{date_str}-no-tickets",
                "value": {
                    **tech_ctx,
                    "ticket_id":              '',
                    "subject":                '',
                    "priority":               '',
                    "category":               '',
                    "sla_name":               '',
                    "sla_violated":           False,
                    "created_time_iso":       '',
                    "completed_time_iso":     '',
                    "due_by_time_iso":        '',
                    "first_response_due_iso": '',
                    "resolution_minutes":     None,
                }
            })

    return rows


# ─────────────────────────────────────────────
# Dust table helpers
# ─────────────────────────────────────────────

# Module-level cache — table ID resolved once per run
_TABLE_ID_CACHE = None


def _get_existing_table_id():
    global _TABLE_ID_CACHE
    if _TABLE_ID_CACHE:
        return _TABLE_ID_CACHE

    if not all([DUST_API_KEY, DUST_WORKSPACE_ID, DUST_SPACE_ID, DUST_DS_ID]):
        return DUST_TABLE_ID

    base_url = (
        "https://dust.tt/api/v1/w/" + DUST_WORKSPACE_ID +
        "/spaces/" + DUST_SPACE_ID +
        "/data_sources/" + DUST_DS_ID
    )
    try:
        resp = requests.get(
            base_url + "/tables",
            headers={'Authorization': 'Bearer ' + DUST_API_KEY},
            timeout=30
        )
        if resp.status_code == 200:
            for t in resp.json().get('tables', []):
                if t.get('name') == 'tech_performance':
                    tid = t.get('table_id') or t.get('tableId')
                    if tid:
                        logger.info(f"Discovered table: name=tech_performance, table_id={tid}")
                        _TABLE_ID_CACHE = tid
                        return _TABLE_ID_CACHE
        else:
            logger.warning(f"Could not list tables: {resp.status_code} {resp.text[:200]}")
    except Exception as ex:
        logger.warning(f"Table discovery exception: {ex}")

    logger.info(f"No existing table found — using fallback DUST_TABLE_ID={DUST_TABLE_ID}")
    _TABLE_ID_CACHE = DUST_TABLE_ID
    return _TABLE_ID_CACHE


def ensure_table_exists():
    if not all([DUST_API_KEY, DUST_WORKSPACE_ID, DUST_SPACE_ID, DUST_DS_ID]):
        return

    table_id = _get_existing_table_id()
    base_url = (
        "https://dust.tt/api/v1/w/" + DUST_WORKSPACE_ID +
        "/spaces/" + DUST_SPACE_ID +
        "/data_sources/" + DUST_DS_ID
    )
    try:
        resp = requests.post(
            base_url + "/tables",
            headers={'Authorization': 'Bearer ' + DUST_API_KEY},
            json={
                "table_id":    table_id,
                "name":        "tech_performance",
                "title":       "tech_performance",
                "description": (
                    "Technician performance data — daily closed tickets, "
                    "SLA compliance, resolution times, and meeting load per technician."
                ),
                "timestamp":   int(time.time() * 1000),
            },
            timeout=30
        )
        if resp.status_code in [200, 201]:
            logger.info(f"✓ Table ready (table_id={table_id})")
        else:
            logger.warning(f"Table ensure failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"ensure_table_exists exception: {e}")


def upsert_rows(rows):
    if not all([DUST_API_KEY, DUST_WORKSPACE_ID, DUST_SPACE_ID, DUST_DS_ID]):
        logger.warning("Dust env vars incomplete — skipping row upsert.")
        return False

    if not rows:
        logger.warning("No rows to upsert.")
        return True

    table_id = _get_existing_table_id()
    url = (
        "https://dust.tt/api/v1/w/" + DUST_WORKSPACE_ID +
        "/spaces/" + DUST_SPACE_ID +
        "/data_sources/" + DUST_DS_ID +
        "/tables/" + table_id + "/rows"
    )

    api_rows      = [{"row_id": r["row_id"], "value": r["value"]} for r in rows]
    batch_size    = 500
    total_batches = (len(api_rows) + batch_size - 1) // batch_size

    for batch_num, i in enumerate(range(0, len(api_rows), batch_size), 1):
        batch = api_rows[i:i + batch_size]

        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    headers={'Authorization': 'Bearer ' + DUST_API_KEY},
                    json={"rows": batch, "truncate": False},
                    timeout=120
                )
                if resp.status_code in [200, 201]:
                    logger.info(
                        f"✓ Upserted batch {batch_num}/{total_batches} "
                        f"({len(batch)} rows) → table_id={table_id}"
                    )
                    break
                elif resp.status_code == 429:
                    wait = 2 ** attempt * 15
                    logger.warning(f"Rate-limited. Waiting {wait}s... (attempt {attempt + 1}/3)")
                    time.sleep(wait)
                else:
                    logger.warning(
                        f"Upsert failed batch {batch_num} (attempt {attempt + 1}/3): "
                        f"{resp.status_code} — {resp.text[:400]}"
                    )
                    if attempt < 2:
                        time.sleep(5)
                    else:
                        return False
            except Exception as ex:
                logger.warning(f"Upsert exception batch {batch_num} (attempt {attempt + 1}/3): {ex}")
                if attempt < 2:
                    time.sleep(5)
                else:
                    return False

    logger.info(f"✓ Total upserted: {len(api_rows)} rows (table_id={table_id})")
    return True


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
    rows     = _build_table_rows(tech_data, target_date)

    ticket_rows = sum(1 for r in rows if r['value'].get('ticket_id'))
    logger.info(
        f"✓ Saved: {filename}  |  "
        f"Rows built: {len(rows)} "
        f"({ticket_rows} tickets + {len(rows) - ticket_rows} zero-days)"
    )

    return rows


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Performance backfill v19.5.8'
    )
    parser.add_argument('--days',       type=int, help='Number of days to backfill')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date',   type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--force',      action='store_true', help='Re-process all dates')
    parser.add_argument('--skip-csv',   action='store_true', help='Skip Dust upload (for testing)')
    args = parser.parse_args()

    print("=" * 130)
    print("  PERFORMANCE DATA BACKFILL v19.5.8")
    print("  Meetings · Azure AD · Ticket Detail · Archive Fix · Dust Table (incremental row upsert)")
    print("=" * 130)

    logger.info(f"Dust env — API_KEY: {'SET' if DUST_API_KEY else 'MISSING'} | "
                f"WORKSPACE_ID: {'SET' if DUST_WORKSPACE_ID else 'MISSING'} | "
                f"SPACE_ID: {'SET' if DUST_SPACE_ID else 'MISSING'} | "
                f"DS_ID: {'SET' if DUST_DS_ID else 'MISSING'} | "
                f"TABLE_ID fallback: {DUST_TABLE_ID}")
    logger.info(f"skip_csv flag: {args.skip_csv}")

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

    logger.info("Obtaining tokens...")
    try:
        graph_token = get_graph_token()
        sdp_token   = get_sdp_token()
    except Exception as e:
        logger.error(f"Token error: {e}")
        sys.exit(1)
    logger.info("✓ Tokens obtained")

    logger.info("Discovering technicians...")
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

    if not args.skip_csv:
        ensure_table_exists()

    start_wall = time.time()

    for i, date in enumerate(dates_to_process, 1):
        try:
            new_rows = process_single_date(date, technicians)

            if not args.skip_csv:
                success = upsert_rows(new_rows)
                if not success:
                    logger.error(f"Row upsert failed for {date.strftime('%Y-%m-%d')} — marking as failed")
                    progress['failed'].append(date.strftime('%Y-%m-%d'))
                    save_progress(progress)
                    continue

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
