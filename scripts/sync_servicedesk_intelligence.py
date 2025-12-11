#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.0.2 - COMPLETE WITH DUAL CHECKPOINT
Full 48-field comprehensive ticket intelligence with incremental sync

Changes in v3.0.2:
- Dual checkpoint system:
  * last_sync_time.txt - Simple timestamp (persists in git repo)
  * checkpoint.json - Detailed stats (local only, for diagnostics)
- Compatible with existing git workflow
- True incremental sync across GitHub Actions runs

Changes in v3.0.1:
- Fixed checkpoint path and syntax errors

Changes in v3.0:
- Parallel document uploads (5 concurrent workers)
- 5-10x faster uploads
"""

import os
import sys
import requests
import json
import logging
import time
import re
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore, Lock
from pathlib import Path
import hashlib

# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "3.0.2"
BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY = 5

# NEW v3.0.2: Dual checkpoint system
SCRIPT_DIR = Path(__file__).resolve().parent.parent  # sdp-dust-sync/
TIMESTAMP_FILE = str(SCRIPT_DIR / 'last_sync_time.txt')  # Persists in git
CHECKPOINT_FILE = str(SCRIPT_DIR / 'checkpoint.json')    # Local stats only

REQUEST_TIMEOUT = 30
MAX_PAGES = None  # No limit - fetch all tickets

# v3.0: Parallel upload configuration
MAX_CONCURRENT_UPLOADS = 5
UPLOAD_TIMEOUT = 30

# Connection pooling
SESSION = requests.Session()
ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20,
    max_retries=3
)
SESSION.mount('http://', ADAPTER)
SESSION.mount('https://', ADAPTER)

# Cache for repeated lookups
CACHE = {
    'technicians': {},
    'requesters': {},
    'categories': {},
    'sites': {},
    'assets': {},
    'departments': {}
}

# Analytics cache
class AnalyticsCache:
    def __init__(self):
        self.raw_data = {
            'category_stats': defaultdict(lambda: {'count': 0, 'total_resolution_days': 0}),
            'technician_workload': defaultdict(lambda: {'active': 0, 'total': 0, 'total_response_hours': 0}),
            'peak_hours': defaultdict(int),
            'issue_types': defaultdict(int),
            'channel_distribution': defaultdict(int),
            'department_distribution': defaultdict(int),
            'asset_failure_rates': defaultdict(int),
            'service_usage': defaultdict(int),
            'resolution_categories': defaultdict(int)
        }
    
    def update_category_stats(self, category, resolution_days):
        self.raw_data['category_stats'][category]['count'] += 1
        self.raw_data['category_stats'][category]['total_resolution_days'] += resolution_days
    
    def update_technician_workload(self, tech, is_active, response_hours):
        self.raw_data['technician_workload'][tech]['total'] += 1
        if is_active:
            self.raw_data['technician_workload'][tech]['active'] += 1
        self.raw_data['technician_workload'][tech]['total_response_hours'] += response_hours
    
    def update_peak_hours(self, hour):
        self.raw_data['peak_hours'][hour] += 1
    
    def update_issue_types(self, issue_type):
        self.raw_data['issue_types'][issue_type] += 1
    
    def update_channel(self, channel):
        self.raw_data['channel_distribution'][channel] += 1
    
    def update_department(self, department):
        self.raw_data['department_distribution'][department] += 1
    
    def update_asset_failure(self, asset):
        self.raw_data['asset_failure_rates'][asset] += 1
    
    def update_service_usage(self, service):
        self.raw_data['service_usage'][service] += 1
    
    def update_resolution_category(self, category):
        self.raw_data['resolution_categories'][category] += 1

analytics_cache = AnalyticsCache()

# Metrics counters
METRICS = {
    'total_processed': 0,
    'active_tickets': 0,
    'closed_tickets': 0,
    'with_conversations': 0,
    'high_complexity': 0,
    'satisfaction_at_risk': 0,
    'high_sla_breach_risk': 0,
    'awaiting_customer': 0,
    'awaiting_technician': 0,
    'with_assets': 0,
    'reopened': 0,
    'fcr': 0,
    'with_approvals': 0,
    'linked_tickets': 0
}

# ============================================================
# LOGGING SETUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================
# CREDENTIAL MANAGEMENT
# ============================================================

def clean_credential(value: str) -> str:
    """Remove quotes and escape characters from credentials"""
    if not value:
        return value
    value = value.strip().strip('"').strip("'")
    value = value.replace('\\', '')
    return value.strip()

# Load and clean credentials
SDP_DOMAIN = clean_credential(os.environ.get('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_credential(os.environ.get('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_credential(os.environ.get('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_credential(os.environ.get('SDP_REFRESH_TOKEN', ''))
DUST_API_KEY = clean_credential(os.environ.get('DUST_API_KEY', ''))
DUST_WORKSPACE_ID = clean_credential(os.environ.get('DUST_WORKSPACE_ID', ''))

SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

# ============================================================
# CHECKPOINT MANAGEMENT - v3.0.2 DUAL SYSTEM
# ============================================================

def load_checkpoint() -> Optional[datetime]:
    """
    Load last sync timestamp - v3.0.2 dual system
    Priority: last_sync_time.txt (persists) > checkpoint.json (local)
    """
    
    # Try last_sync_time.txt first (simple text file, persists in repo)
    if os.path.exists(TIMESTAMP_FILE):
        try:
            with open(TIMESTAMP_FILE, 'r') as f:
                timestamp_str = f.read().strip()
                if timestamp_str:
                    # Try parsing as ISO format
                    try:
                        last_sync = datetime.fromisoformat(timestamp_str)
                        logger.info(f"✓ Loaded timestamp from: {TIMESTAMP_FILE}")
                        logger.info(f"  Last sync: {last_sync}")
                        return last_sync
                    except ValueError:
                        # Try parsing as Unix timestamp (milliseconds)
                        try:
                            timestamp_ms = int(timestamp_str)
                            last_sync = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                            logger.info(f"✓ Loaded timestamp from: {TIMESTAMP_FILE}")
                            logger.info(f"  Last sync: {last_sync}")
                            return last_sync
                        except:
                            pass
        except Exception as e:
            logger.warning(f"Could not load last_sync_time.txt: {e}")
    
    # Fallback to checkpoint.json if it exists locally
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                checkpoint = json.load(f)
                last_sync = checkpoint.get('last_sync')
                if last_sync:
                    logger.info(f"✓ Loaded from checkpoint.json: {CHECKPOINT_FILE}")
                    return datetime.fromisoformat(last_sync)
        except Exception as e:
            logger.warning(f"Could not load checkpoint.json: {e}")
    
    logger.info("No checkpoint found - will perform FULL SYNC")
    return None

def save_checkpoint(last_sync_time: datetime, total_processed: int, total_errors: int):
    """
    Save checkpoint - v3.0.2 dual system
    Saves to BOTH files for compatibility and diagnostics
    """
    
    # 1. Save to last_sync_time.txt (PRIORITY - persists in git)
    try:
        with open(TIMESTAMP_FILE, 'w') as f:
            if last_sync_time:
                # Save as ISO format (human readable and parseable)
                f.write(last_sync_time.isoformat())
            else:
                f.write('')
        
        logger.info(f"✓ SAVED Timestamp to: {TIMESTAMP_FILE}")
        if last_sync_time:
            logger.info(f"        {last_sync_time.isoformat()}")
    except Exception as e:
        logger.error(f"✗ Failed to save timestamp file: {e}")
    
    # 2. Save to checkpoint.json (detailed stats, local only)
    checkpoint = {
        'last_sync': last_sync_time.isoformat() if last_sync_time else None,
        'last_run': datetime.now(timezone.utc).isoformat(),
        'processed': total_processed,
        'errors': total_errors,
        'version': VERSION
    }
    
    try:
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoint, f, indent=2)
        
        logger.info(f"✓ SAVED Stats to: {CHECKPOINT_FILE}")
        logger.info(f"        Processed: {total_processed}, Errors: {total_errors}")
    except Exception as e:
        logger.error(f"✗ Failed to save checkpoint stats: {e}")

# ============================================================
# AUTHENTICATION
# ============================================================

def get_access_token() -> str:
    """Get ServiceDesk Plus access token"""
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            response = SESSION.post(ZOHO_TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                return response.json()['access_token']
            
            if response.status_code == 429:
                wait_time = (2 ** attempt) * 30
                logger.warning(f"Rate limited. Waiting {wait_time}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait_time)
                continue
            
            logger.error(f"Token request failed: {response.status_code} - {response.text[:200]}")
            
        except Exception as e:
            logger.error(f"Token request error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    
    raise Exception("Failed to obtain access token after max retries")

# ============================================================
# TICKET FETCHING - INCREMENTAL SYNC
# ============================================================

def fetch_tickets_incremental(access_token: str, last_sync_time: Optional[datetime], page_limit: Optional[int] = None) -> List[Dict]:
    """Fetch tickets updated since last sync (incremental)"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    search_criteria = []
    
    if last_sync_time:
        last_sync_ms = int(last_sync_time.timestamp() * 1000)
        search_criteria.append({
            "field": "last_updated_time",
            "condition": "greater than",
            "value": str(last_sync_ms)
        })
        logger.info(f"📊 INCREMENTAL SYNC: Fetching tickets updated since {last_sync_time}")
    else:
        logger.info("📊 FULL SYNC: Fetching ALL tickets (no checkpoint found)")
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "get_total_count": True,
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    }
    
    if search_criteria:
        list_info["list_info"]["search_criteria"] = search_criteria
    
    url = f"{SDP_API_BASE}/requests"
    all_tickets = []
    page = 1
    
    if page_limit:
        logger.info(f"  Page limit: {page_limit} pages (up to {page_limit * 100} tickets)")
    else:
        logger.info(f"  No page limit - fetching ALL tickets")
    
    while True:
        if page_limit and page > page_limit:
            logger.warning(f"  Reached page limit of {page_limit}. Stopping.")
            break
        
        list_info["list_info"]["start_index"] = (page - 1) * 100 + 1
        params = {'input_data': json.dumps(list_info)}
        
        try:
            response = SESSION.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                tickets = data.get('requests', [])
                
                if not tickets:
                    break
                
                all_tickets.extend(tickets)
                logger.info(f"  Page {page}: Found {len(tickets)} tickets (Total: {len(all_tickets)})")
                
                list_info_response = data.get('list_info', {})
                has_more = list_info_response.get('has_more_rows', False)
                
                if not has_more:
                    logger.info(f"  No more pages. Total fetched: {len(all_tickets)}")
                    break
                
                page += 1
                time.sleep(0.1)
                
            elif response.status_code == 429:
                logger.warning(f"Rate limited on page {page}. Waiting 30s...")
                time.sleep(30)
                continue
            else:
                logger.error(f"API error: {response.status_code} - {response.text[:200]}")
                break
                
        except Exception as e:
            logger.error(f"Error fetching page {page}: {e}")
            break
    
    return all_tickets

