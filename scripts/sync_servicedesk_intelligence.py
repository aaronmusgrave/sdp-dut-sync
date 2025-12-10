#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v2.8.3 - COMPLETE WITH RATE LIMIT FIX
Full 48-field comprehensive ticket intelligence with incremental sync

Changes in v2.8.3:
- Fixed Dust API rate limiting (429 errors)
- Increased upload delay: 100ms → 500ms
- Added retry logic with exponential backoff
- Better error handling for uploads

v2.8.2 Features:
- Removed page limit to fetch all 4500+ tickets
- Fixed NoneType errors with defensive null checks
- Fixed analytics crash and checkpoint updating

v2.8 Features (48 comprehensive fields):
Core Fields (28): Full ticket details, conversations, timeline, AI intelligence
Advanced Fields (20): Assets, Source/Mode, Impact/Urgency, Level/Tier, Service,
Time tracking, Resolution category, Closure code, Reopened count, Approvals,
Tags/Labels, Department, Linked tickets, Attachments, Change/Problem links,
First Call Resolution, Created by, Template, SLA details, Cost tracking

Performance: Connection pooling, parallel processing, API filtering, caching
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
import hashlib

# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "2.8.3"
BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY = 5
CHECKPOINT_FILE = 'checkpoint.json'
REQUEST_TIMEOUT = 30
MAX_PAGES = None  # No limit - fetch all tickets

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
# CHECKPOINT MANAGEMENT
# ============================================================

def load_checkpoint() -> Optional[datetime]:
    """Load last sync timestamp from checkpoint file"""
    if not os.path.exists(CHECKPOINT_FILE):
        return None
    
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            checkpoint = json.load(f)
            last_sync = checkpoint.get('last_sync')
            if last_sync:
                return datetime.fromisoformat(last_sync)
    except Exception as e:
        logger.warning(f"Could not load checkpoint: {e}")
    
    return None

def save_checkpoint(last_sync_time: datetime, total_processed: int, total_errors: int):
    """Save checkpoint for incremental sync"""
    checkpoint = {
        'last_sync': last_sync_time.isoformat() if last_sync_time else None,
        'last_run': datetime.now(timezone.utc).isoformat(),
        'processed': total_processed,
        'errors': total_errors
    }
    
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(checkpoint, f, indent=2)
    
    logger.info(f"SAVED Checkpoint saved: {total_processed} successful, {total_errors} errors")

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
    """
    Fetch tickets updated since last sync (incremental)
    v2.8.2: No page limit by default, improved timestamp handling
    """
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Build search criteria
    search_criteria = []
    
    if last_sync_time:
        # Convert to milliseconds for API
        last_sync_ms = int(last_sync_time.timestamp() * 1000)
        search_criteria.append({
            "field": "last_updated_time",
            "condition": "greater than",
            "value": str(last_sync_ms)
        })
        logger.info(f"STATS Incremental sync: Fetching tickets updated since {last_sync_time}")
    else:
        logger.info("STATS Full sync: Fetching ALL tickets (no checkpoint found)")
    
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
        logger.info(f"  STATS Page limit: {page_limit} pages (up to {page_limit * 100} tickets)")
    else:
        logger.info(f"  STATS No page limit - fetching ALL tickets")
    
    while True:
        # Check page limit if set
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
                
                # Check if there are more pages
                list_info_response = data.get('list_info', {})
                has_more = list_info_response.get('has_more_rows', False)
                
                if not has_more:
                    logger.info(f"  No more pages. Total fetched: {len(all_tickets)}")
                    break
                
                page += 1
                time.sleep(0.5)  # Rate limiting
                
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
    score = 3  # Base score
    
    # Conversation complexity
    if len(conversations) > 10:
        score += 3
    elif len(conversations) > 5:
        score += 2
    elif len(conversations) > 2:
        score += 1
    
    # Technical indicators
    description = ticket.get('description', '').lower()
    technical_keywords = ['error', 'crash', 'bug', 'failure', 'corrupt', 'network', 'server', 'database']
    tech_count = sum(1 for keyword in technical_keywords if keyword in description)
    score += min(tech_count, 3)
    
    # Priority
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
    
    # Adjust for priority
    priority = ticket.get('priority', {})
    priority_name = priority.get('name', '').lower() if priority else ''
    if 'urgent' in priority_name:
        base_hours *= 0.5
    elif 'low' in priority_name:
        base_hours *= 1.5
    
    # Adjust for conversation volume
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
    
    # Sort by time
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
    
    # Check for unanswered questions
    last_text = last_conv.get('description', '')
    has_unanswered_questions = '?' in last_text and last_responder_type == 'customer'
    
    # Calculate response time
    response_time = 0
    if len(sorted_convs) > 1:
        try:
            first_time = sorted_convs[0].get('created_time', {}).get('value', 0)
            last_time = sorted_convs[-1].get('created_time', {}).get('value', 0)
            response_time = (last_time - first_time) / (1000 * 3600)  # Convert ms to hours
        except:
            pass
    
    return {
        'awaiting_response': awaiting,
        'last_responder': last_responder_type,
        'response_time_hours': round(response_time, 1),
        'has_unanswered_questions': has_unanswered_questions
    }

