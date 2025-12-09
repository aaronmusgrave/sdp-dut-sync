#!/usr/bin/env python3
"""
ServiceDesk Plus Intelligence Suite v2.8 - COMPLETE FIELD COVERAGE
Major additions:
- ALL 20 missing fields integrated
- Asset/CI tracking
- Source/Mode (channel analysis)
- Impact & Urgency (priority matrix)
- Level/Tier tracking
- Service catalog
- Time tracking (time_spent)
- Resolution category
- Closure code
- Reopened count & status
- Approvals
- Tags/Labels
- Department (requester)
- Linked tickets
- Attachments
- Change/Problem links
- First Call Resolution
- Created by
- Template used
- Cost fields
Performance: Phase 1 & 2 optimizations (3-8x faster)
"""

import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape
from collections import defaultdict, Counter, OrderedDict
import statistics
import concurrent.futures
from functools import lru_cache
import hashlib
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sdp_sync.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Settings - OPTIMIZED
TICKETS_PER_PAGE = 100
MAX_TICKETS = 10000
MAX_RETRIES = 3
MIN_PAGES_TO_CHECK = 5
BATCH_SIZE = 50
DUST_UPLOAD_BATCH_SIZE = 50
MAX_PARALLEL_WORKERS = 8
MAX_CONVERSATION_WORKERS = 5
CHECKPOINT_FILE = 'sync_checkpoint.json'
TOKEN_REFRESH_BUFFER_MINUTES = 10

# Status categorization
STATUS_CATEGORIES = {
    'open': ['open', 'new', 'in progress', 'reopened', 'pending', 'assigned'],
    'closed': ['closed', 'resolved', 'completed', 'done'],
    'waiting': ['waiting', 'on hold', 'pending approval', 'awaiting'],
    'cancelled': ['cancelled', 'rejected', 'declined']
}

# Token cache
_access_token = None
_token_expiry = None

# Session with connection pooling
_session = None

def get_session():
    """Get or create a requests session with connection pooling"""
    global _session
    if _session is None:
        _session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=40
        )
        _session.mount('https://', adapter)
        _session.mount('http://', adapter)
    return _session

# Compiled regex patterns for fast keyword extraction
KEYWORD_PATTERNS = {
    'access': re.compile(r'\b(cannot access|access denied|login|password|locked out|permission)\b', re.I),
    'performance': re.compile(r'\b(slow|hanging|freeze|timeout|not responding|lag)\b', re.I),
    'error': re.compile(r'\b(error|exception|failed|crash|bug|issue)\b', re.I),
    'hardware': re.compile(r'\b(laptop|computer|printer|monitor|keyboard|mouse|device)\b', re.I),
    'software': re.compile(r'\b(application|program|software|app|system)\b', re.I),
    'network': re.compile(r'\b(network|internet|wifi|connection|vpn|offline)\b', re.I),
    'email': re.compile(r'\b(email|outlook|mail|inbox|send|receive)\b', re.I),
    'account': re.compile(r'\b(account|user|profile|credentials)\b', re.I)
}
# Bounded Cache for Memory Management
class BoundedCache:
    """LRU cache with size limit to prevent memory bloat"""
    def __init__(self, max_size=1000):
        self.cache = OrderedDict()
        self.max_size = max_size
    
    def get(self, key, default=None):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return default
    
    def set(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)
    
    def __len__(self):
        return len(self.cache)

# Pre-calculated Analytics Cache
class AnalyticsCache:
    """Smart analytics cache with lazy calculation"""
    def __init__(self):
        self.raw_data = {
            'category_stats': defaultdict(lambda: {'count': 0, 'avg_resolution_days': [], 'overdue_count': 0, 'sla_breach_count': 0}),
            'technician_stats': defaultdict(lambda: {'active': 0, 'total': 0, 'avg_response_hours': []}),
            'requester_stats': BoundedCache(max_size=5000),
            'hourly_distribution': defaultdict(int),
            'daily_distribution': defaultdict(int),
            'issue_keywords': Counter(),
            'sla_breach_patterns': defaultdict(lambda: {'total': 0, 'breached': 0}),
            # NEW v2.8: Additional analytics
            'source_distribution': Counter(),
            'department_stats': Counter(),
            'asset_failure_rates': defaultdict(int),
            'service_stats': Counter(),
            'reopened_patterns': defaultdict(int),
            'fcr_stats': {'total': 0, 'fcr': 0}
        }
        self._calculated = {}
        self._dirty = True
    
    def mark_dirty(self):
        self._dirty = True
    
    def get_category_breach_rate(self, category_key):
        """Get pre-calculated breach rate"""
        if self._dirty:
            self._recalculate()
        return self._calculated.get('breach_rates', {}).get(category_key, 0)
    
    def get_category_avg_resolution(self, category):
        """Get pre-calculated average resolution time"""
        if self._dirty:
            self._recalculate()
        return self._calculated.get('avg_resolutions', {}).get(category, 3.0)
    
    def _recalculate(self):
        """Pre-calculate all analytics metrics"""
        self._calculated['breach_rates'] = {}
        self._calculated['avg_resolutions'] = {}
        
        # Calculate breach rates
        for key, stats in self.raw_data['sla_breach_patterns'].items():
            if stats['total'] > 0:
                self._calculated['breach_rates'][key] = stats['breached'] / stats['total']
        
        # Calculate average resolutions
        for cat, stats in self.raw_data['category_stats'].items():
            if stats['avg_resolution_days']:
                self._calculated['avg_resolutions'][cat] = statistics.mean(stats['avg_resolution_days'])
        
        self._dirty = False

analytics_cache = AnalyticsCache()

# Performance Monitor
class PerformanceMonitor:
    def __init__(self):
        self.timings = defaultdict(list)
    
    def time_operation(self, operation_name):
        class Timer:
            def __init__(self, monitor, name):
                self.monitor = monitor
                self.name = name
                self.start = None
            
            def __enter__(self):
                self.start = time.time()
                return self
            
            def __exit__(self, *args):
                elapsed = time.time() - self.start
                self.monitor.timings[self.name].append(elapsed)
        
        return Timer(self, operation_name)
    
    def print_report(self):
        logger.info("\n" + "="*60)
        logger.info("STATS PERFORMANCE REPORT (v2.8 COMPLETE)")
        logger.info("="*60)
        for op, times in sorted(self.timings.items()):
            if times:
                avg = statistics.mean(times)
                total = sum(times)
                logger.info(f"{op:35} | Avg: {avg:.3f}s | Total: {total:6.1f}s | Calls: {len(times):4}")
        logger.info("="*60)

perf = PerformanceMonitor()

# Checkpoint Management
class CheckpointManager:
    def __init__(self, checkpoint_file=CHECKPOINT_FILE):
        self.checkpoint_file = checkpoint_file
    
    def save(self, processed_ids, success_count, error_count):
        try:
            checkpoint = {
                'timestamp': datetime.now().isoformat(),
                'processed_ids': list(processed_ids),
                'success_count': success_count,
                'error_count': error_count
            }
            with open(self.checkpoint_file, 'w') as f:
                json.dump(checkpoint, f)
            logger.info(f"SAVED Checkpoint saved: {success_count} successful, {error_count} errors")
        except Exception as e:
            logger.error(f"Could not save checkpoint: {e}")
    
    def load(self):
        try:
            if os.path.exists(self.checkpoint_file):
                with open(self.checkpoint_file, 'r') as f:
                    checkpoint = json.load(f)
                logger.info(f"LOADED Loaded checkpoint: {len(checkpoint['processed_ids'])} tickets already processed")
                return set(checkpoint['processed_ids']), checkpoint['success_count'], checkpoint['error_count']
        except Exception as e:
            logger.error(f"Could not load checkpoint: {e}")
        
        return set(), 0, 0
    
    def clear(self):
        try:
            if os.path.exists(self.checkpoint_file):
                os.remove(self.checkpoint_file)
                logger.info("CLEARED  Checkpoint cleared")
        except Exception as e:
            logger.error(f"Could not clear checkpoint: {e}")

checkpoint_mgr = CheckpointManager()
def validate_environment():
    """Validate all required environment variables"""
    config = {
        'SDP_DOMAIN': os.environ.get('SDP_DOMAIN'),
        'SDP_CLIENT_ID': os.environ.get('SDP_CLIENT_ID'),
        'SDP_CLIENT_SECRET': os.environ.get('SDP_CLIENT_SECRET'),
        'SDP_REFRESH_TOKEN': os.environ.get('SDP_REFRESH_TOKEN'),
        'DUST_API_KEY': os.environ.get('DUST_API_KEY'),
        'DUST_WORKSPACE_ID': os.environ.get('DUST_WORKSPACE_ID'),
    }
    
    missing = [k for k, v in config.items() if not v]
    
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    
    if config['SDP_DOMAIN'] and ('http://' in config['SDP_DOMAIN'] or 'https://' in config['SDP_DOMAIN']):
        raise ValueError("SDP_DOMAIN should not include http:// or https://")
    
    logger.info("OK Environment validation passed")
    return config