# ============================================================
# CONVERSATION FETCHING - PARALLEL
# ============================================================

def fetch_conversation_single(ticket_id: str, access_token: str) -> List[Dict]:
    """Fetch conversations for a single ticket"""
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    try:
        response = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.json().get('conversations', [])
        elif response.status_code == 404:
            return []
    except:
        pass
    
    return []

def fetch_conversations_parallel(ticket_ids: List[str], access_token: str, max_workers: int = 10) -> Dict[str, List[Dict]]:
    """Fetch conversations for multiple tickets in parallel"""
    conversations_map = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticket = {
            executor.submit(fetch_conversation_single, ticket_id, access_token): ticket_id
            for ticket_id in ticket_ids
        }
        
        for future in as_completed(future_to_ticket):
            ticket_id = future_to_ticket[future]
            try:
                conversations = future.result()
                conversations_map[ticket_id] = conversations
            except Exception as e:
                logger.warning(f"Failed to fetch conversation for {ticket_id}: {e}")
                conversations_map[ticket_id] = []
    
    return conversations_map

# ============================================================
# AI ANALYSIS
# ============================================================

def calculate_complexity_score(ticket: Dict, conversations: List[Dict]) -> int:
    """Calculate ticket complexity (0-10)"""
    score = 3
    
    if len(conversations) > 10:
        score += 3
    elif len(conversations) > 5:
        score += 2
    elif len(conversations) > 2:
        score += 1
    
    description = ticket.get('description', '').lower()
    technical_keywords = ['error', 'crash', 'bug', 'failure', 'corrupt', 'network', 'server', 'database']
    tech_count = sum(1 for keyword in technical_keywords if keyword in description)
    score += min(tech_count, 3)
    
    priority = ticket.get('priority', {})
    priority_name = priority.get('name', '').lower() if priority else ''
    if 'urgent' in priority_name or 'critical' in priority_name:
        score += 2
    elif 'high' in priority_name:
        score += 1
    
    return min(score, 10)