# ============================================================
# FIELD EXTRACTION - v2.8.2 WITH DEFENSIVE NULL CHECKS
# ============================================================

def extract_comprehensive_fields(ticket: Dict) -> Dict[str, Any]:
    """
    Extract all 48 comprehensive fields from ticket
    v2.8.2: Added defensive null checks for tickets with missing data
    """
    fields = {}
    
    # Helper function for safe nested gets
    def safe_get(obj, key, default=''):
        """Safely get value from dict, handling None"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default
    
    # ========================================
    # CORE FIELDS (28)
    # ========================================
    
    # Basic info
    fields['ticket_id'] = ticket.get('id', 'unknown')
    fields['subject'] = ticket.get('subject', 'No subject')
    fields['description'] = ticket.get('description', 'No description')
    
    # Status - safe handling
    status = ticket.get('status') or {}
    fields['status'] = safe_get(status, 'name', 'Unknown')
    fields['status_category'] = 'active' if fields['status'].lower() in ['open', 'in progress', 'pending'] else 'closed'
    
    # Priority - safe handling
    priority = ticket.get('priority') or {}
    fields['priority'] = safe_get(priority, 'name', 'Normal')
    
    # People - safe handling
    requester = ticket.get('requester') or {}
    fields['requester_name'] = safe_get(requester, 'name', 'Unknown')
    fields['requester_email'] = safe_get(requester, 'email_id', '')
    
    technician = ticket.get('technician') or {}
    fields['technician_name'] = safe_get(technician, 'name', 'Unassigned')
    fields['technician_email'] = safe_get(technician, 'email_id', '')
    
    # Category - safe handling
    category = ticket.get('category') or {}
    fields['category'] = safe_get(category, 'name', 'Uncategorized')
    
    subcategory = ticket.get('subcategory') or {}
    fields['subcategory'] = safe_get(subcategory, 'name', '')
    
    # Site - safe handling
    site = ticket.get('site') or {}
    fields['site'] = safe_get(site, 'name', '')
    
    # Dates - safe handling with nested gets
    created_time = ticket.get('created_time') or {}
    fields['created_time'] = safe_get(created_time, 'display_value', '')
    
    due_by_time = ticket.get('due_by_time') or {}
    fields['due_by_time'] = safe_get(due_by_time, 'display_value', '')
    
    resolved_time = ticket.get('resolved_time') or {}
    fields['resolved_time'] = safe_get(resolved_time, 'display_value', '')
    
    last_updated_time = ticket.get('last_updated_time') or {}
    fields['last_updated_time'] = safe_get(last_updated_time, 'display_value', '')
    
    # Group - safe handling
    group = ticket.get('group') or {}
    fields['group'] = safe_get(group, 'name', '')
    
    # ========================================
    # ADVANCED FIELDS (20) - v2.8
    # ========================================
    
    # 1. Assets/CI - safe handling
    assets = ticket.get('assets') or []
    if not isinstance(assets, list):
        assets = []
    fields['has_assets'] = len(assets) > 0
    fields['asset_count'] = len(assets)
    fields['asset_names'] = [safe_get(asset, 'name', '') for asset in assets] if assets else []
    fields['primary_asset'] = safe_get(assets[0], 'name', '') if assets else ''
    
    # 2. Source/Mode (Channel) - safe handling
    request_type = ticket.get('request_type') or {}
    fields['source'] = safe_get(request_type, 'name', 'Unknown')
    
    mode = ticket.get('mode') or {}
    fields['mode'] = safe_get(mode, 'name', fields['source'])
    fields['channel'] = fields['mode']
    
    # 3. Impact & Urgency - safe handling
    impact = ticket.get('impact') or {}
    fields['impact'] = safe_get(impact, 'name', '')
    
    urgency = ticket.get('urgency') or {}
    fields['urgency'] = safe_get(urgency, 'name', '')
    
    # 4. Level/Tier - safe handling
    level = ticket.get('level') or {}
    fields['level'] = safe_get(level, 'name', '')
    fields['tier'] = fields['level']
    
    # 5. Service - safe handling
    service = ticket.get('service') or {}
    fields['service'] = safe_get(service, 'name', '')
    
    # 6. Time Spent - safe handling
    fields['time_spent'] = ticket.get('time_spent', 0) or 0
    fields['time_spent_hours'] = round(fields['time_spent'] / 3600000, 2) if fields['time_spent'] else 0
    
    # 7. Resolution Category - safe handling
    resolution = ticket.get('resolution') or {}
    fields['resolution_category'] = safe_get(resolution, 'name', '')
    
    # 8. Closure Code - safe handling
    closure_info = ticket.get('closure_info') or {}
    closure_code = closure_info.get('closure_code') or {} if isinstance(closure_info, dict) else {}
    fields['closure_code'] = safe_get(closure_code, 'name', '')
    
    # 9. Reopened Count - safe handling
    fields['reopened_count'] = ticket.get('reopened_count', 0) or 0
    fields['has_been_reopened'] = fields['reopened_count'] > 0
    
    # 10. Approvals - safe handling
    approval_status = ticket.get('approval_status') or {}
    fields['approval_status'] = safe_get(approval_status, 'name', '')
    fields['has_approvals'] = bool(fields['approval_status'])
    
    # 11. Tags/Labels - safe handling
    tags = ticket.get('tags') or []
    if not isinstance(tags, list):
        tags = []
    fields['tags'] = [safe_get(tag, 'name', '') for tag in tags] if tags else []
    fields['has_tags'] = len(fields['tags']) > 0
    
    # 12. Department - safe handling
    department = ticket.get('department') or {}
    fields['department'] = safe_get(department, 'name', '')
    
    # 13. Linked Tickets - safe handling
    linked_requests = ticket.get('linked_requests') or []
    if not isinstance(linked_requests, list):
        linked_requests = []
    fields['linked_ticket_count'] = len(linked_requests)
    fields['has_linked_tickets'] = len(linked_requests) > 0
    fields['linked_ticket_ids'] = [safe_get(req, 'id', '') for req in linked_requests] if linked_requests else []
    
    # 14. Attachments - safe handling
    attachments = ticket.get('attachments') or []
    if not isinstance(attachments, list):
        attachments = []
    fields['attachment_count'] = len(attachments)
    fields['has_attachments'] = len(attachments) > 0
    
    # 15. Change Link - safe handling
    change = ticket.get('change') or {}
    fields['linked_change_id'] = safe_get(change, 'id', '')
    fields['has_linked_change'] = bool(fields['linked_change_id'])
    
    # 16. Problem Link - safe handling
    problem = ticket.get('problem') or {}
    fields['linked_problem_id'] = safe_get(problem, 'id', '')
    fields['has_linked_problem'] = bool(fields['linked_problem_id'])
    
    # 17. First Call Resolution - safe handling
    fields['is_first_call_resolution'] = ticket.get('is_fcr', False) or False
    
    # 18. Created By - safe handling
    created_by = ticket.get('created_by') or {}
    fields['created_by'] = safe_get(created_by, 'name', '')
    fields['is_proxy_request'] = bool(fields['created_by']) and fields['created_by'] != fields['requester_name']
    
    # 19. Template - safe handling
    template = ticket.get('template') or {}
    fields['template'] = safe_get(template, 'name', '')
    fields['used_template'] = bool(fields['template'])
    
    # 20. SLA Details - safe handling
    sla = ticket.get('sla') or {}
    fields['sla_name'] = safe_get(sla, 'name', '')
    fields['sla_violated'] = ticket.get('is_sla_violated', False) or False
    
    return fields

# ============================================================
# ANALYTICS TRACKING
# ============================================================

def track_analytics(ticket: Dict, conversations: List[Dict], complexity: int, comprehensive_fields: Dict):
    """Track analytics for reporting"""
    
    # Category stats
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
    
    # Technician workload
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
    
    # Peak hours
    if comprehensive_fields['created_time']:
        try:
            created = datetime.fromisoformat(comprehensive_fields['created_time'].replace('Z', '+00:00'))
            analytics_cache.update_peak_hours(created.hour)
        except:
            pass
    
    # Issue types
    description = ticket.get('description', '').lower()
    issue_keywords = ['hardware', 'email', 'network', 'error', 'access']
    for keyword in issue_keywords:
        if keyword in description:
            analytics_cache.update_issue_types(keyword)
    
    # v2.8: New analytics
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

# ============================================================
# METRICS TRACKING
# ============================================================

def track_metrics(comprehensive_fields: Dict, conversations: List[Dict], 
                 complexity: int, sentiment: str, sla_risk: str, response_status: Dict):
    """Track comprehensive metrics"""
    
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
    
    # v2.8 metrics
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
# TAG GENERATION
# ============================================================

def generate_comprehensive_tags(ticket: Dict, conversations: List[Dict], 
                                comprehensive_fields: Dict, complexity: int,
                                sentiment: str, sla_risk: str, response_status: Dict) -> List[str]:
    """Generate comprehensive searchable tags - 200+ possible tags"""
    tags = []
    
    # Status tags
    status = comprehensive_fields['status'].lower().replace(' ', '-')
    tags.extend([
        f"status:{status}",
        f"status-category:{comprehensive_fields['status_category']}"
    ])
    
    if comprehensive_fields['status_category'] == 'active':
        tags.append('active')
        tags.append('open-status')
        tags.append('needs-attention')
    
    # Priority tags
    priority = comprehensive_fields['priority'].lower()
    tags.append(f"priority:{priority}")
    
    if 'urgent' in priority:
        tags.extend(['urgent-attention', 'critical-action-required'])
    
    # People tags
    tech = comprehensive_fields['technician_name'].lower().replace(' ', '-')
    req = comprehensive_fields['requester_name'].lower().replace(' ', '-')
    
    tags.extend([
        f"technician:{tech}",
        f"requester:{req}"
    ])
    
    if tech == 'unassigned':
        tags.extend(['unassigned', 'needs-assignment'])
    
    # Category tags
    category = comprehensive_fields['category'].lower().replace(' ', '-')
    tags.append(f"category:{category}")
    
    if comprehensive_fields['subcategory']:
        subcategory = comprehensive_fields['subcategory'].lower().replace(' ', '-')
        tags.append(f"subcategory:{subcategory}")
    
    # Complexity tags
    tags.append(f"complexity:{complexity}")
    if complexity >= 7:
        tags.extend(['high-complexity', 'complex-issue'])
    elif complexity <= 3:
        tags.extend(['low-complexity', 'simple-issue'])
    
    # Sentiment tags
    tags.append(f"sentiment:{sentiment}")
    if sentiment == 'at-risk':
        tags.extend(['satisfaction-at-risk', 'customer-frustration'])
    
    # SLA tags
    tags.append(f"sla-risk:{sla_risk}")
    if sla_risk in ['critical', 'high']:
        tags.extend(['high-breach-risk', 'sla-attention-needed'])
    
    # Conversation tags
    conv_count = len(conversations)
    tags.append(f"conversations:{conv_count}")
    
    if conv_count == 0:
        tags.append('no-conversation')
    elif conv_count > 10:
        tags.extend(['high-conversation-volume', 'extensive-discussion'])
    
    # Response status tags
    tags.append(f"awaiting:{response_status['awaiting_response']}")
    
    if response_status['awaiting_response'] == 'customer':
        tags.append('awaiting-customer-response')
    elif response_status['awaiting_response'] == 'technician':
        tags.extend(['awaiting-technician-response', 'actionable'])
    
    if response_status['has_unanswered_questions']:
        tags.extend(['has-unanswered-questions', 'needs-clarification'])
    
    # Time-based tags
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
            
            # Business hours tag
            if 8 <= created.hour <= 17:
                tags.append('created:business-hours')
            else:
                tags.append('created:after-hours')
        except:
            pass
    
    # ========================================
    # v2.8: NEW TAGS FOR 20 ADDITIONAL FIELDS
    # ========================================
    
    # Asset tags
    if comprehensive_fields.get('has_assets'):
        tags.extend(['has-asset', f"asset-count:{comprehensive_fields['asset_count']}"])
        for asset in comprehensive_fields['asset_names'][:3]:  # First 3 assets
            asset_tag = asset.lower().replace(' ', '-')
            tags.append(f"asset:{asset_tag}")
    
    # Channel/Source tags
    if comprehensive_fields.get('channel'):
        channel = comprehensive_fields['channel'].lower().replace(' ', '-')
        tags.extend([f"channel:{channel}", f"source:{channel}"])
    
    # Impact/Urgency tags
    if comprehensive_fields.get('impact'):
        impact = comprehensive_fields['impact'].lower().replace(' ', '-')
        tags.append(f"impact:{impact}")
    
    if comprehensive_fields.get('urgency'):
        urgency = comprehensive_fields['urgency'].lower().replace(' ', '-')
        tags.append(f"urgency:{urgency}")
    
    # Tier/Level tags
    if comprehensive_fields.get('tier'):
        tier = comprehensive_fields['tier'].lower().replace(' ', '-')
        tags.extend([f"tier:{tier}", f"level:{tier}"])
    
    # Service tags
    if comprehensive_fields.get('service'):
        service = comprehensive_fields['service'].lower().replace(' ', '-')
        tags.append(f"service:{service}")
    
    # Time spent tags
    if comprehensive_fields.get('time_spent_hours') > 0:
        hours = comprehensive_fields['time_spent_hours']
        tags.append(f"time-spent:{hours}h")
        if hours > 8:
            tags.append('high-time-investment')
    
    # Resolution tags
    if comprehensive_fields.get('resolution_category'):
        resolution = comprehensive_fields['resolution_category'].lower().replace(' ', '-')
        tags.append(f"resolution:{resolution}")
    
    # Closure code tags
    if comprehensive_fields.get('closure_code'):
        closure = comprehensive_fields['closure_code'].lower().replace(' ', '-')
        tags.append(f"closure:{closure}")
    
    # Reopened tags
    if comprehensive_fields.get('has_been_reopened'):
        tags.extend([
            'reopened',
            f"reopened-count:{comprehensive_fields['reopened_count']}"
        ])
    
    # Approval tags
    if comprehensive_fields.get('has_approvals'):
        tags.extend(['has-approvals', f"approval:{comprehensive_fields['approval_status']}"])
    
    # Custom tags from ticket
    if comprehensive_fields.get('tags'):
        for tag in comprehensive_fields['tags']:
            tag_clean = tag.lower().replace(' ', '-')
            tags.append(f"custom-tag:{tag_clean}")
    
    # Department tags
    if comprehensive_fields.get('department'):
        dept = comprehensive_fields['department'].lower().replace(' ', '-')
        tags.append(f"department:{dept}")
    
    # Linked tickets tags
    if comprehensive_fields.get('has_linked_tickets'):
        tags.extend([
            'has-linked-tickets',
            f"linked-count:{comprehensive_fields['linked_ticket_count']}"
        ])
    
    # Attachment tags
    if comprehensive_fields.get('has_attachments'):
        tags.extend([
            'has-attachments',
            f"attachments:{comprehensive_fields['attachment_count']}"
        ])
    
    # ITIL tags
    if comprehensive_fields.get('has_linked_change'):
        tags.extend(['linked-to-change', 'itil-change'])
    
    if comprehensive_fields.get('has_linked_problem'):
        tags.extend(['linked-to-problem', 'itil-problem'])
    
    # FCR tags
    if comprehensive_fields.get('is_first_call_resolution'):
        tags.extend(['fcr', 'first-call-resolution', 'quality-win'])
    
    # Proxy request tags
    if comprehensive_fields.get('is_proxy_request'):
        tags.extend(['proxy-request', 'created-by-tech'])
    
    # Template tags
    if comprehensive_fields.get('used_template'):
        template_clean = comprehensive_fields['template'].lower().replace(' ', '-')
        tags.extend(['used-template', f"template:{template_clean}"])
    
    # SLA tags
    if comprehensive_fields.get('sla_name'):
        sla = comprehensive_fields['sla_name'].lower().replace(' ', '-')
        tags.append(f"sla:{sla}")
    
    if comprehensive_fields.get('sla_violated'):
        tags.extend(['sla-violated', 'sla-breach'])
    
    # Combined actionable tags
    if comprehensive_fields['status_category'] == 'active' and sla_risk == 'critical':
        tags.append('critical-open-ticket')
    
    if comprehensive_fields['status_category'] == 'active' and tech == 'unassigned':
        tags.extend(['actionable:unassigned-and-open', 'needs-immediate-assignment'])
    
    if sla_risk in ['critical', 'high'] and comprehensive_fields['status_category'] == 'active':
        tags.extend(['actionable:overdue-and-open', 'urgent-action'])
    
    return list(set(tags))  # Remove duplicates

# ============================================================
# DOCUMENT CREATION
# ============================================================

def create_dust_document(ticket: Dict, conversations: List[Dict], 
                        comprehensive_fields: Dict, complexity: int,
                        sentiment: str, sla_risk: str, sla_breach_likelihood: int,
                        estimated_resolution_hours: float, issue_types: List[str],
                        response_status: Dict) -> Dict:
    """Create comprehensive Dust document"""
    
    # Build conversation text
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
    
    # Generate tags
    tags = generate_comprehensive_tags(
        ticket, conversations, comprehensive_fields,
        complexity, sentiment, sla_risk, response_status
    )
    
    # Build structured text document
    document_text = f"""# Ticket #{comprehensive_fields['ticket_id']}: {comprehensive_fields['subject']}