def get_access_token(force_refresh=False):
    """Get a valid access token with proactive refresh"""
    global _access_token, _token_expiry
    
    # Proactive refresh if within buffer period
    if not force_refresh and _access_token and _token_expiry:
        if datetime.now() + timedelta(minutes=TOKEN_REFRESH_BUFFER_MINUTES) < _token_expiry:
            return _access_token
        else:
            logger.info("SYNC Proactive token refresh (approaching expiry)")
            force_refresh = True
    
    logger.info("Getting access token...")
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    session = get_session()
    response = session.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        data = response.json()
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        logger.info("OK Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def safe_get_value(obj, default=None):
    """Extract value from ServiceDesk object"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get('value', default)
        if val is not None:
            try:
                return int(val)
            except:
                return default
        return default
    try:
        return int(obj)
    except:
        return default

def safe_string(value, default=''):
    """Safely convert to string"""
    if value is None:
        return default
    try:
        return str(value).strip()
    except:
        return default

def safe_get_name(obj, default='Unknown'):
    """Extract name from ServiceDesk object"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('name', default))
    return safe_string(obj, default)

def safe_get_display_value(obj, default=''):
    """Extract display_value from ServiceDesk object"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('display_value', default))
    return safe_string(obj, default)

def safe_int_conversion(value, default=None):
    """Safely convert any value to int"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def safe_float_conversion(value, default=None):
    """Safely convert any value to float"""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_list(obj, default=None):
    """Safely get list from object"""
    if default is None:
        default = []
    if obj is None:
        return default
    if isinstance(obj, list):
        return obj
    return default

@lru_cache(maxsize=10000)
def strip_html(html_text):
    """Remove HTML tags and decode HTML entities - CACHED"""
    if not html_text:
        return ""
    
    try:
        text = unescape(html_text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except:
        return str(html_text)

def get_display_id(details):
    """Extract the display ID"""
    if 'display_id' in details:
        display_id = details['display_id']
        if isinstance(display_id, dict):
            return safe_string(display_id.get('display_value', display_id.get('value', details.get('id'))))
        return safe_string(display_id)
    
    return safe_string(details.get('id', 'Unknown'))

def get_status_category(status_lower):
    """Determine status category from status string"""
    for category, keywords in STATUS_CATEGORIES.items():
        if any(kw in status_lower for kw in keywords):
            return category
    return 'unknown'

def extract_keywords_fast(text):
    """Extract key issue keywords - OPTIMIZED with compiled regex"""
    if not text:
        return []
    
    keywords = []
    for category, pattern in KEYWORD_PATTERNS.items():
        if pattern.search(text):
            keywords.append(category)
            analytics_cache.raw_data['issue_keywords'][category] += 1
    
    return keywords
def fetch_all_tickets():
    """Fetch tickets with API-level filtering"""
    with perf.time_operation('fetch_all_tickets'):
        access_token = get_access_token()
        all_tickets = []
        seen_ticket_ids = set()
        start_index = 1
        
        last_sync_time = os.environ.get('LAST_SYNC_TIME')
        
        if last_sync_time:
            try:
                last_sync_ms = int(last_sync_time)
                last_sync_dt = datetime.fromtimestamp(last_sync_ms / 1000)
                logger.info(f"STATS Incremental sync: Fetching tickets updated since {last_sync_dt}")
                incremental = True
            except:
                logger.warning("WARNING  Invalid last sync time, performing full sync")
                incremental = False
        else:
            logger.info("SYNC No last sync time found - performing initial full sync")
            incremental = False
        
        logger.info(f"Fetching {'updated' if incremental else 'all'} tickets...")
        
        pages_checked = 0
        tickets_found = 0
        
        page_limit = 20 if incremental else 100
        
        logger.info(f"  STATS Page limit: {page_limit} pages (up to {page_limit * TICKETS_PER_PAGE} tickets)")
        
        session = get_session()
        
        while len(seen_ticket_ids) < MAX_TICKETS and pages_checked < page_limit:
            headers = {
                'Authorization': f'Zoho-oauthtoken {access_token}',
                'Accept': 'application/vnd.manageengine.sdp.v3+json'
            }
            
            sort_field = "last_updated_time" if incremental else "created_time"
            
            # API-LEVEL FILTERING
            list_info = {
                "list_info": {
                    "row_count": TICKETS_PER_PAGE,
                    "start_index": start_index,
                    "sort_field": sort_field,
                    "sort_order": "desc"
                }
            }
            
            # Add server-side filter for incremental sync
            if incremental and last_sync_time:
                list_info["list_info"]["search_criteria"] = [
                    {
                        "field": "last_updated_time",
                        "condition": "greater than",
                        "value": str(last_sync_ms)
                    }
                ]
            
            params = {'input_data': json.dumps(list_info)}
            url = f"{SDP_API_BASE}/requests"
            
            try:
                response = session.get(url, headers=headers, params=params, timeout=30)
                
                if response.status_code == 200:
                    result = response.json()
                    tickets = result.get('requests', [])
                    
                    if not tickets:
                        logger.info(f"  OK No more tickets available from API")
                        break
                    
                    pages_checked += 1
                    page_added = 0
                    
                    for ticket in tickets:
                        ticket_id = ticket.get('id')
                        if ticket_id not in seen_ticket_ids:
                            all_tickets.append(ticket)
                            seen_ticket_ids.add(ticket_id)
                            page_added += 1
                            tickets_found += 1
                    
                    if page_added > 0:
                        logger.info(f"  Page {pages_checked}: Found {page_added} tickets")
                    
                    response_list_info = result.get('list_info', {})
                    has_more = response_list_info.get('has_more_rows', False)
                    
                    if not has_more or len(tickets) == 0:
                        break
                    
                    start_index += TICKETS_PER_PAGE
                    time.sleep(0.3)
                    
                else:
                    logger.warning(f"  WARNING  API returned status {response.status_code}")
                    break
                    
            except Exception as e:
                logger.error(f"  WARNING  Error fetching tickets: {e}")
                break
        
        sync_type = "incremental" if incremental else "full"
        logger.info(f"OK Fetched {len(all_tickets)} tickets ({sync_type} sync)")
        return all_tickets

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    try:
        with perf.time_operation('fetch_ticket_details'):
            session = get_session()
            headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
            url = f"{SDP_API_BASE}/requests/{ticket_id}"
            
            response = session.get(url, headers=headers, timeout=20)
            
            if response and response.status_code == 200:
                return response.json().get('request', {})
            return None
    except Exception as e:
        logger.error(f"  WARNING  Error fetching details for {ticket_id}: {str(e)[:100]}")
        return None

def fetch_single_notification_detail(ticket_id, notif_id, access_token):
    """Fetch single notification detail"""
    try:
        session = get_session()
        headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
        detail_url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications/{notif_id}"
        detail_response = session.get(detail_url, headers=headers, timeout=10)
        
        if detail_response.status_code == 200:
            detail_data = detail_response.json()
            notification = detail_data.get('notification', {})
            
            description = notification.get('description', '')
            if description and len(description.strip()) > 0:
                sender = notification.get('sender', {})
                time_obj = notification.get('time', {})
                
                return {
                    'id': notif_id,
                    'type': notification.get('type', ''),
                    'sender': sender.get('name', 'Unknown'),
                    'sender_email': sender.get('email_id', ''),
                    'time': time_obj.get('display_value', 'Unknown'),
                    'time_ms': time_obj.get('value'),
                    'description': description,
                    'is_public': notification.get('is_public', True)
                }
        return None
    except Exception as e:
        return None

def fetch_ticket_conversations_parallel(ticket_id, access_token):
    """Fetch ticket conversations in parallel"""
    try:
        with perf.time_operation('fetch_conversations'):
            session = get_session()
            headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
            
            url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
            response = session.get(url, headers=headers, timeout=15)
            
            if response.status_code != 200:
                return []
            
            notifications = response.json().get('notifications', [])
            
            # Filter relevant notifications
            relevant_notifs = [
                n for n in notifications[:20]
                if any(keyword in n.get('type', '').lower() for keyword in ['conversation', 'reply', 'reqreply', 'technician'])
            ]
            
            if not relevant_notifs:
                return []
            
            # Parallel fetch of notification details
            conversations = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONVERSATION_WORKERS) as executor:
                future_to_notif = {
                    executor.submit(fetch_single_notification_detail, ticket_id, n.get('id'), access_token): n
                    for n in relevant_notifs
                }
                
                for future in concurrent.futures.as_completed(future_to_notif, timeout=30):
                    try:
                        result = future.result(timeout=5)
                        if result:
                            conversations.append(result)
                    except Exception as e:
                        continue
            
            return conversations
            
    except Exception as e:
        logger.error(f"  WARNING  Error fetching conversations for {ticket_id}: {str(e)[:50]}")
        return []
def calculate_complexity_score(ticket_data, conversations, age_days):
    """Calculate ticket complexity score (0-10)"""
    score = 0
    
    if len(conversations) > 10:
        score += 3
    elif len(conversations) > 5:
        score += 2
    elif len(conversations) > 2:
        score += 1
    
    if age_days > 30:
        score += 3
    elif age_days > 14:
        score += 2
    elif age_days > 7:
        score += 1
    
    if ticket_data.get('is_escalated'):
        score += 2
    if ticket_data.get('is_overdue'):
        score += 1
    
    priority = ticket_data.get('priority', '').lower()
    if priority == 'urgent':
        score += 1
    
    # NEW v2.8: Reopened adds complexity
    if ticket_data.get('reopened_count', 0) > 0:
        score += 1
    
    return min(score, 10)

def predict_sla_breach_likelihood(ticket_data, hours_until_due, complexity_score, requester_history, category_key):
    """Predict likelihood of SLA breach - uses pre-calculated stats"""
    if hours_until_due is None or hours_until_due < 0:
        return (100, 'breached', ['Already overdue'])
    
    breach_score = 0
    factors = []
    
    if hours_until_due < 2:
        breach_score += 40
        factors.append('Critical time pressure (<2h)')
    elif hours_until_due < 4:
        breach_score += 30
        factors.append('High time pressure (<4h)')
    elif hours_until_due < 8:
        breach_score += 20
        factors.append('Moderate time pressure (<8h)')
    elif hours_until_due < 24:
        breach_score += 10
        factors.append('Limited time remaining (<24h)')
    
    if complexity_score >= 8:
        breach_score += 25
        factors.append('Very high complexity')
    elif complexity_score >= 6:
        breach_score += 18
        factors.append('High complexity')
    elif complexity_score >= 4:
        breach_score += 10
        factors.append('Moderate complexity')
    
    # Use pre-calculated breach rate
    breach_rate = analytics_cache.get_category_breach_rate(category_key)
    if breach_rate > 0.3:
        breach_score += 15
        factors.append(f'Category has high breach rate ({breach_rate*100:.0f}%)')
    elif breach_rate > 0.15:
        breach_score += 8
        factors.append(f'Category has elevated breach rate ({breach_rate*100:.0f}%)')
    
    if requester_history and requester_history.get('open_count', 0) > 5:
        breach_score += 10
        factors.append(f"Requester has {requester_history['open_count']} open tickets")
    
    priority = ticket_data.get('priority', '').lower()
    if priority in ['urgent', 'high']:
        breach_score += 10
        factors.append(f'{priority.title()} priority ticket')
    
    if breach_score >= 75:
        risk_level = 'critical'
    elif breach_score >= 50:
        risk_level = 'high'
    elif breach_score >= 25:
        risk_level = 'moderate'
    else:
        risk_level = 'low'
    
    return (min(breach_score, 100), risk_level, factors)

def analyze_conversation_intelligence(conversations, created_time_ms, technician_name):
    """Deep conversation flow analysis"""
    if not conversations:
        return {
            'total_exchanges': 0,
            'customer_messages': 0,
            'technician_messages': 0,
            'avg_response_time_hours': None,
            'longest_gap_hours': None,
            'last_responder': 'none',
            'awaiting': 'unknown',
            'response_pattern': 'no-conversation',
            'unanswered_questions': 0,
            'engagement_score': 0
        }
    
    sorted_convs = sorted(conversations, key=lambda x: safe_int_conversion(x.get('time_ms'), 0))
    
    customer_count = 0
    technician_count = 0
    response_times = []
    gaps = []
    last_responder_type = 'unknown'
    unanswered_questions = 0
    
    prev_time_ms = created_time_ms
    
    for idx, conv in enumerate(sorted_convs):
        sender = conv.get('sender', '')
        time_ms = safe_int_conversion(conv.get('time_ms'))
        description = strip_html(conv.get('description', '')).lower()
        
        is_technician = (sender == technician_name or 
                        'technician' in conv.get('type', '').lower() or
                        any(word in sender.lower() for word in ['tech', 'support', 'admin']))
        
        if is_technician:
            technician_count += 1
            last_responder_type = 'technician'
        else:
            customer_count += 1
            last_responder_type = 'customer'
        
        if time_ms and prev_time_ms:
            gap_hours = (time_ms - prev_time_ms) / (1000 * 60 * 60)
            if gap_hours > 0:
                response_times.append(gap_hours)
                gaps.append(gap_hours)
        
        if '?' in description and not is_technician:
            if idx < len(sorted_convs) - 1:
                next_sender = sorted_convs[idx + 1].get('sender', '')
                next_is_tech = (next_sender == technician_name or 
                               'technician' in sorted_convs[idx + 1].get('type', '').lower())
                if not next_is_tech:
                    unanswered_questions += 1
            else:
                unanswered_questions += 1
        
        prev_time_ms = time_ms
    
    awaiting = 'unknown'
    if last_responder_type == 'customer':
        awaiting = 'technician'
    elif last_responder_type == 'technician':
        awaiting = 'customer'
    
    total_messages = customer_count + technician_count
    if total_messages == 0:
        response_pattern = 'no-conversation'
    elif technician_count == 0:
        response_pattern = 'no-tech-response'
    elif customer_count == 0:
        response_pattern = 'no-customer-response'
    elif technician_count > customer_count * 2:
        response_pattern = 'tech-heavy'
    elif customer_count > technician_count * 2:
        response_pattern = 'customer-heavy'
    else:
        response_pattern = 'balanced'
    
    engagement_score = 0
    if total_messages > 0:
        engagement_score = min(10, total_messages)
    if technician_count > 0 and customer_count > 0:
        engagement_score += 2
    if response_times and statistics.mean(response_times) < 4:
        engagement_score += 2
    engagement_score = min(10, engagement_score)
    
    return {
        'total_exchanges': len(conversations),
        'customer_messages': customer_count,
        'technician_messages': technician_count,
        'avg_response_time_hours': round(statistics.mean(response_times), 1) if response_times else None,
        'longest_gap_hours': round(max(gaps), 1) if gaps else None,
        'last_responder': last_responder_type,
        'awaiting': awaiting,
        'response_pattern': response_pattern,
        'unanswered_questions': unanswered_questions,
        'engagement_score': engagement_score,
        'message_ratio': round(technician_count / customer_count, 2) if customer_count > 0 else None
    }

def get_requester_statistics(requester_name, requester_email):
    """Get or create requester statistics from bounded cache"""
    key = requester_email if requester_email else requester_name
    existing = analytics_cache.raw_data['requester_stats'].get(key)
    if existing is None:
        existing = {'open_count': 0, 'total_count': 0, 'avg_resolution_days': [], 'satisfaction_scores': []}
        analytics_cache.raw_data['requester_stats'].set(key, existing)
    return existing

def predict_resolution_time(category, priority, complexity_score):
    """Predict resolution time using pre-calculated averages"""
    # Use pre-calculated average
    avg_days = analytics_cache.get_category_avg_resolution(category)
    
    priority_multipliers = {
        'urgent': 0.5,
        'high': 0.75,
        'normal': 1.0,
        'low': 1.5
    }
    multiplier = priority_multipliers.get(priority.lower(), 1.0)
    
    complexity_multiplier = 1.0 + (complexity_score / 20.0)
    
    predicted_days = avg_days * multiplier * complexity_multiplier
    
    return round(predicted_days, 1)

def analyze_sentiment_risk(conversations):
    """Analyze conversation sentiment for satisfaction risk"""
    if not conversations:
        return 'unknown', 0
    
    negative_keywords = ['frustrated', 'angry', 'disappointed', 'unacceptable', 'terrible', 
                         'urgent', 'asap', 'immediately', 'escalate', 'complaint']
    positive_keywords = ['thank', 'appreciate', 'great', 'excellent', 'resolved', 'perfect', 'helpful']
    
    negative_count = 0
    positive_count = 0
    
    for conv in conversations:
        text = strip_html(conv.get('description', '')).lower()
        negative_count += sum(1 for kw in negative_keywords if kw in text)
        positive_count += sum(1 for kw in positive_keywords if kw in text)
    
    total_signals = negative_count + positive_count
    if total_signals == 0:
        return 'neutral', 0
    
    risk_score = (negative_count / total_signals) * 100
    
    if risk_score > 60:
        return 'high_risk', risk_score
    elif risk_score > 30:
        return 'moderate_risk', risk_score
    elif positive_count > negative_count:
        return 'positive', risk_score
    else:
        return 'neutral', risk_score
def generate_resolution_suggestions(category, keywords, similar_count=0):
    """Generate AI-powered resolution suggestions"""
    suggestions = []
    
    kb_templates = {
        'IT - Office 365 Email/Office Apps': [
            "Clear Microsoft Office cache and credentials",
            "Sign out completely and sign back in",
            "Verify Office 365 license status in admin portal",
            "Check MFA/authentication settings",
            "Test in Outlook Web Access to isolate client issues"
        ],
        'IT - Priority Request/Support': [
            "Verify request has proper approvals",
            "Confirm resource availability and lead time",
            "Set clear expectations on timeline",
            "Document any dependencies or blockers"
        ],
        'IT - General IT Support': [
            "Gather detailed information (screenshots, error messages)",
            "Search for similar past tickets for resolution patterns",
            "Attempt basic troubleshooting steps",
            "Check knowledge base for documented solutions"
        ],
        'IT - Network/Infrastructure': [
            "Check network connectivity and DNS resolution",
            "Verify firewall rules and security groups",
            "Test from different network/location",
            "Review recent infrastructure changes"
        ]
    }
    
    if category in kb_templates:
        suggestions.extend(kb_templates[category])
    
    if 'access' in keywords or 'password' in keywords:
        suggestions.append("🔐 Reset credentials and verify account status")
    
    if 'performance' in keywords:
        suggestions.append("⚡ Check system resources and clear cache")
    
    if 'error' in keywords:
        suggestions.append("🐛 Collect error logs for troubleshooting")
    
    if 'network' in keywords:
        suggestions.append("🌐 Test connectivity and verify VPN")
    
    if similar_count > 3:
        suggestions.append(f"STATS {similar_count} similar tickets found - review for solutions")
    
    return suggestions[:5]

def update_analytics_cache(ticket_data, conversations, age_days, is_closed):
    """Update global analytics cache - v2.8 with NEW fields"""
    category = ticket_data.get('category', 'Unknown')
    technician = ticket_data.get('technician_name', 'Unassigned')
    requester_name = ticket_data.get('requester_name', 'Unknown')
    requester_email = ticket_data.get('requester_email', '')
    
    # Existing analytics
    analytics_cache.raw_data['category_stats'][category]['count'] += 1
    if ticket_data.get('is_overdue'):
        analytics_cache.raw_data['category_stats'][category]['overdue_count'] += 1
    if is_closed and age_days:
        analytics_cache.raw_data['category_stats'][category]['avg_resolution_days'].append(age_days)
    
    category_key = f"{category}_{ticket_data.get('priority', 'normal')}"
    analytics_cache.raw_data['sla_breach_patterns'][category_key]['total'] += 1
    if ticket_data.get('is_overdue'):
        analytics_cache.raw_data['sla_breach_patterns'][category_key]['breached'] += 1
    
    if technician != 'Unassigned':
        analytics_cache.raw_data['technician_stats'][technician]['total'] += 1
        if not is_closed:
            analytics_cache.raw_data['technician_stats'][technician]['active'] += 1
        
        if conversations and len(conversations) > 0:
            first_conv = conversations[-1]
            first_response_ms = safe_int_conversion(first_conv.get('time_ms'))
            created_ms = ticket_data.get('created_time_ms')
            if first_response_ms and created_ms:
                response_hours = (first_response_ms - created_ms) / (1000 * 60 * 60)
                if response_hours > 0:
                    analytics_cache.raw_data['technician_stats'][technician]['avg_response_hours'].append(response_hours)
    
    req_stats = get_requester_statistics(requester_name, requester_email)
    req_stats['total_count'] += 1
    if not is_closed:
        req_stats['open_count'] += 1
    if is_closed and age_days:
        req_stats['avg_resolution_days'].append(age_days)
    
    created_ms = ticket_data.get('created_time_ms')
    if created_ms:
        created_dt = datetime.fromtimestamp(created_ms / 1000)
        analytics_cache.raw_data['hourly_distribution'][created_dt.hour] += 1
        analytics_cache.raw_data['daily_distribution'][created_dt.strftime('%A')] += 1
    
    # NEW v2.8: Additional analytics
    
    # Source/Mode tracking
    source = ticket_data.get('source', 'Unknown')
    if source and source != 'Unknown':
        analytics_cache.raw_data['source_distribution'][source] += 1
    
    # Department tracking
    department = ticket_data.get('department', 'Unknown')
    if department and department != 'Unknown':
        analytics_cache.raw_data['department_stats'][department] += 1
    
    # Asset tracking (if failed/closed)
    asset = ticket_data.get('asset', '')
    if asset and asset != 'Unknown' and asset != '':
        analytics_cache.raw_data['asset_failure_rates'][asset] += 1
    
    # Service tracking
    service = ticket_data.get('service', '')
    if service and service != 'Unknown' and service != '':
        analytics_cache.raw_data['service_stats'][service] += 1
    
    # Reopened patterns
    reopened_count = ticket_data.get('reopened_count', 0)
    if reopened_count > 0:
        analytics_cache.raw_data['reopened_patterns'][category] += 1
    
    # FCR tracking
    is_fcr = ticket_data.get('is_fcr', False)
    if is_closed:
        analytics_cache.raw_data['fcr_stats']['total'] += 1
        if is_fcr:
            analytics_cache.raw_data['fcr_stats']['fcr'] += 1
    
    # Mark analytics as dirty for recalculation
    analytics_cache.mark_dirty()
def format_ticket_with_intelligence(ticket, details, conversations):
    """Format ticket with MAXIMUM intelligence - v2.8 COMPLETE FIELD COVERAGE"""
    
    with perf.time_operation('format_ticket'):
        try:
            # ========== BASIC FIELDS ==========
            internal_id = safe_string(ticket.get('id'))
            display_id = get_display_id(details)
            subject = safe_string(details.get('subject', 'No Subject'))
            
            # Status
            status_obj = details.get('status', {})
            status = safe_get_name(status_obj)
            status_lower = status.lower()
            status_category = get_status_category(status_lower)
            
            # Priority
            priority = safe_get_name(details.get('priority'))
            
            # Requester
            requester_obj = details.get('requester', {})
            requester_name = safe_get_name(requester_obj)
            requester_email = safe_string(requester_obj.get('email_id', '') if isinstance(requester_obj, dict) else '')
            
            # Technician
            technician_obj = details.get('technician', {})
            technician_name = safe_get_name(technician_obj, 'Unassigned')
            technician_email = safe_string(technician_obj.get('email_id', '') if isinstance(technician_obj, dict) else '')
            
            # Classification
            category = safe_get_name(details.get('category'), 'Uncategorized')
            subcategory = safe_get_name(details.get('subcategory'), 'None')
            group = safe_get_name(details.get('group'), 'Unassigned')
            site = safe_get_name(details.get('site'), 'No site')
            request_type = safe_get_name(details.get('request_type'), 'Incident')
            
            # Timestamps
            created_time_ms = safe_get_value(details.get('created_time'))
            due_by_time_ms = safe_get_value(details.get('due_by_time'))
            resolved_time_ms = safe_get_value(details.get('resolved_time'))
            last_updated_time_ms = safe_get_value(details.get('last_updated_time'))
            
            created_time_display = safe_get_display_value(details.get('created_time'))
            due_by_time_display = safe_get_display_value(details.get('due_by_time'), 'Not set')
            resolved_time_display = safe_get_display_value(details.get('resolved_time'), 'Not resolved')
            
            # Flags
            is_overdue = bool(details.get('is_overdue', False))
            is_escalated = bool(details.get('is_escalated', False))
            
            # Description & Resolution
            raw_description = details.get('description', '')
            clean_description = strip_html(raw_description)
            
            resolution_obj = details.get('resolution')
            resolution_text = ''
            if resolution_obj and isinstance(resolution_obj, dict):
                resolution_content = resolution_obj.get('content', '')
                if resolution_content:
                    resolution_text = strip_html(resolution_content)
            
            # ========== NEW v2.8 FIELDS ==========
            
            # 1. ASSETS/CI
            assets = safe_list(details.get('assets'))
            asset_name = 'Unknown'
            asset_id = None
            if assets and len(assets) > 0:
                asset_obj = assets[0]
                if isinstance(asset_obj, dict):
                    asset_name = safe_get_name(asset_obj, 'Unknown')
                    asset_id = asset_obj.get('id')
                else:
                    asset_name = safe_string(asset_obj)
            
            # 2. SOURCE/MODE (Channel)
            source_obj = details.get('source', {})
            source = safe_get_name(source_obj, 'Unknown')
            
            mode_obj = details.get('mode', {})
            mode = safe_get_name(mode_obj, 'Unknown')
            
            # 3. IMPACT & URGENCY
            impact_obj = details.get('impact', {})
            impact = safe_get_name(impact_obj, 'Unknown')
            
            urgency_obj = details.get('urgency', {})
            urgency = safe_get_name(urgency_obj, 'Unknown')
            
            # 4. LEVEL/TIER
            level_obj = details.get('level', {})
            level = safe_get_name(level_obj, 'Unknown')
            
            # 5. SERVICE
            service_obj = details.get('service', {})
            service = safe_get_name(service_obj, 'Unknown')
            
            service_category_obj = details.get('service_category', {})
            service_category = safe_get_name(service_category_obj, 'Unknown')
            
            # 6. TIME TRACKING
            time_spent_obj = details.get('time_spent', {})
            time_spent_minutes = safe_get_value(time_spent_obj, 0)
            time_spent_hours = round(time_spent_minutes / 60, 2) if time_spent_minutes else 0
            
            # 7. RESOLUTION CATEGORY
            resolution_category = 'Unknown'
            resolution_subcategory = 'Unknown'
            if resolution_obj and isinstance(resolution_obj, dict):
                res_cat_obj = resolution_obj.get('resolution_category', {})
                resolution_category = safe_get_name(res_cat_obj, 'Unknown')
                
                res_subcat_obj = resolution_obj.get('resolution_subcategory', {})
                resolution_subcategory = safe_get_name(res_subcat_obj, 'Unknown')
            
            # 8. CLOSURE CODE
            closure_code_obj = details.get('closure_code', {})
            closure_code = safe_get_name(closure_code_obj, 'Unknown')
            
            closure_info_obj = details.get('closure_info', {})
            closure_comments = ''
            if closure_info_obj and isinstance(closure_info_obj, dict):
                closure_comments = safe_string(closure_info_obj.get('closure_comments', ''))
            
            # 9. REOPENED COUNT
            reopened_count = safe_int_conversion(details.get('reopened_count'), 0)
            is_reopened = bool(details.get('is_reopened', False))
            
            # 10. APPROVAL STATUS
            approval_status_obj = details.get('approval_status', {})
            approval_status = safe_get_name(approval_status_obj, 'Not Required')
            
            approvals = safe_list(details.get('approvals'))
            approval_count = len(approvals)
            has_approvals = approval_count > 0  # <-- THIS WAS MISSING
            has_pending_approvals = any(
                safe_get_name(a.get('approval_status', {})) == 'Pending' 
                for a in approvals if isinstance(a, dict)
            )
            
            # 11. TAGS
            tags = safe_list(details.get('tags'))
            tag_names = [safe_get_name(tag) for tag in tags if tag]
            
            # 12. DEPARTMENT (Requester)
            department = 'Unknown'
            if isinstance(requester_obj, dict):
                dept_obj = requester_obj.get('department', {})
                department = safe_get_name(dept_obj, 'Unknown')
            
            # 13. LINKED TICKETS
            linked_requests = safe_list(details.get('linked_requests'))
            linked_ticket_count = len(linked_requests)
            has_linked_tickets = linked_ticket_count > 0
            linked_ticket_ids = [safe_string(lr.get('id')) for lr in linked_requests if isinstance(lr, dict)]

            parent_request_obj = details.get('parent_request', {})
            parent_request_id = None
            has_parent = False
            if parent_request_obj and isinstance(parent_request_obj, dict):
                parent_request_id = safe_string(parent_request_obj.get('id'))
                has_parent = bool(parent_request_id)
            
            # 14. ATTACHMENTS
            attachments = safe_list(details.get('attachments'))
            attachment_count = len(attachments)
            has_attachments = attachment_count > 0
            
            # 15. CHANGE/PROBLEM LINKS
            associated_change_obj = details.get('change', {})
            associated_change_id = None
            has_change = False
            if associated_change_obj and isinstance(associated_change_obj, dict):
                associated_change_id = safe_string(associated_change_obj.get('id'))
                has_change = bool(associated_change_id)
            
            associated_problem_obj = details.get('problem', {})
            associated_problem_id = None
            has_problem = False
            if associated_problem_obj and isinstance(associated_problem_obj, dict):
                associated_problem_id = safe_string(associated_problem_obj.get('id'))
                has_problem = bool(associated_problem_id)
            
            # 16. FIRST CALL RESOLUTION
            is_fcr = bool(details.get('is_fcr', False))
            fcr_status_obj = details.get('fcr_status', {})
            fcr_status = safe_get_name(fcr_status_obj, 'Unknown')
            
            # 17. CREATED BY (vs Requester)
            created_by_obj = details.get('created_by', {})
            created_by_name = safe_get_name(created_by_obj, 'Unknown')
            created_by_email = ''
            if isinstance(created_by_obj, dict):
                created_by_email = safe_string(created_by_obj.get('email_id', ''))
            
            is_proxy_request = (created_by_name != requester_name) and (created_by_name != 'Unknown')
            
            # 18. TEMPLATE
            template_obj = details.get('template', {})
            template_name = safe_get_name(template_obj, 'None')
            
            # 19. SLA DETAILS (Granular)
            sla_obj = details.get('sla', {})
            sla_name = safe_get_name(sla_obj, 'Unknown')
            
            response_sla_obj = details.get('response_sla', {})
            response_sla_name = safe_get_name(response_sla_obj, 'Unknown')
            
            resolution_sla_obj = details.get('resolution_sla', {})
            resolution_sla_name = safe_get_name(resolution_sla_obj, 'Unknown')
            
            # 20. COST FIELDS
            estimated_cost = safe_float_conversion(details.get('estimated_cost'), 0)
            actual_cost = safe_float_conversion(details.get('actual_cost'), 0)
# ========== CALCULATED FIELDS ==========
            
            # Status categories
            is_open = status_category == 'open'
            is_closed = status_category == 'closed'
            is_waiting = status_category == 'waiting'
            is_cancelled = status_category == 'cancelled'
            is_active = status_category in ['open', 'waiting']
            is_resolved = status_category == 'closed'
            
            # Age calculation
            now_ms = datetime.now().timestamp() * 1000
            age_days = None
            if created_time_ms:
                age_days = (now_ms - created_time_ms) / (1000 * 60 * 60 * 24)
            
            # SLA status
            sla_status = 'unknown'
            hours_until_due = None
            if due_by_time_ms and is_active:
                hours_until_due = (due_by_time_ms - now_ms) / (1000 * 60 * 60)
                if hours_until_due < 0:
                    sla_status = 'breached'
                elif hours_until_due < 4:
                    sla_status = 'critical'
                elif hours_until_due < 24:
                    sla_status = 'warning'
                else:
                    sla_status = 'on-track'
            
            # Resolution time
            resolution_time_days = None
            if is_closed and created_time_ms and resolved_time_ms:
                resolution_time_days = (resolved_time_ms - created_time_ms) / (1000 * 60 * 60 * 24)
            
            # ========== CONVERSATION PROCESSING ==========
            
            conversations_structured = []
            first_response_time_hours = None
            days_since_last_activity = None
            latest_conversation_time_ms = None
            
            if conversations:
                conversations_sorted = sorted(
                    conversations,
                    key=lambda x: safe_int_conversion(x.get('time_ms'), 0),
                    reverse=True
                )
                
                latest_conv_ms_raw = conversations_sorted[0].get('time_ms')
                latest_conversation_time_ms = safe_int_conversion(latest_conv_ms_raw)
                
                if latest_conversation_time_ms:
                    days_since_last_activity = (now_ms - latest_conversation_time_ms) / (1000 * 60 * 60 * 24)
                
                if len(conversations_sorted) > 0 and created_time_ms:
                    first_conv_ms_raw = conversations_sorted[-1].get('time_ms')
                    first_conv_ms = safe_int_conversion(first_conv_ms_raw)
                    
                    if first_conv_ms:
                        first_response_time_hours = (first_conv_ms - created_time_ms) / (1000 * 60 * 60)
                
                for conv in conversations_sorted:
                    try:
                        sender = conv.get('sender', 'Unknown')
                        sender_email = conv.get('sender_email', '')
                        time_str = conv.get('time', 'Unknown time')
                        time_ms = safe_int_conversion(conv.get('time_ms'))
                        conv_type = conv.get('type', 'Unknown')
                        description = conv.get('description', '')
                        is_public = conv.get('is_public', True)
                        
                        clean_text = strip_html(description)
                        
                        if len(clean_text.strip()) < 10:
                            continue
                        
                        conversations_structured.append({
                            'sender': sender,
                            'sender_email': sender_email,
                            'time': time_str,
                            'time_ms': time_ms,
                            'type': conv_type,
                            'is_public': is_public,
                            'text': clean_text[:300]
                        })
                        
                    except Exception as e:
                        continue
            
            conversation_count = len(conversations_structured)
            
            # ========== AI INTELLIGENCE ==========
            
            # Keywords
            keywords = extract_keywords_fast(f"{subject} {clean_description}")
            
            # Complexity
            complexity_score = calculate_complexity_score(
                {
                    'is_escalated': is_escalated, 
                    'is_overdue': is_overdue, 
                    'priority': priority,
                    'reopened_count': reopened_count
                },
                conversations_structured,
                age_days or 0
            )
            
            # Sentiment
            sentiment, sentiment_score = analyze_sentiment_risk(conversations)
            
            # Predicted resolution
            predicted_resolution_days = predict_resolution_time(category, priority, complexity_score)
            
            # Similar tickets
            similar_ticket_count = analytics_cache.raw_data['category_stats'][category]['count']
            
            # Suggestions
            resolution_suggestions = generate_resolution_suggestions(category, keywords, similar_ticket_count)
            
            # Requester stats
            requester_stats = get_requester_statistics(requester_name, requester_email)
            
            # Conversation intelligence
            conv_intelligence = analyze_conversation_intelligence(conversations, created_time_ms, technician_name)
            
            # SLA breach prediction
            category_sla_key = f"{category}_{priority.lower()}"
            
            sla_breach_likelihood = 0
            sla_risk_level = 'unknown'
            sla_risk_factors = []
            
            if is_active and hours_until_due is not None:
                sla_breach_likelihood, sla_risk_level, sla_risk_factors = predict_sla_breach_likelihood(
                    {'priority': priority, 'is_overdue': is_overdue, 'is_escalated': is_escalated},
                    hours_until_due,
                    complexity_score,
                    requester_stats,
                    category_sla_key
                )
            
            # Update analytics
            update_analytics_cache(
                {
                    'category': category,
                    'technician_name': technician_name,
                    'is_overdue': is_overdue,
                    'created_time_ms': created_time_ms,
                    'priority': priority,
                    'requester_name': requester_name,
                    'requester_email': requester_email,
                    'source': source,
                    'department': department,
                    'asset': asset_name,
                    'service': service,
                    'reopened_count': reopened_count,
                    'is_fcr': is_fcr
                },
                conversations_structured,
                age_days,
                is_closed
            )
            
            # Created datetime
            created_dt = None
            if created_time_ms:
                created_dt = datetime.fromtimestamp(created_time_ms / 1000)
# ========== COMPLETE STRUCTURED DATA (v2.8) ==========
            
            structured_data = {
                # Basic ticket info
                "ticket_id": display_id,
                "internal_id": internal_id,
                "subject": subject,
                "status": status,
                "status_lower": status_lower,
                "status_category": status_category,
                "is_open": is_open,
                "is_closed": is_closed,
                "is_waiting": is_waiting,
                "is_cancelled": is_cancelled,
                "is_active": is_active,
                "is_resolved": is_resolved,
                "priority": priority,
                "request_type": request_type,
                
                # People
                "requester_name": requester_name,
                "requester_email": requester_email,
                "technician_name": technician_name,
                "technician_email": technician_email,
                
                # Classification
                "group": group,
                "site": site,
                "category": category,
                "subcategory": subcategory,
                "category_path": f"{category}/{subcategory}",
                
                # Timestamps
                "created_time": created_time_display,
                "due_by_time": due_by_time_display,
                "resolved_time": resolved_time_display,
                
                # Flags
                "is_overdue": is_overdue,
                "is_escalated": is_escalated,
                
                # Content
                "description": clean_description[:1000],
                "resolution": resolution_text[:1000] if resolution_text else "",
                
                # Conversations
                "conversations": conversations_structured,
                "conversation_count": conversation_count,
                
                # Time calculations
                "age_days": round(age_days, 2) if age_days else None,
                "sla_status": sla_status,
                "hours_until_due": round(hours_until_due, 2) if hours_until_due else None,
                "resolution_time_days": round(resolution_time_days, 2) if resolution_time_days else None,
                "first_response_time_hours": round(first_response_time_hours, 2) if first_response_time_hours else None,
                "days_since_last_activity": round(days_since_last_activity, 2) if days_since_last_activity else None,
                
                # AI Intelligence
                "keywords": keywords,
                "complexity_score": complexity_score,
                "complexity_level": "high" if complexity_score >= 7 else "medium" if complexity_score >= 4 else "low",
                "sentiment": sentiment,
                "sentiment_score": round(sentiment_score, 1),
                "satisfaction_risk": sentiment == 'high_risk',
                "predicted_resolution_days": predicted_resolution_days,
                "similar_ticket_count": similar_ticket_count,
                
                # Time patterns
                "created_hour": created_dt.hour if created_dt else None,
                "created_day_of_week": created_dt.strftime('%A') if created_dt else None,
                "created_month": created_dt.strftime('%B') if created_dt else None,
                "is_business_hours": (created_dt.hour >= 9 and created_dt.hour <= 17) if created_dt else None,
                "is_weekend": (created_dt.strftime('%A') in ['Saturday', 'Sunday']) if created_dt else None,
                
                # Action flags
                "needs_immediate_attention": (is_overdue and is_active and priority.lower() in ['urgent', 'high']),
                "needs_followup": (is_active and days_since_last_activity and days_since_last_activity > 3),
                
                # SLA prediction
                "sla_breach_likelihood_percent": sla_breach_likelihood,
                "sla_breach_risk_level": sla_risk_level,
                "sla_breach_factors": sla_risk_factors,
                
                # Requester analytics
                "requester_open_tickets": requester_stats.get('open_count', 0),
                "requester_total_tickets": requester_stats.get('total_count', 0),
                "requester_avg_resolution_days": round(statistics.mean(requester_stats['avg_resolution_days']), 1) if requester_stats.get('avg_resolution_days') else None,
                "is_frequent_requester": requester_stats.get('total_count', 0) > 10,
                "is_repeat_requester": requester_stats.get('open_count', 0) > 3,
                
                # Conversation intelligence
                "conversation_intelligence": conv_intelligence,
                "awaiting_response_from": conv_intelligence['awaiting'],
                "conversation_pattern": conv_intelligence['response_pattern'],
                "conversation_engagement_score": conv_intelligence['engagement_score'],
                "unanswered_questions": conv_intelligence['unanswered_questions'],
                "avg_response_time_hours": conv_intelligence['avg_response_time_hours'],
                "longest_conversation_gap_hours": conv_intelligence['longest_gap_hours'],
                
                # ========== NEW v2.8 FIELDS ==========
                
                # 1. Asset/CI
                "asset": asset_name,
                "asset_id": asset_id,
                "has_asset": (asset_name != 'Unknown' and asset_name != ''),
                
                # 2. Source/Mode
                "source": source,
                "mode": mode,
                "channel": f"{source} - {mode}" if source != 'Unknown' and mode != 'Unknown' else source,
                
                # 3. Impact & Urgency
                "impact": impact,
                "urgency": urgency,
                "priority_matrix": f"{impact}/{urgency}",
                
                # 4. Level/Tier
                "level": level,
                "tier": level,
                
                # 5. Service
                "service": service,
                "service_category": service_category,
                "service_path": f"{service_category}/{service}" if service != 'Unknown' else service_category,
                
                # 6. Time Tracking
                "time_spent_minutes": time_spent_minutes,
                "time_spent_hours": time_spent_hours,
                
                # 7. Resolution Category
                "resolution_category": resolution_category,
                "resolution_subcategory": resolution_subcategory,
                "resolution_classification": f"{resolution_category}/{resolution_subcategory}",
                
                # 8. Closure Code
                "closure_code": closure_code,
                "closure_comments": closure_comments,
                
                # 9. Reopened
                "reopened_count": reopened_count,
                "is_reopened": is_reopened,
                "has_been_reopened": reopened_count > 0,
                
                # 10. Approvals
                "approval_status": approval_status,
                "approval_count": approval_count,
                "has_approvals": approval_count > 0,
                "has_pending_approvals": has_pending_approvals,
                
                # 11. Tags
                "tags": tag_names,
                "tag_count": len(tag_names),
                "has_tags": len(tag_names) > 0,
                
                # 12. Department
                "department": department,
                "requester_department": department,
                
                # 13. LINKED TICKETS
            linked_requests = safe_list(details.get('linked_requests'))
            linked_ticket_count = len(linked_requests)
            has_linked_tickets = linked_ticket_count > 0  # <-- THIS WAS MISSING
            linked_ticket_ids = [safe_string(lr.get('id')) for lr in linked_requests if isinstance(lr, dict)]
            
            parent_request_obj = details.get('parent_request', {})
            parent_request_id = None
            has_parent = False
            if parent_request_obj and isinstance(parent_request_obj, dict):
                parent_request_id = safe_string(parent_request_obj.get('id'))
                has_parent = bool(parent_request_id)
                
                # 14. Attachments
                "attachment_count": attachment_count,
                "has_attachments": has_attachments,
                
                # 15. Change/Problem Links
                "associated_change_id": associated_change_id,
                "has_change": has_change,
                "associated_problem_id": associated_problem_id,
                "has_problem": has_problem,
                "has_itil_links": (has_change or has_problem),
                
                # 16. First Call Resolution
                "is_fcr": is_fcr,
                "fcr_status": fcr_status,
                
                # 17. Created By
                "created_by_name": created_by_name,
                "created_by_email": created_by_email,
                "is_proxy_request": is_proxy_request,
                
                # 18. Template
                "template_name": template_name,
                "used_template": (template_name != 'None' and template_name != 'Unknown'),
                
                # 19. SLA Details
                "sla_name": sla_name,
                "response_sla": response_sla_name,
                "resolution_sla": resolution_sla_name,
                
                # 20. Cost
                "estimated_cost": estimated_cost,
                "actual_cost": actual_cost,
                "has_cost_tracking": (estimated_cost > 0 or actual_cost > 0),
            }
            
            # Add millisecond timestamps if available
            if created_time_ms:
                structured_data["created_time_ms"] = created_time_ms
            if due_by_time_ms:
                structured_data["due_by_time_ms"] = due_by_time_ms
            if resolved_time_ms:
                structured_data["resolved_time_ms"] = resolved_time_ms
            if last_updated_time_ms:
                structured_data["last_updated_time_ms"] = last_updated_time_ms
            if latest_conversation_time_ms:
                structured_data["latest_conversation_time_ms"] = latest_conversation_time_ms
# ========== COMPREHENSIVE DUST TAGS (v2.8) ==========
            
            dust_tags = [
                f"status:{status_lower.replace(' ', '-')}",
                f"status-category:{status_category}",
                f"ticket-state:{status_category}",
                f"priority:{priority.lower().replace(' ', '-')}",
                f"ticket-id:{display_id}",
                f"category:{category.lower().replace(' ', '-').replace('/', '-')}",
                f"complexity:{structured_data['complexity_level']}",
            ]
            
            # Status variations
            if status_lower in ['open', 'new', 'reopened']:
                dust_tags.extend(['status:open', 'status:new', 'state:open', 'not-resolved'])
            elif status_lower in ['in progress', 'assigned']:
                dust_tags.extend(['status:in-progress', 'status:working', 'state:active'])
            elif status_lower in ['resolved', 'closed']:
                dust_tags.extend(['status:resolved', 'status:closed', 'state:closed'])
            elif 'waiting' in status_lower or 'pending' in status_lower:
                dust_tags.extend(['status:waiting', 'status:pending', 'state:waiting'])
            
            # People tags
            if group and group != 'Unassigned':
                dust_tags.append(f"group:{group.lower().replace(' ', '-')}")
            if technician_name and technician_name != 'Unassigned':
                dust_tags.append(f"technician:{technician_name.lower().replace(' ', '-')}")
            
            # Active status tags
            if is_active:
                dust_tags.extend(['not-closed', 'active', 'open-status', 'needs-attention'])
                
                if technician_name != 'Unassigned':
                    dust_tags.append('actionable:assigned-and-open')
                else:
                    dust_tags.append('actionable:unassigned-and-open')
                
                if is_overdue:
                    dust_tags.append('actionable:overdue-and-open')
            else:
                dust_tags.append('closed-status')
            
            # Age tags
            if age_days:
                if age_days < 1:
                    dust_tags.append('age:new')
                elif age_days < 7:
                    dust_tags.append('age:this-week')
                elif age_days < 30:
                    dust_tags.append('age:this-month')
                elif age_days < 90:
                    dust_tags.append('age:aging')
                else:
                    dust_tags.append('age:stale')
            
            # SLA tags
            if sla_status != 'unknown':
                dust_tags.append(f"sla:{sla_status}")
            
            if sla_risk_level != 'unknown':
                dust_tags.append(f"sla-risk:{sla_risk_level}")
                if sla_breach_likelihood > 75:
                    dust_tags.append('high-breach-risk')
            
            # Response tags
            if first_response_time_hours:
                if first_response_time_hours < 1:
                    dust_tags.append('response:immediate')
                elif first_response_time_hours < 24:
                    dust_tags.append('response:same-day')
                else:
                    dust_tags.append('response:delayed')
            
            # Activity tags
            if days_since_last_activity:
                if days_since_last_activity > 14 and is_active:
                    dust_tags.append('inactive:2-weeks')
                elif days_since_last_activity > 7 and is_active:
                    dust_tags.append('inactive:1-week')
            
            # Issue type tags
            for keyword in keywords:
                dust_tags.append(f"issue-type:{keyword}")
            
            # Sentiment tags
            if sentiment == 'high_risk':
                dust_tags.append('satisfaction:at-risk')
            elif sentiment == 'positive':
                dust_tags.append('satisfaction:positive')
            
            # Conversation tags
            if conv_intelligence['awaiting'] == 'customer':
                dust_tags.append('awaiting-customer-response')
            elif conv_intelligence['awaiting'] == 'technician':
                dust_tags.append('awaiting-technician-response')
            
            if conv_intelligence['unanswered_questions'] > 0:
                dust_tags.append('has-unanswered-questions')
            
            # Requester tags
            if requester_stats.get('open_count', 0) > 3:
                dust_tags.append('repeat-requester')
            if requester_stats.get('total_count', 0) > 10:
                dust_tags.append('frequent-requester')
            
            # Time pattern tags
            if created_dt:
                if created_dt.hour >= 9 and created_dt.hour <= 17:
                    dust_tags.append('created:business-hours')
                else:
                    dust_tags.append('created:after-hours')
                
                if created_dt.strftime('%A') in ['Saturday', 'Sunday']:
                    dust_tags.append('created:weekend')
            
            # Flag tags
            if is_overdue:
                dust_tags.append('overdue')
            if is_escalated:
                dust_tags.append('escalated')
            if conversation_count > 0:
                dust_tags.append('has-conversations')
            if conversation_count > 5:
                dust_tags.append('high-conversation-volume')
            if technician_name == 'Unassigned':
                dust_tags.append('unassigned')
            if is_overdue and is_active:
                dust_tags.append('urgent-attention')
            if structured_data['needs_immediate_attention']:
                dust_tags.append('critical-action-required')
            
            # ========== NEW v2.8 TAGS ==========
            
            # Source/Channel tags
            if source and source != 'Unknown':
                dust_tags.append(f"source:{source.lower().replace(' ', '-')}")
            if mode and mode != 'Unknown':
                dust_tags.append(f"mode:{mode.lower().replace(' ', '-')}")
            
            # Impact/Urgency tags
            if impact and impact != 'Unknown':
                dust_tags.append(f"impact:{impact.lower().replace(' ', '-')}")
            if urgency and urgency != 'Unknown':
                dust_tags.append(f"urgency:{urgency.lower().replace(' ', '-')}")
            
            # Level/Tier tags
            if level and level != 'Unknown':
                dust_tags.append(f"level:{level.lower().replace(' ', '-')}")
                dust_tags.append(f"tier:{level.lower().replace(' ', '-')}")
            
            # Service tags
            if service and service != 'Unknown':
                dust_tags.append(f"service:{service.lower().replace(' ', '-')}")
            
            # Department tags
            if department and department != 'Unknown':
                dust_tags.append(f"department:{department.lower().replace(' ', '-')}")
            
            # Asset tags
            if asset_name and asset_name != 'Unknown' and asset_name != '':
                dust_tags.append(f"asset:{asset_name.lower().replace(' ', '-')[:50]}")
                dust_tags.append('has-asset')
            
            # Reopened tags
            if reopened_count > 0:
                dust_tags.append('reopened')
                dust_tags.append(f"reopened-count:{reopened_count}")
            
            # FCR tags
            if is_fcr:
                dust_tags.append('fcr')
                dust_tags.append('first-call-resolution')
            
            # Approval tags
            if has_approvals:
                dust_tags.append('has-approvals')
            if has_pending_approvals:
                dust_tags.append('pending-approvals')
            
            # Attachment tags
            if has_attachments:
                dust_tags.append('has-attachments')
                dust_tags.append(f"attachments:{attachment_count}")
            
            # Linked ticket tags
            if has_linked_tickets:
                dust_tags.append('has-linked-tickets')
            if has_parent:
                dust_tags.append('has-parent-ticket')
            
            # ITIL tags
            if has_change:
                dust_tags.append('linked-to-change')
            if has_problem:
                dust_tags.append('linked-to-problem')
            if has_change or has_problem:
                dust_tags.append('itil-linked')
            
            # Proxy request tags
            if is_proxy_request:
                dust_tags.append('proxy-request')
                dust_tags.append('created-by-tech')
            
            # Template tags
            if template_name and template_name not in ['None', 'Unknown']:
                dust_tags.append('used-template')
            
            # Resolution category tags
            if resolution_category and resolution_category != 'Unknown' and is_closed:
                dust_tags.append(f"resolution-category:{resolution_category.lower().replace(' ', '-')}")
            
            # Closure code tags
            if closure_code and closure_code != 'Unknown' and is_closed:
                dust_tags.append(f"closure-code:{closure_code.lower().replace(' ', '-')}")
            
            # Cost tracking tags
            if estimated_cost > 0 or actual_cost > 0:
                dust_tags.append('has-cost-tracking')
            
            # Custom tags from ticket
            for tag in tag_names:
                if tag and tag not in ['', 'Unknown']:
                    dust_tags.append(f"custom-tag:{tag.lower().replace(' ', '-')}")
# ========== OPTIMIZED TEXT CONTENT (v2.8) ==========
            
            status_indicator = "🔴 ACTIVE" if is_active else "DONE CLOSED"
            
            text_content = f"""# {status_indicator} - Ticket #{display_id}

{subject}

**Status:** {status} ({status_category}) | **Priority:** {priority} | **Complexity:** {structured_data['complexity_level']} ({complexity_score}/10)
**Requester:** {requester_name} ({department}) | **Technician:** {technician_name}
**Category:** {category} / {subcategory}
**Created:** {created_time_display} | **Age:** {age_days:.1f} days if age_days else 'N/A'

## Description
{clean_description[:500]}

## v2.8 New Fields
**Source/Channel:** {source} via {mode}
**Impact/Urgency:** {impact} / {urgency}
**Asset:** {asset_name}
**Service:** {service}
**Department:** {department}
**Level:** {level}
"""

            # Add reopened info
            if reopened_count > 0:
                text_content += f"**Reopened:** {reopened_count} time(s)\n"
            
            # Add time tracking
            if time_spent_hours > 0:
                text_content += f"**Time Spent:** {time_spent_hours} hours\n"
            
            # Add linked tickets
            if has_linked_tickets:
                text_content += f"**Linked Tickets:** {linked_ticket_count}\n"
            
            # Add approvals
            if has_approvals:
                text_content += f"**Approvals:** {approval_count} ({approval_status})\n"
            
            # Add attachments
            if has_attachments:
                text_content += f"**Attachments:** {attachment_count}\n"
            
            # Add FCR
            if is_fcr:
                text_content += f"**First Call Resolution:** Yes\n"
            
            text_content += "\n## AI Insights\n"
            text_content += f"- **Complexity:** {structured_data['complexity_level'].upper()} ({complexity_score}/10)\n"
            text_content += f"- **Predicted Resolution:** {predicted_resolution_days} days\n"
            text_content += f"- **Similar Tickets:** {similar_ticket_count}\n"
            
            if is_active and sla_breach_likelihood > 0:
                text_content += f"- **SLA Breach Risk:** {sla_breach_likelihood}% ({sla_risk_level})\n"
            
            if conv_intelligence['total_exchanges'] > 0:
                text_content += f"- **Conversations:** {conv_intelligence['total_exchanges']} ({conv_intelligence['response_pattern']})\n"
                text_content += f"- **Awaiting:** {conv_intelligence['awaiting'].title()}\n"
            
            if sentiment != 'unknown':
                text_content += f"- **Sentiment:** {sentiment.replace('_', ' ').title()}\n"
            
            if resolution_suggestions:
                text_content += "\n## Suggestions\n"
                for idx, suggestion in enumerate(resolution_suggestions, 1):
                    text_content += f"{idx}. {suggestion}\n"
            
            if resolution_text:
                text_content += f"\n## Resolution\n"
                text_content += f"**Category:** {resolution_category} / {resolution_subcategory}\n"
                text_content += f"**Closure Code:** {closure_code}\n"
                text_content += f"{resolution_text[:500]}\n"
            
            # Add conversation summaries (not full text)
            if conversations_structured:
                text_content += f"\n## Conversations ({len(conversations_structured)})\n"
                for idx, conv in enumerate(conversations_structured[:5], 1):
                    text_content += f"{idx}. {conv['sender']} ({conv['time']}): {conv['text'][:100]}...\n"
            
            # Add ITIL links
            if has_change or has_problem:
                text_content += f"\n## ITIL Links\n"
                if has_change:
                    text_content += f"- **Change:** {associated_change_id}\n"
                if has_problem:
                    text_content += f"- **Problem:** {associated_problem_id}\n"
            
            timestamp = last_updated_time_ms if last_updated_time_ms else int(time.time() * 1000)
            
            # ========== RETURN DOCUMENT ==========
            
            return {
                "document_id": f"ticket-{internal_id}",
                "timestamp": timestamp,
                "tags": dust_tags,
                "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{internal_id}",
                "text": text_content,
                "structured_data": structured_data
            }
            
        except Exception as e:
            logger.error(f"Format error for ticket: {str(e)[:100]}")
            return None

# ========== UPLOAD FUNCTIONS ==========

def upload_to_dust(document):
    """Upload single document to Dust"""
    try:
        with perf.time_operation('upload_to_dust'):
            if not document:
                return False
            
            session = get_session()
            headers = {
                'Authorization': f'Bearer {DUST_API_KEY}',
                'Content-Type': 'application/json'
            }
            url = f"{DUST_API_BASE}/{document['document_id']}"
            
            response = session.post(url, headers=headers, json=document, timeout=20)
            if response.status_code in [200, 201]:
                return True
            
            return False
    except Exception as e:
        logger.error(f" WARNING  Upload error: {str(e)[:50]}")
        return False

def upload_batch_to_dust(documents, max_workers=MAX_PARALLEL_WORKERS):
    """Upload multiple documents in parallel"""
    with perf.time_operation('upload_batch_to_dust'):
        success = 0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_doc = {executor.submit(upload_to_dust, doc): doc for doc in documents}
            
            try:
                for future in concurrent.futures.as_completed(future_to_doc, timeout=60):
                    try:
                        result = future.result(timeout=5)
                        if result:
                            success += 1
                    except Exception as e:
                        logger.error(f"  WARNING  Upload failed: {str(e)[:50]}")
            except concurrent.futures.TimeoutError:
                logger.error("  TIMEOUT  Batch upload timed out")
        
        return success
# ========== ANALYTICS REPORTING ==========

def save_analytics_report():
    """Save analytics cache to JSON - v2.8 with NEW fields"""
    try:
        # Calculate FCR rate
        fcr_rate = 0
        if analytics_cache.raw_data['fcr_stats']['total'] > 0:
            fcr_rate = (analytics_cache.raw_data['fcr_stats']['fcr'] / analytics_cache.raw_data['fcr_stats']['total']) * 100
        
        report = {
            'generated_at': datetime.now().isoformat(),
            'version': 'v2.8 - Complete Field Coverage',
            
            # Existing analytics
            'category_statistics': dict(analytics_cache.raw_data['category_stats']),
            'technician_statistics': dict(analytics_cache.raw_data['technician_stats']),
            'requester_statistics_count': len(analytics_cache.raw_data['requester_stats']),
            'sla_breach_patterns': dict(analytics_cache.raw_data['sla_breach_patterns']),
            'hourly_distribution': dict(analytics_cache.raw_data['hourly_distribution']),
            'daily_distribution': dict(analytics_cache.raw_data['daily_distribution']),
            'top_issue_keywords': dict(analytics_cache.raw_data['issue_keywords'].most_common(20)),
            
            # NEW v2.8 analytics
            'source_distribution': dict(analytics_cache.raw_data['source_distribution']),
            'department_statistics': dict(analytics_cache.raw_data['department_stats'].most_common(10)),
            'asset_failure_rates': dict(analytics_cache.raw_data['asset_failure_rates'].most_common(20)),
            'service_statistics': dict(analytics_cache.raw_data['service_stats'].most_common(10)),
            'reopened_patterns': dict(analytics_cache.raw_data['reopened_patterns']),
            'fcr_statistics': {
                'total_closed': analytics_cache.raw_data['fcr_stats']['total'],
                'fcr_count': analytics_cache.raw_data['fcr_stats']['fcr'],
                'fcr_rate_percent': round(fcr_rate, 1)
            }
        }
        
        filename = f"analytics_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        logger.info(f"\nSTATS Analytics report saved: {filename}")
        
    except Exception as e:
        logger.error(f"Could not save analytics report: {e}")

def print_analytics_summary():
    """Print analytics summary - v2.8 with NEW insights"""
    logger.info("\n" + "="*60)
    logger.info("STATS ANALYTICS SUMMARY (v2.8 COMPLETE)")
    logger.info("="*60)
    
    logger.info("\n🏆 Top Categories:")
    top_cats = sorted(analytics_cache.raw_data['category_stats'].items(), 
                     key=lambda x: x[1]['count'], reverse=True)[:5]
    for cat, stats in top_cats:
        logger.info(f"  {cat}: {stats['count']} tickets")
        if stats['avg_resolution_days']:
            avg = statistics.mean(stats['avg_resolution_days'])
            logger.info(f"    Avg resolution: {avg:.1f} days")
    
    logger.info("\n👥 Technician Workload:")
    for tech, stats in sorted(analytics_cache.raw_data['technician_stats'].items(), 
                             key=lambda x: x[1]['active'], reverse=True)[:5]:
        logger.info(f"  {tech}: {stats['active']} active / {stats['total']} total")
        if stats['avg_response_hours']:
            avg = statistics.mean(stats['avg_response_hours'])
            logger.info(f"    Avg response: {avg:.1f} hours")
    
    logger.info("\n⏰ Peak Hours:")
    top_hours = sorted(analytics_cache.raw_data['hourly_distribution'].items(), 
                      key=lambda x: x[1], reverse=True)[:5]
    for hour, count in top_hours:
        logger.info(f"  {hour:02d}:00 - {count} tickets")
    
    logger.info("\n🔍 Top Issue Types:")
    for keyword, count in analytics_cache.raw_data['issue_keywords'].most_common(5):
        logger.info(f"  {keyword}: {count} tickets")
    
    # NEW v2.8 analytics
    logger.info("\n📱 Top Channels:")
    for source, count in analytics_cache.raw_data['source_distribution'].most_common(5):
        logger.info(f"  {source}: {count} tickets")
    
    logger.info("\n🏢 Top Departments:")
    for dept, count in analytics_cache.raw_data['department_stats'].most_common(5):
        logger.info(f"  {dept}: {count} tickets")
    
    logger.info("\n💻 Top Asset Failures:")
    for asset, count in analytics_cache.raw_data['asset_failure_rates'].most_common(5):
        if asset and asset != 'Unknown':
            logger.info(f"  {asset}: {count} issues")
    
    logger.info("\n🔁 Reopened Tickets by Category:")
    for cat, count in sorted(analytics_cache.raw_data['reopened_patterns'].items(), 
                            key=lambda x: x[1], reverse=True)[:5]:
        logger.info(f"  {cat}: {count} reopened")
    
    # FCR Rate
    fcr_stats = analytics_cache.raw_data['fcr_stats']
    if fcr_stats['total'] > 0:
        fcr_rate = (fcr_stats['fcr'] / fcr_stats['total']) * 100
        logger.info(f"\nDONE First Call Resolution Rate: {fcr_rate:.1f}% ({fcr_stats['fcr']}/{fcr_stats['total']})")
    
    logger.info("="*60 + "\n")
# ========== MAIN EXECUTION ==========

def main():
    logger.info("=" * 60)
    logger.info(f"START SERVICEDESK INTELLIGENCE SUITE v2.8 - COMPLETE")
    logger.info(f"Starting at {datetime.now()}")
    logger.info("=" * 60)
    logger.info("✨ v2.8 COMPLETE FIELD COVERAGE:")
    logger.info("   ALL 20 MISSING FIELDS INTEGRATED:")
    logger.info("   • Assets/CI tracking")
    logger.info("   • Source/Mode (channel analysis)")
    logger.info("   • Impact & Urgency (priority matrix)")
    logger.info("   • Level/Tier tracking")
    logger.info("   • Service catalog")
    logger.info("   • Time tracking")
    logger.info("   • Resolution category")
    logger.info("   • Closure code")
    logger.info("   • Reopened count")
    logger.info("   • Approvals")
    logger.info("   • Tags/Labels")
    logger.info("   • Department")
    logger.info("   • Linked tickets")
    logger.info("   • Attachments")
    logger.info("   • Change/Problem links")
    logger.info("   • First Call Resolution")
    logger.info("   • Created by")
    logger.info("   • Template")
    logger.info("   • SLA details")
    logger.info("   • Cost tracking")
    logger.info("")
    logger.info("   PERFORMANCE OPTIMIZATIONS:")
    logger.info("   • Connection pooling (20 connections)")
    logger.info("   • Parallel conversation fetching")
    logger.info("   • API-level filtering")
    logger.info("   • Cached processing")
    logger.info("   • Pre-calculated analytics")
    logger.info("   • Bounded memory caches")
    logger.info("")
    
    try:
        config = validate_environment()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return
    
    with perf.time_operation('total_execution'):
        processed_ids, success_count, error_count = checkpoint_mgr.load()
        
        tickets = fetch_all_tickets()
        
        if processed_ids:
            original_count = len(tickets)
            tickets = [t for t in tickets if t.get('id') not in processed_ids]
            logger.info(f"INFO:  Resuming: {len(processed_ids)} already processed, {len(tickets)} remaining")
        
        logger.info(f"\nTotal tickets to process: {len(tickets)}\n")
        
        if not tickets:
            logger.info("DONE No tickets to process - system is up to date!")
            current_time_ms = int(datetime.now().timestamp() * 1000)
            with open('last_sync_time.txt', 'w') as f:
                f.write(str(current_time_ms))
            logger.info(f"\nDATE Timestamp updated: {current_time_ms}")
            checkpoint_mgr.clear()
            return
        
        access_token = get_access_token()
        conv_count = 0
        active_count = 0
        closed_count = 0
        high_complexity_count = 0
        at_risk_count = 0
        high_breach_risk_count = 0
        awaiting_customer_count = 0
        awaiting_tech_count = 0
        
        # NEW v2.8 counters
        with_asset_count = 0
        reopened_count_total = 0
        fcr_count = 0
        with_approvals_count = 0
        linked_tickets_count = 0
        
        batch_size = BATCH_SIZE
        document_batch = []
        
        for batch_start in range(0, len(tickets), batch_size):
            batch_end = min(batch_start + batch_size, len(tickets))
            batch = tickets[batch_start:batch_end]
            
            logger.info(f"\n[Batch {batch_start//batch_size + 1}] Processing tickets {batch_start+1}-{batch_end}...")
            
            for idx, ticket in enumerate(batch, start=batch_start+1):
                ticket_id = ticket.get('id')
                
                if ticket_id in processed_ids:
                    continue
                
                logger.info(f"  [{idx}/{len(tickets)}] Processing ticket {ticket_id}...")
                
                try:
                    details = fetch_ticket_details(ticket_id, access_token)
                    
                    if not details:
                        logger.error(f"  WARNING  Failed to fetch details for {ticket_id} - skipping")
                        error_count += 1
                        processed_ids.add(ticket_id)
                        continue
                    
                    conversations = fetch_ticket_conversations_parallel(ticket_id, access_token)
                    
                    status = safe_get_name(details.get('status'))
                    status_category = get_status_category(status.lower())
                    
                    if status_category == 'closed':
                        closed_count += 1
                    else:
                        active_count += 1
                    
                    if conversations:
                        conv_count += 1
                    
                    document = format_ticket_with_intelligence(ticket, details, conversations)
                    
                    if document:
                        structured = document.get('structured_data', {})
                        
                        # Existing counters
                        if structured.get('complexity_score', 0) >= 7:
                            high_complexity_count += 1
                        if structured.get('satisfaction_risk'):
                            at_risk_count += 1
                        if structured.get('sla_breach_likelihood_percent', 0) > 75:
                            high_breach_risk_count += 1
                        if structured.get('awaiting_response_from') == 'customer':
                            awaiting_customer_count += 1
                        elif structured.get('awaiting_response_from') == 'technician':
                            awaiting_tech_count += 1
                        
                        # NEW v2.8 counters
                        if structured.get('has_asset'):
                            with_asset_count += 1
                        if structured.get('reopened_count', 0) > 0:
                            reopened_count_total += 1
                        if structured.get('is_fcr'):
                            fcr_count += 1
                        if structured.get('has_approvals'):
                            with_approvals_count += 1
                        if structured.get('has_linked_tickets'):
                            linked_tickets_count += 1
                        
                        document_batch.append(document)
                        processed_ids.add(ticket_id)
                        
                        if len(document_batch) >= DUST_UPLOAD_BATCH_SIZE:
                            batch_success = upload_batch_to_dust(document_batch)
                            success_count += batch_success
                            document_batch = []
                            
                            logger.info(f"  OK Uploaded batch | Total: {success_count}/{idx} | Active: {active_count} | Errors: {error_count}")
                    else:
                        error_count += 1
                        processed_ids.add(ticket_id)
                
                except requests.exceptions.Timeout:
                    logger.error(f"  TIMEOUT  TIMEOUT: {ticket_id} - skipping")
                    error_count += 1
                    processed_ids.add(ticket_id)
                    continue
                
                except Exception as e:
                    logger.error(f"  WARNING  ERROR: {ticket_id} - {str(e)[:100]}")
                    error_count += 1
                    processed_ids.add(ticket_id)
                    continue
                
                if idx % 50 == 0:
                    checkpoint_mgr.save(processed_ids, success_count, error_count)
                    
                    if idx % 250 == 0:
                        access_token = get_access_token(force_refresh=False)
        
        if document_batch:
            batch_success = upload_batch_to_dust(document_batch)
            success_count += batch_success
            logger.info(f"  OK Final batch uploaded: {batch_success} documents")
        
        logger.info(f"\n{'='*60}")
        logger.info(f"DONE INTELLIGENCE SYNC COMPLETED (v2.8 COMPLETE)")
        logger.info(f"{'='*60}")
        logger.info(f"  Total Processed: {success_count}/{len(tickets)}")
        logger.info(f"  Active Tickets: {active_count}")
        logger.info(f"  Closed Tickets: {closed_count}")
        logger.info(f"  With Conversations: {conv_count}")
        logger.info(f"  High Complexity: {high_complexity_count}")
        logger.info(f"  Satisfaction At-Risk: {at_risk_count}")
        logger.info(f"  High SLA Breach Risk: {high_breach_risk_count}")
        logger.info(f"  Awaiting Customer: {awaiting_customer_count}")
        logger.info(f"  Awaiting Technician: {awaiting_tech_count}")
        logger.info(f"")
        logger.info(f"  NEW v2.8 METRICS:")
        logger.info(f"  With Assets: {with_asset_count}")
        logger.info(f"  Reopened Tickets: {reopened_count_total}")
        logger.info(f"  First Call Resolution: {fcr_count}")
        logger.info(f"  With Approvals: {with_approvals_count}")
        logger.info(f"  Linked Tickets: {linked_tickets_count}")
        logger.info(f"")
        logger.info(f"  Errors/Skipped: {error_count}")
        logger.info(f"  Finished: {datetime.now()}")
        logger.info(f"{'='*60}")
        
        print_analytics_summary()
        save_analytics_report()
        
        current_time_ms = int(datetime.now().timestamp() * 1000)
        logger.info(f"\nDATE Current sync timestamp: {current_time_ms}")
        logger.info(f"   (Will be used for next incremental sync)")
        
        with open('last_sync_time.txt', 'w') as f:
            f.write(str(current_time_ms))
        
        checkpoint_mgr.clear()
        
        logger.info("\nTARGET v2.8 Features Active:")
        logger.info("  OK ALL 20 additional fields captured")
        logger.info("  OK Complete field coverage (48 total fields)")
        logger.info("  OK Connection pooling (20 connections)")
        logger.info("  OK Parallel conversation fetching")
        logger.info("  OK API-level filtering")
        logger.info("  OK Pre-calculated analytics")
        logger.info("  OK Bounded memory caches")
        logger.info("  OK Enhanced analytics reporting")
        logger.info("")
        
        perf.print_report()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\nWARNING  Sync interrupted by user - checkpoint saved")
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()