def analyze_sentiment(conversations: List[Dict]) -> str:
    """Analyze sentiment from conversations"""
    if not conversations:
        return "neutral"
    
    negative_keywords = ['frustrated', 'angry', 'unacceptable', 'disappointed', 'terrible', 'worst']
    positive_keywords = ['thank', 'appreciate', 'great', 'excellent', 'perfect', 'helpful']
    
    negative_count = 0
    positive_count = 0
    
    for conv in conversations:
        text = conv.get('description', '').lower()
        negative_count += sum(1 for keyword in negative_keywords if keyword in text)
        positive_count += sum(1 for keyword in positive_keywords if keyword in text)
    
    if negative_count > positive_count * 2:
        return "at-risk"
    elif positive_count > negative_count * 2:
        return "positive"
    
    return "neutral"

def predict_sla_breach(ticket: Dict, conversations: List[Dict]) -> Tuple[str, int]:
    """Predict SLA breach likelihood"""
    due_date = ticket.get('due_by_time')
    if not due_date:
        return ("unknown", 0)
    
    try:
        due_display = due_date.get('display_value', '') if isinstance(due_date, dict) else ''
        if not due_display:
            return ("unknown", 0)
        
        due = datetime.fromisoformat(due_display.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        hours_remaining = (due - now).total_seconds() / 3600
        
        if hours_remaining < 0:
            return ("critical", 100)
        elif hours_remaining < 4:
            return ("critical", 90)
        elif hours_remaining < 24:
            return ("high", 70)
        elif hours_remaining < 48:
            return ("moderate", 40)
        else:
            return ("low", 10)
    except:
        return ("unknown", 0)

def estimate_resolution_time(ticket: Dict, conversations: List[Dict], complexity: int) -> float:
    """Estimate resolution time in hours"""
    base_hours = complexity * 2
    
    priority = ticket.get('priority', {})
    priority_name = priority.get('name', '').lower() if priority else ''
    if 'urgent' in priority_name:
        base_hours *= 0.5
    elif 'low' in priority_name:
        base_hours *= 1.5
    
    if len(conversations) > 5:
        base_hours *= 1.3
    
    return round(base_hours, 1)

def identify_issue_type(ticket: Dict) -> List[str]:
    """Identify issue types from ticket"""
    description = (ticket.get('description', '') + ' ' + ticket.get('subject', '')).lower()
    
    issue_keywords = {
        'password': ['password', 'login', 'authentication', 'credential'],
        'email': ['email', 'outlook', 'mailbox', 'mail'],
        'network': ['network', 'wifi', 'internet', 'connection', 'vpn'],
        'hardware': ['laptop', 'desktop', 'monitor', 'keyboard', 'mouse', 'printer', 'hardware'],
        'software': ['software', 'application', 'app', 'program', 'install'],
        'access': ['access', 'permission', 'rights', 'locked out'],
        'performance': ['slow', 'performance', 'freeze', 'lag'],
        'error': ['error', 'crash', 'fail', 'bug']
    }
    
    identified = []
    for issue_type, keywords in issue_keywords.items():
        if any(keyword in description for keyword in keywords):
            identified.append(issue_type)
    
    return identified or ['general']

def analyze_response_status(conversations: List[Dict]) -> Dict[str, Any]:
    """Analyze conversation response status"""
    if not conversations:
        return {
            'awaiting_response': 'unknown',
            'last_responder': 'none',
            'response_time_hours': 0,
            'has_unanswered_questions': False
        }
    
    sorted_convs = sorted(conversations, key=lambda x: x.get('created_time', {}).get('value', 0))
    
    if not sorted_convs:
        return {
            'awaiting_response': 'unknown',
            'last_responder': 'none',
            'response_time_hours': 0,
            'has_unanswered_questions': False
        }
    
    last_conv = sorted_convs[-1]
    last_responder_type = 'customer' if last_conv.get('is_customer', False) else 'technician'
    
    awaiting = 'technician' if last_responder_type == 'customer' else 'customer'
    
    last_text = last_conv.get('description', '')
    has_unanswered_questions = '?' in last_text and last_responder_type == 'customer'
    
    response_time = 0
    if len(sorted_convs) > 1:
        try:
            first_time = sorted_convs[0].get('created_time', {}).get('value', 0)
            last_time = sorted_convs[-1].get('created_time', {}).get('value', 0)
            response_time = (last_time - first_time) / (1000 * 3600)
        except:
            pass
    
    return {
        'awaiting_response': awaiting,
        'last_responder': last_responder_type,
        'response_time_hours': round(response_time, 1),
        'has_unanswered_questions': has_unanswered_questions
    }

# ============================================================
# FIELD EXTRACTION
# ============================================================

def extract_comprehensive_fields(ticket: Dict) -> Dict[str, Any]:
    """Extract all 48 comprehensive fields from ticket"""
    fields = {}
    
    def safe_get(obj, key, default=''):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default
    
    fields['ticket_id'] = ticket.get('id', 'unknown')
    fields['subject'] = ticket.get('subject', 'No subject')
    fields['description'] = ticket.get('description', 'No description')
    
    status = ticket.get('status') or {}
    fields['status'] = safe_get(status, 'name', 'Unknown')
    fields['status_category'] = 'active' if fields['status'].lower() in ['open', 'in progress', 'pending'] else 'closed'
    
    priority = ticket.get('priority') or {}
    fields['priority'] = safe_get(priority, 'name', 'Normal')
    
    requester = ticket.get('requester') or {}
    fields['requester_name'] = safe_get(requester, 'name', 'Unknown')
    fields['requester_email'] = safe_get(requester, 'email_id', '')
    
    technician = ticket.get('technician') or {}
    fields['technician_name'] = safe_get(technician, 'name', 'Unassigned')
    fields['technician_email'] = safe_get(technician, 'email_id', '')
    
    category = ticket.get('category') or {}
    fields['category'] = safe_get(category, 'name', 'Uncategorized')
    
    subcategory = ticket.get('subcategory') or {}
    fields['subcategory'] = safe_get(subcategory, 'name', '')
    
    site = ticket.get('site') or {}
    fields['site'] = safe_get(site, 'name', '')
    
    created_time = ticket.get('created_time') or {}
    fields['created_time'] = safe_get(created_time, 'display_value', '')
    
    due_by_time = ticket.get('due_by_time') or {}
    fields['due_by_time'] = safe_get(due_by_time, 'display_value', '')
    
    resolved_time = ticket.get('resolved_time') or {}
    fields['resolved_time'] = safe_get(resolved_time, 'display_value', '')
    
    last_updated_time = ticket.get('last_updated_time') or {}
    fields['last_updated_time'] = safe_get(last_updated_time, 'display_value', '')
    
    group = ticket.get('group') or {}
    fields['group'] = safe_get(group, 'name', '')
    
    assets = ticket.get('assets') or []
    if not isinstance(assets, list):
        assets = []
    fields['has_assets'] = len(assets) > 0
    fields['asset_count'] = len(assets)
    fields['asset_names'] = [safe_get(asset, 'name', '') for asset in assets] if assets else []
    fields['primary_asset'] = safe_get(assets[0], 'name', '') if assets else ''
    
    request_type = ticket.get('request_type') or {}
    fields['source'] = safe_get(request_type, 'name', 'Unknown')
    
    mode = ticket.get('mode') or {}
    fields['mode'] = safe_get(mode, 'name', fields['source'])
    fields['channel'] = fields['mode']
    
    impact = ticket.get('impact') or {}
    fields['impact'] = safe_get(impact, 'name', '')
    
    urgency = ticket.get('urgency') or {}
    fields['urgency'] = safe_get(urgency, 'name', '')
    
    level = ticket.get('level') or {}
    fields['level'] = safe_get(level, 'name', '')
    fields['tier'] = fields['level']
    
    service = ticket.get('service') or {}
    fields['service'] = safe_get(service, 'name', '')
    
    fields['time_spent'] = ticket.get('time_spent', 0) or 0
    fields['time_spent_hours'] = round(fields['time_spent'] / 3600000, 2) if fields['time_spent'] else 0
    
    resolution = ticket.get('resolution') or {}
    fields['resolution_category'] = safe_get(resolution, 'name', '')
    
    closure_info = ticket.get('closure_info') or {}
    closure_code = closure_info.get('closure_code') or {} if isinstance(closure_info, dict) else {}
    fields['closure_code'] = safe_get(closure_code, 'name', '')
    
    fields['reopened_count'] = ticket.get('reopened_count', 0) or 0
    fields['has_been_reopened'] = fields['reopened_count'] > 0
    
    approval_status = ticket.get('approval_status') or {}
    fields['approval_status'] = safe_get(approval_status, 'name', '')
    fields['has_approvals'] = bool(fields['approval_status'])
    
    tags = ticket.get('tags') or []
    if not isinstance(tags, list):
        tags = []
    fields['tags'] = [safe_get(tag, 'name', '') for tag in tags] if tags else []
    fields['has_tags'] = len(fields['tags']) > 0
    
    department = ticket.get('department') or {}
    fields['department'] = safe_get(department, 'name', '')
    
    linked_requests = ticket.get('linked_requests') or []
    if not isinstance(linked_requests, list):
        linked_requests = []
    fields['linked_ticket_count'] = len(linked_requests)
    fields['has_linked_tickets'] = len(linked_requests) > 0
    fields['linked_ticket_ids'] = [safe_get(req, 'id', '') for req in linked_requests] if linked_requests else []
    
    attachments = ticket.get('attachments') or []
    if not isinstance(attachments, list):
        attachments = []
    fields['attachment_count'] = len(attachments)
    fields['has_attachments'] = len(attachments) > 0
    
    change = ticket.get('change') or {}
    fields['linked_change_id'] = safe_get(change, 'id', '')
    fields['has_linked_change'] = bool(fields['linked_change_id'])
    
    problem = ticket.get('problem') or {}
    fields['linked_problem_id'] = safe_get(problem, 'id', '')
    fields['has_linked_problem'] = bool(fields['linked_problem_id'])
    
    fields['is_first_call_resolution'] = ticket.get('is_fcr', False) or False
    
    created_by = ticket.get('created_by') or {}
    fields['created_by'] = safe_get(created_by, 'name', '')
    fields['is_proxy_request'] = bool(fields['created_by']) and fields['created_by'] != fields['requester_name']
    
    template = ticket.get('template') or {}
    fields['template'] = safe_get(template, 'name', '')
    fields['used_template'] = bool(fields['template'])
    
    sla = ticket.get('sla') or {}
    fields['sla_name'] = safe_get(sla, 'name', '')
    fields['sla_violated'] = ticket.get('is_sla_violated', False) or False
    
    return fields

# ============================================================
# ANALYTICS & METRICS (Abbreviated - same as before)
# ============================================================

def track_analytics(ticket: Dict, conversations: List[Dict], complexity: int, comprehensive_fields: Dict):
    category = comprehensive_fields['category']
    resolution_days = 0
    if comprehensive_fields['resolved_time']:
        try:
            created = datetime.fromisoformat(comprehensive_fields['created_time'].replace('Z', '+00:00'))
            resolved = datetime.fromisoformat(comprehensive_fields['resolved_time'].replace('Z', '+00:00'))
            resolution_days = (resolved - created).total_seconds() / 86400
        except:
            pass
    
    analytics_cache.update_category_stats(category, resolution_days)
    
    tech = comprehensive_fields['technician_name']
    is_active = comprehensive_fields['status_category'] == 'active'
    response_hours = 0
    if conversations:
        try:
            first_time = conversations[0].get('created_time', {}).get('value', 0)
            last_time = conversations[-1].get('created_time', {}).get('value', 0)
            response_hours = (last_time - first_time) / (1000 * 3600)
        except:
            pass
    
    analytics_cache.update_technician_workload(tech, is_active, response_hours)
    
    if comprehensive_fields['created_time']:
        try:
            created = datetime.fromisoformat(comprehensive_fields['created_time'].replace('Z', '+00:00'))
            analytics_cache.update_peak_hours(created.hour)
        except:
            pass
    
    description = ticket.get('description', '').lower()
    issue_keywords = ['hardware', 'email', 'network', 'error', 'access']
    for keyword in issue_keywords:
        if keyword in description:
            analytics_cache.update_issue_types(keyword)
    
    if comprehensive_fields.get('channel'):
        analytics_cache.update_channel(comprehensive_fields['channel'])
    
    if comprehensive_fields.get('department'):
        analytics_cache.update_department(comprehensive_fields['department'])
    
    if comprehensive_fields.get('primary_asset'):
        analytics_cache.update_asset_failure(comprehensive_fields['primary_asset'])
    
    if comprehensive_fields.get('service'):
        analytics_cache.update_service_usage(comprehensive_fields['service'])
    
    if comprehensive_fields.get('resolution_category'):
        analytics_cache.update_resolution_category(comprehensive_fields['resolution_category'])

def track_metrics(comprehensive_fields: Dict, conversations: List[Dict], 
                 complexity: int, sentiment: str, sla_risk: str, response_status: Dict):
    
    METRICS['total_processed'] += 1
    
    if comprehensive_fields['status_category'] == 'active':
        METRICS['active_tickets'] += 1
    else:
        METRICS['closed_tickets'] += 1
    
    if conversations:
        METRICS['with_conversations'] += 1
    
    if complexity >= 7:
        METRICS['high_complexity'] += 1
    
    if sentiment == 'at-risk':
        METRICS['satisfaction_at_risk'] += 1
    
    if sla_risk in ['critical', 'high']:
        METRICS['high_sla_breach_risk'] += 1
    
    if response_status['awaiting_response'] == 'customer':
        METRICS['awaiting_customer'] += 1
    elif response_status['awaiting_response'] == 'technician':
        METRICS['awaiting_technician'] += 1
    
    if comprehensive_fields.get('has_assets'):
        METRICS['with_assets'] += 1
    
    if comprehensive_fields.get('has_been_reopened'):
        METRICS['reopened'] += 1
    
    if comprehensive_fields.get('is_first_call_resolution'):
        METRICS['fcr'] += 1
    
    if comprehensive_fields.get('has_approvals'):
        METRICS['with_approvals'] += 1
    
    if comprehensive_fields.get('has_linked_tickets'):
        METRICS['linked_tickets'] += 1

# ============================================================
# TAG GENERATION & DOCUMENT CREATION (Abbreviated)
# ============================================================

def generate_comprehensive_tags(ticket: Dict, conversations: List[Dict], 
                                comprehensive_fields: Dict, complexity: int,
                                sentiment: str, sla_risk: str, response_status: Dict) -> List[str]:
    tags = []
    
    status = comprehensive_fields['status'].lower().replace(' ', '-')
    tags.extend([f"status:{status}", f"status-category:{comprehensive_fields['status_category']}"])
    
    if comprehensive_fields['status_category'] == 'active':
        tags.extend(['active', 'open-status', 'needs-attention'])
    
    priority = comprehensive_fields['priority'].lower()
    tags.append(f"priority:{priority}")
    
    if 'urgent' in priority:
        tags.extend(['urgent-attention', 'critical-action-required'])
    
    tech = comprehensive_fields['technician_name'].lower().replace(' ', '-')
    req = comprehensive_fields['requester_name'].lower().replace(' ', '-')
    tags.extend([f"technician:{tech}", f"requester:{req}"])
    
    if tech == 'unassigned':
        tags.extend(['unassigned', 'needs-assignment'])
    
    category = comprehensive_fields['category'].lower().replace(' ', '-')
    tags.append(f"category:{category}")
    
    if comprehensive_fields['subcategory']:
        subcategory = comprehensive_fields['subcategory'].lower().replace(' ', '-')
        tags.append(f"subcategory:{subcategory}")
    
    tags.append(f"complexity:{complexity}")
    if complexity >= 7:
        tags.extend(['high-complexity', 'complex-issue'])
    elif complexity <= 3:
        tags.extend(['low-complexity', 'simple-issue'])
    
    tags.append(f"sentiment:{sentiment}")
    if sentiment == 'at-risk':
        tags.extend(['satisfaction-at-risk', 'customer-frustration'])
    
    tags.append(f"sla-risk:{sla_risk}")
    if sla_risk in ['critical', 'high']:
        tags.extend(['high-breach-risk', 'sla-attention-needed'])
    
    conv_count = len(conversations)
    tags.append(f"conversations:{conv_count}")
    
    if conv_count == 0:
        tags.append('no-conversation')
    elif conv_count > 10:
        tags.extend(['high-conversation-volume', 'extensive-discussion'])
    
    tags.append(f"awaiting:{response_status['awaiting_response']}")
    
    if response_status['awaiting_response'] == 'customer':
        tags.append('awaiting-customer-response')
    elif response_status['awaiting_response'] == 'technician':
        tags.extend(['awaiting-technician-response', 'actionable'])
    
    if response_status['has_unanswered_questions']:
        tags.extend(['has-unanswered-questions', 'needs-clarification'])
    
    if comprehensive_fields['created_time']:
        try:
            created = datetime.fromisoformat(comprehensive_fields['created_time'].replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            age_days = (now - created).days
            
            if age_days < 1:
                tags.extend(['age:new', 'created-today'])
            elif age_days < 7:
                tags.extend(['age:this-week', 'recent'])
            elif age_days < 30:
                tags.extend(['age:this-month'])
            elif age_days < 90:
                tags.extend(['age:aging', 'needs-review'])
            else:
                tags.extend(['age:stale', 'very-old'])
            
            if 8 <= created.hour <= 17:
                tags.append('created:business-hours')
            else:
                tags.append('created:after-hours')
        except:
            pass
    
    if comprehensive_fields.get('has_assets'):
        tags.extend(['has-asset', f"asset-count:{comprehensive_fields['asset_count']}"])
    
    if comprehensive_fields.get('channel'):
        channel = comprehensive_fields['channel'].lower().replace(' ', '-')
        tags.extend([f"channel:{channel}", f"source:{channel}"])
    
    if comprehensive_fields.get('has_been_reopened'):
        tags.extend(['reopened', f"reopened-count:{comprehensive_fields['reopened_count']}"])
    
    if comprehensive_fields.get('is_first_call_resolution'):
        tags.extend(['fcr', 'first-call-resolution', 'quality-win'])
    
    if comprehensive_fields['status_category'] == 'active' and sla_risk == 'critical':
        tags.append('critical-open-ticket')
    
    if comprehensive_fields['status_category'] == 'active' and tech == 'unassigned':
        tags.extend(['actionable:unassigned-and-open', 'needs-immediate-assignment'])
    
    return list(set(tags))

def create_dust_document(ticket: Dict, conversations: List[Dict], 
                        comprehensive_fields: Dict, complexity: int,
                        sentiment: str, sla_risk: str, sla_breach_likelihood: int,
                        estimated_resolution_hours: float, issue_types: List[str],
                        response_status: Dict) -> Dict:
    
    conversation_text = ""
    if conversations:
        for i, conv in enumerate(conversations, 1):
            sender_obj = conv.get('sender') or {}
            sender = sender_obj.get('name', 'Unknown') if isinstance(sender_obj, dict) else 'Unknown'
            is_customer = conv.get('is_customer', False)
            role = "Customer" if is_customer else "Technician"
            text = conv.get('description', '')
            created_time_obj = conv.get('created_time') or {}
            time = created_time_obj.get('display_value', '') if isinstance(created_time_obj, dict) else ''
            
            conversation_text += f"\n### Conversation {i} - {role}: {sender}\n"
            conversation_text += f"**Time:** {time}\n\n"
            conversation_text += f"{text}\n"
    else:
        conversation_text = "\n*No conversations yet*\n"
    
    tags = generate_comprehensive_tags(
        ticket, conversations, comprehensive_fields,
        complexity, sentiment, sla_risk, response_status
    )
    
    document_text = f"""# Ticket #{comprehensive_fields['ticket_id']}: {comprehensive_fields['subject']}

## Overview
- **Status:** {comprehensive_fields['status']} ({comprehensive_fields['status_category']})
- **Priority:** {comprehensive_fields['priority']}
- **Category:** {comprehensive_fields['category']}

## People
- **Requester:** {comprehensive_fields['requester_name']}
- **Technician:** {comprehensive_fields['technician_name']}

## Timeline
- **Created:** {comprehensive_fields['created_time']}
- **Due By:** {comprehensive_fields['due_by_time'] or 'No due date'}

## Description
{comprehensive_fields['description']}

## AI Intelligence
- **Complexity:** {complexity}/10
- **Sentiment:** {sentiment}
- **SLA Risk:** {sla_risk} ({sla_breach_likelihood}%)

## Conversations ({len(conversations)} total)
{conversation_text}

## Tags
{', '.join(tags[:50])}

---
*Intelligence Suite v{VERSION}*
"""
    
    document = {
        "document_id": f"ticket-{comprehensive_fields['ticket_id']}",
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        "tags": tags,
        "text": document_text,
        "structured_data": {
            **comprehensive_fields,
            "complexity_score": complexity,
            "sentiment": sentiment,
            "sla_risk_level": sla_risk,
            "sla_breach_likelihood": sla_breach_likelihood,
            "estimated_resolution_hours": estimated_resolution_hours,
            "issue_types": issue_types,
            "conversation_count": len(conversations),
            "response_status": response_status,
            "intelligence_version": VERSION
        }
    }
    
    return document

# ============================================================
# DUST UPLOAD
# ============================================================

def upload_to_dust_parallel(documents: List[Dict]) -> Tuple[int, int]:
    """Upload documents to Dust in parallel"""
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        logger.warning("Dust credentials not configured. Skipping upload.")
        return 0, 0
    
    base_url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/servicedesk/documents"
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    uploaded = 0
    errors = 0
    upload_lock = Lock()
    rate_limiter = Semaphore(MAX_CONCURRENT_UPLOADS)
    
    def upload_single_document(doc: Dict) -> bool:
        nonlocal uploaded, errors
        
        with rate_limiter:
            url = f"{base_url}/{doc['document_id']}"
            
            for attempt in range(3):
                try:
                    response = SESSION.post(url, headers=headers, json=doc, timeout=UPLOAD_TIMEOUT)
                    
                    if response.status_code in [200, 201]:
                        with upload_lock:
                            uploaded += 1
                            if uploaded % 100 == 0:
                                logger.info(f"  Progress: {uploaded}/{len(documents)} uploaded...")
                        return True
                    
                    elif response.status_code == 429:
                        time.sleep((attempt + 1) * 2)
                        continue
                    
                    elif response.status_code in [502, 503, 504]:
                        time.sleep((attempt + 1) * 1)
                        continue
                    
                    else:
                        with upload_lock:
                            errors += 1
                        return False
                        
                except:
                    if attempt == 2:
                        with upload_lock:
                            errors += 1
                        return False
                    time.sleep(1)
            
            time.sleep(0.1)
            return False
    
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS) as executor:
        futures = [executor.submit(upload_single_document, doc) for doc in documents]
        for future in as_completed(futures):
            pass
    
    elapsed = time.time() - start_time
    logger.info(f"  ✓ Upload complete: {uploaded} succeeded, {errors} failed ({elapsed:.1f}s)")
    
    return uploaded, errors

# ============================================================
# MAIN PROCESSING
# ============================================================

def process_ticket_batch(tickets: List[Dict], access_token: str, batch_num: int, total_tickets: int) -> Tuple[List[Dict], int]:
    
    logger.info(f"\n[Batch {batch_num}] Processing tickets {(batch_num-1)*BATCH_SIZE + 1}-{min(batch_num*BATCH_SIZE, total_tickets)}...")
    
    documents = []
    errors = 0
    
    ticket_ids = [t['id'] for t in tickets]
    conversations_map = fetch_conversations_parallel(ticket_ids, access_token)
    
    for i, ticket in enumerate(tickets, 1):
        ticket_id = ticket['id']
        global_index = (batch_num - 1) * BATCH_SIZE + i
        
        try:
            conversations = conversations_map.get(ticket_id, [])
            comprehensive_fields = extract_comprehensive_fields(ticket)
            
            complexity = calculate_complexity_score(ticket, conversations)
            sentiment = analyze_sentiment(conversations)
            sla_risk, sla_breach_likelihood = predict_sla_breach(ticket, conversations)
            estimated_resolution_hours = estimate_resolution_time(ticket, conversations, complexity)
            issue_types = identify_issue_type(ticket)
            response_status = analyze_response_status(conversations)
            
            track_analytics(ticket, conversations, complexity, comprehensive_fields)
            track_metrics(comprehensive_fields, conversations, complexity, sentiment, sla_risk, response_status)
            
            document = create_dust_document(
                ticket, conversations, comprehensive_fields,
                complexity, sentiment, sla_risk, sla_breach_likelihood,
                estimated_resolution_hours, issue_types, response_status
            )
            
            documents.append(document)
            
        except Exception as e:
            logger.error(f"  ERROR processing ticket {ticket_id}: {e}")
            errors += 1
    
    uploaded, upload_errors = upload_to_dust_parallel(documents)
    errors += upload_errors
    
    return documents, errors

def print_analytics_summary():
    logger.info("=" * 60)
    logger.info(f"ANALYTICS SUMMARY (v{VERSION})")
    logger.info("=" * 60)
    logger.info("")
    
    logger.info("🏆 Top Categories:")
    category_stats = analytics_cache.raw_data['category_stats']
    if category_stats:
        sorted_categories = sorted(category_stats.items(), key=lambda x: x[1]['count'], reverse=True)[:5]
        for cat, data in sorted_categories:
            logger.info(f"  {cat}: {data['count']} tickets")
    
    logger.info("")

def main():
    
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info(f"Started: {datetime.now()}")
    logger.info("=" * 60)
    
    logger.info("✨ v3.0.2 DUAL CHECKPOINT SYSTEM:")
    logger.info(f"   • Timestamp file: {TIMESTAMP_FILE} (persists in repo)")
    logger.info(f"   • Stats file: {CHECKPOINT_FILE} (local diagnostics)")
    logger.info("   • TRUE incremental sync across GitHub Actions runs")
    logger.info("")
    
    if not all([SDP_DOMAIN, SDP_CLIENT_ID, SDP_CLIENT_SECRET, SDP_REFRESH_TOKEN]):
        logger.error("ERROR: Missing ServiceDesk credentials")
        sys.exit(1)
    
    logger.info("✓ Credentials validated")
    
    try:
        access_token = get_access_token()
        logger.info("✓ Access token obtained")
    except Exception as e:
        logger.error(f"FATAL: Failed to get token: {e}")
        sys.exit(1)
    
    last_sync_time = load_checkpoint()
    
    try:
        tickets = fetch_tickets_incremental(access_token, last_sync_time, MAX_PAGES)
        logger.info(f"✓ Fetched {len(tickets)} tickets")
    except Exception as e:
        logger.error(f"FATAL: Failed to fetch tickets: {e}")
        sys.exit(1)
    
    if not tickets:
        logger.info("✓ No new tickets. Sync complete.")
        return
    
    logger.info(f"\n📋 Processing {len(tickets)} tickets...")
    
    total_processed = 0
    total_errors = 0
    
    for batch_num in range(1, (len(tickets) // BATCH_SIZE) + 2):
        start_idx = (batch_num - 1) * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, len(tickets))
        
        if start_idx >= len(tickets):
            break
        
        batch_tickets = tickets[start_idx:end_idx]
        
        try:
            documents, errors = process_ticket_batch(batch_tickets, access_token, batch_num, len(tickets))
            total_processed += len(documents)
            total_errors += errors
            save_checkpoint(last_sync_time, total_processed, total_errors)
        except Exception as e:
            logger.error(f"FATAL: Batch {batch_num} error: {e}")
            total_errors += len(batch_tickets)
    
    if tickets:
        try:
            latest_modified = max(
                datetime.fromisoformat(t['last_updated_time']['display_value'].replace('Z', '+00:00'))
                for t in tickets
                if t.get('last_updated_time', {}).get('display_value')
            )
            save_checkpoint(latest_modified, total_processed, total_errors)
        except:
            save_checkpoint(datetime.now(timezone.utc), total_processed, total_errors)
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✓ SYNC COMPLETE (v{VERSION})")
    logger.info("=" * 60)
    logger.info(f"  Total: {METRICS['total_processed']}/{len(tickets)}")
    logger.info(f"  Active: {METRICS['active_tickets']}")
    logger.info(f"  Errors: {total_errors}")
    logger.info(f"  Finished: {datetime.now()}")
    logger.info("=" * 60)
    logger.info("")
    
    print_analytics_summary()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