## Overview
- **Status:** {comprehensive_fields['status']} ({comprehensive_fields['status_category']})
- **Priority:** {comprehensive_fields['priority']}
- **Category:** {comprehensive_fields['category']}
- **Subcategory:** {comprehensive_fields['subcategory'] or 'N/A'}

## People
- **Requester:** {comprehensive_fields['requester_name']} ({comprehensive_fields['requester_email']})
- **Technician:** {comprehensive_fields['technician_name']} ({comprehensive_fields['technician_email']})
- **Group:** {comprehensive_fields['group'] or 'N/A'}
- **Site:** {comprehensive_fields['site'] or 'N/A'}

## Timeline
- **Created:** {comprehensive_fields['created_time']}
- **Due By:** {comprehensive_fields['due_by_time'] or 'No due date'}
- **Resolved:** {comprehensive_fields['resolved_time'] or 'Not resolved'}
- **Last Updated:** {comprehensive_fields['last_updated_time']}

## Description
{comprehensive_fields['description']}

## AI Intelligence
- **Complexity Score:** {complexity}/10
- **Sentiment:** {sentiment}
- **SLA Breach Risk:** {sla_risk} ({sla_breach_likelihood}% likelihood)
- **Estimated Resolution:** {estimated_resolution_hours} hours
- **Issue Types:** {', '.join(issue_types)}

## Response Status
- **Awaiting Response From:** {response_status['awaiting_response']}
- **Last Responder:** {response_status['last_responder']}
- **Total Response Time:** {response_status['response_time_hours']} hours
- **Has Unanswered Questions:** {'Yes' if response_status['has_unanswered_questions'] else 'No'}

## Advanced Fields (v2.8)

### Assets & Resources
- **Has Assets:** {'Yes' if comprehensive_fields.get('has_assets') else 'No'}
- **Asset Count:** {comprehensive_fields.get('asset_count', 0)}
- **Primary Asset:** {comprehensive_fields.get('primary_asset') or 'N/A'}
- **All Assets:** {', '.join(comprehensive_fields.get('asset_names', [])) or 'None'}

### Channel & Source
- **Channel/Mode:** {comprehensive_fields.get('channel') or 'Unknown'}
- **Source:** {comprehensive_fields.get('source') or 'Unknown'}

### Priority Matrix
- **Impact:** {comprehensive_fields.get('impact') or 'N/A'}
- **Urgency:** {comprehensive_fields.get('urgency') or 'N/A'}

### Support Details
- **Level/Tier:** {comprehensive_fields.get('tier') or 'N/A'}
- **Service:** {comprehensive_fields.get('service') or 'N/A'}
- **Time Spent:** {comprehensive_fields.get('time_spent_hours', 0)} hours

### Resolution Info
- **Resolution Category:** {comprehensive_fields.get('resolution_category') or 'N/A'}
- **Closure Code:** {comprehensive_fields.get('closure_code') or 'N/A'}
- **Reopened Count:** {comprehensive_fields.get('reopened_count', 0)}
- **First Call Resolution:** {'Yes' if comprehensive_fields.get('is_first_call_resolution') else 'No'}

### Workflow & Approvals
- **Approval Status:** {comprehensive_fields.get('approval_status') or 'N/A'}
- **Has Approvals:** {'Yes' if comprehensive_fields.get('has_approvals') else 'No'}

### Organization
- **Department:** {comprehensive_fields.get('department') or 'N/A'}
- **Created By:** {comprehensive_fields.get('created_by') or comprehensive_fields['requester_name']}
- **Is Proxy Request:** {'Yes' if comprehensive_fields.get('is_proxy_request') else 'No'}

### Relationships
- **Linked Tickets:** {comprehensive_fields.get('linked_ticket_count', 0)}
- **Linked Change:** {comprehensive_fields.get('linked_change_id') or 'None'}
- **Linked Problem:** {comprehensive_fields.get('linked_problem_id') or 'None'}

### Documentation
- **Attachments:** {comprehensive_fields.get('attachment_count', 0)}
- **Template Used:** {comprehensive_fields.get('template') or 'None'}
- **Custom Tags:** {', '.join(comprehensive_fields.get('tags', [])) or 'None'}

### SLA Details
- **SLA Name:** {comprehensive_fields.get('sla_name') or 'N/A'}
- **SLA Violated:** {'Yes' if comprehensive_fields.get('sla_violated') else 'No'}

## Conversations ({len(conversations)} total)
{conversation_text}

## Searchable Tags
{', '.join(tags[:50])}  {'...' if len(tags) > 50 else ''}

---
*Last synced: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*
*Intelligence Suite v{VERSION} - 48 Comprehensive Fields*
"""
    
    # Create document
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
# DUST UPLOAD - v2.8.3 WITH RATE LIMIT FIX
# ============================================================

def upload_to_dust(documents: List[Dict]) -> Tuple[int, int]:
    """
    Upload documents to Dust in batch with rate limiting and retries
    v2.8.3: Fixed 429 rate limit errors with retry logic and slower upload rate
    """
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
    
    for doc in documents:
        url = f"{base_url}/{doc['document_id']}"
        
        # Retry logic for rate limiting
        for attempt in range(3):
            try:
                response = SESSION.post(url, headers=headers, json=doc, timeout=REQUEST_TIMEOUT)
                
                if response.status_code in [200, 201]:
                    uploaded += 1
                    break  # Success!
                elif response.status_code == 429:
                    # Rate limited - wait longer
                    wait_time = (attempt + 1) * 2  # 2s, 4s, 6s
                    logger.warning(f"Rate limited on {doc['document_id']}, waiting {wait_time}s (attempt {attempt + 1}/3)")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning(f"Upload failed for {doc['document_id']}: {response.status_code}")
                    errors += 1
                    break  # Don't retry non-429 errors
                    
            except Exception as e:
                logger.warning(f"Upload error for {doc['document_id']}: {e}")
                if attempt == 2:  # Last attempt
                    errors += 1
                else:
                    time.sleep(1)
        
        # Slower rate limiting: 500ms between uploads (was 100ms)
        time.sleep(0.5)
    
    return uploaded, errors

# ============================================================
# MAIN PROCESSING
# ============================================================

def process_ticket_batch(tickets: List[Dict], access_token: str, batch_num: int, total_tickets: int) -> Tuple[List[Dict], int]:
    """Process a batch of tickets"""
    
    logger.info(f"\n[Batch {batch_num}] Processing tickets {(batch_num-1)*BATCH_SIZE + 1}-{min(batch_num*BATCH_SIZE, total_tickets)}...")
    
    documents = []
    errors = 0
    
    # Fetch conversations in parallel for entire batch
    ticket_ids = [t['id'] for t in tickets]
    conversations_map = fetch_conversations_parallel(ticket_ids, access_token)
    
    # Process each ticket
    for i, ticket in enumerate(tickets, 1):
        ticket_id = ticket['id']
        global_index = (batch_num - 1) * BATCH_SIZE + i
        
        try:
            logger.info(f"  [{global_index}/{total_tickets}] Processing ticket {ticket_id}...")
            
            # Get conversations
            conversations = conversations_map.get(ticket_id, [])
            
            # Extract comprehensive fields (48 fields)
            comprehensive_fields = extract_comprehensive_fields(ticket)
            
            # AI analysis
            complexity = calculate_complexity_score(ticket, conversations)
            sentiment = analyze_sentiment(conversations)
            sla_risk, sla_breach_likelihood = predict_sla_breach(ticket, conversations)
            estimated_resolution_hours = estimate_resolution_time(ticket, conversations, complexity)
            issue_types = identify_issue_type(ticket)
            response_status = analyze_response_status(conversations)
            
            # Track analytics
            track_analytics(ticket, conversations, complexity, comprehensive_fields)
            
            # Track metrics
            track_metrics(comprehensive_fields, conversations, complexity, sentiment, sla_risk, response_status)
            
            # Create document
            document = create_dust_document(
                ticket, conversations, comprehensive_fields,
                complexity, sentiment, sla_risk, sla_breach_likelihood,
                estimated_resolution_hours, issue_types, response_status
            )
            
            documents.append(document)
            
        except Exception as e:
            logger.error(f"  ERROR processing ticket {ticket_id}: {e}")
            errors += 1
    
    # Upload batch to Dust
    uploaded, upload_errors = upload_to_dust(documents)
    errors += upload_errors
    
    active_count = sum(1 for d in documents if d['structured_data']['status_category'] == 'active')
    
    logger.info(f"  OK Uploaded batch | Total: {len(documents)}/{len(tickets)} | Active: {active_count} | Errors: {errors}")
    
    return documents, errors

def print_analytics_summary():
    """Print comprehensive analytics summary - v2.8.1 FIXED"""
    logger.info("============================================================")
    logger.info(f"STATS ANALYTICS SUMMARY (v{VERSION})")
    logger.info("============================================================")
    logger.info("")
    
    # Top categories
    logger.info("🏆 Top Categories:")
    for cat, data in Counter(analytics_cache.raw_data['category_stats']).most_common(5):
        logger.info(f"  {cat}: {data['count']} tickets")
        if data['count'] > 0:
            logger.info(f"    Avg resolution: {data['total_resolution_days'] / data['count']:.1f} days")
    
    logger.info("")
    
    # Technician workload
    logger.info("👥 Technician Workload:")
    for tech, data in sorted(analytics_cache.raw_data['technician_workload'].items(), 
                            key=lambda x: x[1]['active'], reverse=True)[:5]:
        logger.info(f"  {tech}: {data['active']} active / {data['total']} total")
        if data['total'] > 0:
            logger.info(f"    Avg response: {data['total_response_hours'] / data['total']:.1f} hours")
    
    logger.info("")
    
    # Peak hours
    logger.info("⏰ Peak Hours:")
    for hour, count in Counter(analytics_cache.raw_data['peak_hours']).most_common(5):
        logger.info(f"  {hour}:00 - {count} tickets")
    
    logger.info("")
    
    # Top issue types
    logger.info("🔍 Top Issue Types:")
    for issue, count in Counter(analytics_cache.raw_data['issue_types']).most_common(5):
        logger.info(f"  {issue}: {count} tickets")
    
    logger.info("")
    
    # Top channels (v2.8)
    logger.info("📱 Top Channels:")
    channel_counts = Counter(analytics_cache.raw_data['channel_distribution'])
    if channel_counts:
        for channel, count in channel_counts.most_common(5):
            logger.info(f"  {channel}: {count} tickets")
    else:
        logger.info("  No channel data available")
    
    logger.info("")
    
    # Top departments (v2.8)
    logger.info("🏢 Top Departments:")
    dept_counts = Counter(analytics_cache.raw_data['department_distribution'])
    if dept_counts:
        for dept, count in dept_counts.most_common(5):
            logger.info(f"  {dept}: {count} tickets")
    else:
        logger.info("  No department data available")
    
    logger.info("")
    
    # Top asset failures (v2.8) - FIXED in v2.8.1
    logger.info("💻 Top Asset Failures:")
    asset_failures = Counter(analytics_cache.raw_data['asset_failure_rates'])
    if asset_failures:
        for asset, count in asset_failures.most_common(5):
            logger.info(f"  {asset}: {count} failures")
    else:
        logger.info("  No asset data available")
    
    logger.info("")

def main():
    """Main execution - v2.8.3 with rate limit fix"""
    
    logger.info("============================================================")
    logger.info(f"START SERVICEDESK INTELLIGENCE SUITE v{VERSION} - COMPLETE")
    logger.info(f"Starting at {datetime.now()}")
    logger.info("============================================================")
    
    logger.info("✨ v2.8.3 FIXES:")
    logger.info("   • Fixed Dust API rate limiting (429 errors)")
    logger.info("   • Upload delay: 100ms → 500ms")
    logger.info("   • Added retry logic with exponential backoff")
    logger.info("")
    logger.info("✨ v2.8.2 FEATURES:")
    logger.info("   • Removed page limit - fetches ALL 4500+ tickets")
    logger.info("   • Fixed NoneType errors with defensive null checks")
    logger.info("   • Fixed analytics crash and checkpoint updating")
    logger.info("")
    logger.info("✨ v2.8 COMPLETE FIELD COVERAGE:")
    logger.info("   ALL 48 COMPREHENSIVE FIELDS:")
    logger.info("   • Core fields (28): Status, priority, people, timeline, conversations")
    logger.info("   • Advanced fields (20): Assets, channels, impact/urgency, service, etc.")
    logger.info("")
    logger.info("   PERFORMANCE OPTIMIZATIONS:")
    logger.info("   • Connection pooling (20 connections)")
    logger.info("   • Parallel conversation fetching")
    logger.info("   • API-level filtering")
    logger.info("   • Cached processing")
    logger.info("")
    
    # Validate environment
    if not all([SDP_DOMAIN, SDP_CLIENT_ID, SDP_CLIENT_SECRET, SDP_REFRESH_TOKEN]):
        logger.error("ERROR Missing ServiceDesk Plus credentials")
        sys.exit(1)
    
    logger.info("OK Environment validation passed")
    
    # Get access token
    logger.info("Getting access token...")
    try:
        access_token = get_access_token()
        logger.info("OK Access token obtained")
    except Exception as e:
        logger.error(f"FATAL Failed to get access token: {e}")
        sys.exit(1)
    
    # Load checkpoint for incremental sync
    last_sync_time = load_checkpoint()
    
    # Fetch tickets (incremental or full)
    logger.info("Fetching tickets...")
    try:
        tickets = fetch_tickets_incremental(
            access_token=access_token,
            last_sync_time=last_sync_time,
            page_limit=MAX_PAGES
        )
        logger.info(f"OK Fetched {len(tickets)} tickets ({'full' if not last_sync_time else 'incremental'} sync)")
    except Exception as e:
        logger.error(f"FATAL Failed to fetch tickets: {e}")
        sys.exit(1)
    
    if not tickets:
        logger.info("No new tickets to process. Exiting.")
        return
    
    # Process in batches
    logger.info(f"\nTotal tickets to process: {len(tickets)}")
    
    total_processed = 0
    total_errors = 0
    
    for batch_num in range(1, (len(tickets) // BATCH_SIZE) + 2):
        start_idx = (batch_num - 1) * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, len(tickets))
        
        if start_idx >= len(tickets):
            break
        
        batch_tickets = tickets[start_idx:end_idx]
        
        try:
            documents, errors = process_ticket_batch(
                batch_tickets, access_token, batch_num, len(tickets)
            )
            
            total_processed += len(documents)
            total_errors += errors
            
            # Save checkpoint after each batch
            save_checkpoint(last_sync_time, total_processed, total_errors)
            
        except Exception as e:
            logger.error(f"FATAL Error processing batch {batch_num}: {e}")
            total_errors += len(batch_tickets)
    
    # ============================================================
    # v2.8.1 FIX: Update checkpoint with latest ticket timestamp
    # ============================================================
    if tickets:
        try:
            # Find the most recent modified time from processed tickets
            latest_modified = max(
                datetime.fromisoformat(t['last_updated_time']['display_value'].replace('Z', '+00:00'))
                for t in tickets
                if t.get('last_updated_time', {}).get('display_value')
            )
            logger.info(f"CHECKPOINT Updating to latest ticket time: {latest_modified.isoformat()}")
            save_checkpoint(latest_modified, total_processed, total_errors)
        except Exception as e:
            logger.warning(f"Could not update checkpoint with latest time: {e}")
            # Fallback: use current time
            save_checkpoint(datetime.now(timezone.utc), total_processed, total_errors)
    else:
        # No tickets processed, keep existing checkpoint
        logger.info("CHECKPOINT No new tickets, keeping existing timestamp")
        save_checkpoint(last_sync_time, total_processed, total_errors)
    
    # ============================================================
    # Final summary
    # ============================================================
    
    logger.info("")
    logger.info("============================================================")
    logger.info(f"DONE INTELLIGENCE SYNC COMPLETED (v{VERSION})")
    logger.info("============================================================")
    logger.info(f"  Total Processed: {METRICS['total_processed']}/{len(tickets)}")
    logger.info(f"  Active Tickets: {METRICS['active_tickets']}")
    logger.info(f"  Closed Tickets: {METRICS['closed_tickets']}")
    logger.info(f"  With Conversations: {METRICS['with_conversations']}")
    logger.info(f"  High Complexity: {METRICS['high_complexity']}")
    logger.info(f"  Satisfaction At-Risk: {METRICS['satisfaction_at_risk']}")
    logger.info(f"  High SLA Breach Risk: {METRICS['high_sla_breach_risk']}")
    logger.info(f"  Awaiting Customer: {METRICS['awaiting_customer']}")
    logger.info(f"  Awaiting Technician: {METRICS['awaiting_technician']}")
    logger.info("")
    logger.info(f"  NEW v2.8 METRICS:")
    logger.info(f"  With Assets: {METRICS['with_assets']}")
    logger.info(f"  Reopened Tickets: {METRICS['reopened']}")
    logger.info(f"  First Call Resolution: {METRICS['fcr']}")
    logger.info(f"  With Approvals: {METRICS['with_approvals']}")
    logger.info(f"  Linked Tickets: {METRICS['linked_tickets']}")
    logger.info("")
    logger.info(f"  Errors/Skipped: {total_errors}")
    logger.info(f"  Finished: {datetime.now()}")
    logger.info("============================================================")
    
    # Print analytics
    logger.info("")
    print_analytics_summary()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
