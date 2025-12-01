#!/usr/bin/env python3
"""
ServiceDesk Plus Intelligence Suite v2.5 - RESILIENT & FAULT-TOLERANT (FIXED)
Major improvements:
- Individual ticket timeouts (skip stuck tickets)
- Resume capability (checkpoint progress)
- Better error isolation
- Per-ticket logging
- Automatic retry logic
- Graceful degradation
- Thread-safe timeout handling
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape
from collections import defaultdict, Counter
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

# Settings - OPTIMIZED for resilience
TICKETS_PER_PAGE = 100
MAX_TICKETS = 10000
MAX_RETRIES = 3
MIN_PAGES_TO_CHECK = 5
BATCH_SIZE = 25  # Smaller batches for better error isolation
DUST_UPLOAD_BATCH_SIZE = 10
MAX_PARALLEL_WORKERS = 5
CHECKPOINT_FILE = 'sync_checkpoint.json'

# Status categorization
STATUS_CATEGORIES = {
    'open': ['open', 'new', 'in progress', 'reopened', 'pending', 'assigned'],
    'closed': ['closed', 'resolved', 'completed', 'done'],
    'waiting': ['waiting', 'on hold', 'pending approval', 'awaiting'],
    'cancelled': ['cancelled', 'rejected', 'declined']
}

# Analytics Cache
analytics_cache = {
    'category_stats': defaultdict(lambda: {'count': 0, 'avg_resolution_days': [], 'overdue_count': 0, 'sla_breach_count': 0}),
    'technician_stats': defaultdict(lambda: {'active': 0, 'total': 0, 'avg_response_hours': []}),
    'requester_stats': defaultdict(lambda: {'open_count': 0, 'total_count': 0, 'avg_resolution_days': [], 'satisfaction_scores': []}),
    'hourly_distribution': defaultdict(int),
    'daily_distribution': defaultdict(int),
    'issue_keywords': Counter(),
    'resolution_patterns': defaultdict(list),
    'sla_breach_patterns': defaultdict(lambda: {'total': 0, 'breached': 0})
}

# Token cache
_access_token = None
_token_expiry = None

# Performance Monitor
class PerformanceMonitor:
    def __init__(self):
        self.timings = defaultdict(list)
        self.operation_start = None
    
    def time_operation(self, operation_name):
        """Context manager for timing operations"""
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
        logger.info("📊 PERFORMANCE REPORT")
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
        """Save progress checkpoint"""
        try:
            checkpoint = {
                'timestamp': datetime.now().isoformat(),
                'processed_ids': list(processed_ids),
                'success_count': success_count,
                'error_count': error_count
            }
            with open(self.checkpoint_file, 'w') as f:
                json.dump(checkpoint, f)
            logger.info(f"💾 Checkpoint saved: {success_count} successful, {error_count} errors")
        except Exception as e:
            logger.error(f"Could not save checkpoint: {e}")
    
    def load(self):
        """Load progress checkpoint"""
        try:
            if os.path.exists(self.checkpoint_file):
                with open(self.checkpoint_file, 'r') as f:
                    checkpoint = json.load(f)
                logger.info(f"📂 Loaded checkpoint: {len(checkpoint['processed_ids'])} tickets already processed")
                return set(checkpoint['processed_ids']), checkpoint['success_count'], checkpoint['error_count']
        except Exception as e:
            logger.error(f"Could not load checkpoint: {e}")
        
        return set(), 0, 0
    
    def clear(self):
        """Clear checkpoint file"""
        try:
            if os.path.exists(self.checkpoint_file):
                os.remove(self.checkpoint_file)
                logger.info("🗑️  Checkpoint cleared")
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
    
    logger.info("✓ Environment validation passed")
    return config

def get_access_token(force_refresh=False):
    """Get a valid access token using refresh token"""
    global _access_token, _token_expiry
    
    if not force_refresh and _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    logger.info("Getting access token...")
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        data = response.json()
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        logger.info("✓ Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def safe_get_value(obj, default=None):
    """Get the value field from a dict or convert to int"""
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
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('name', default))
    return safe_string(obj, default)

def safe_get_display_value(obj, default=''):
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

def fetch_all_tickets():
    """Fetch tickets - full or incremental based on last sync time"""
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
                logger.info(f"📊 Incremental sync: Fetching tickets updated since {last_sync_dt}")
                incremental = True
            except:
                logger.warning("⚠️  Invalid last sync time, performing full sync")
                incremental = False
        else:
            logger.info("🔄 No last sync time found - performing initial full sync")
            incremental = False
        
        logger.info(f"Fetching {'updated' if incremental else 'all'} tickets...")
        
        if incremental:
            logger.info(f"  ℹ️  Will check minimum {MIN_PAGES_TO_CHECK * TICKETS_PER_PAGE} tickets for thoroughness")
        
        pages_checked = 0
        tickets_found = 0
        
        if incremental:
            page_limit = 20
        else:
            page_limit = 100
        
        logger.info(f"  📊 Page limit: {page_limit} pages (up to {page_limit * TICKETS_PER_PAGE} tickets)")
        
        while len(seen_ticket_ids) < MAX_TICKETS and pages_checked < page_limit:
            headers = {
                'Authorization': f'Zoho-oauthtoken {access_token}',
                'Accept': 'application/vnd.manageengine.sdp.v3+json'
            }
            
            if incremental:
                sort_field = "last_updated_time"
            else:
                sort_field = "created_time"
            
            list_info = {
                "list_info": {
                    "row_count": TICKETS_PER_PAGE,
                    "start_index": start_index,
                    "sort_field": sort_field,
                    "sort_order": "desc"
                }
            }
            
            params = {'input_data': json.dumps(list_info)}
            url = f"{SDP_API_BASE}/requests"
            
            try:
                response = requests.get(url, headers=headers, params=params, timeout=30)
                
                if response.status_code == 200:
                    result = response.json()
                    tickets = result.get('requests', [])
                    
                    if not tickets:
                        logger.info(f"  ✓ No more tickets available from API")
                        break
                    
                    pages_checked += 1
                    
                    if incremental:
                        oldest_updated_in_page = None
                        page_added = 0
                        
                        for ticket in tickets:
                            ticket_id = ticket.get('id')
                            if ticket_id in seen_ticket_ids:
                                continue
                            
                            seen_ticket_ids.add(ticket_id)
                            
                            created_ms = safe_get_value(ticket.get('created_time'))
                            updated_ms = safe_get_value(ticket.get('last_updated_time'))
                            
                            if updated_ms:
                                if oldest_updated_in_page is None or updated_ms < oldest_updated_in_page:
                                    oldest_updated_in_page = updated_ms
                            
                            should_include = False
                            if created_ms and created_ms > last_sync_ms:
                                should_include = True
                            elif updated_ms and updated_ms > last_sync_ms:
                                should_include = True
                            
                            if should_include:
                                all_tickets.append(ticket)
                                page_added += 1
                                tickets_found += 1
                        
                        if page_added > 0:
                            logger.info(f"  Page {pages_checked}: Found {page_added} tickets to sync")
                        
                        if pages_checked >= MIN_PAGES_TO_CHECK:
                            if oldest_updated_in_page and oldest_updated_in_page < last_sync_ms:
                                oldest_dt = datetime.fromtimestamp(oldest_updated_in_page / 1000)
                                logger.info(f"  ℹ️  Checked minimum {MIN_PAGES_TO_CHECK * TICKETS_PER_PAGE} tickets")
                                logger.info(f"  ✓ Reached tickets older than last sync (oldest: {oldest_dt})")
                                logger.info(f"  Total checked: {pages_checked} pages, found {tickets_found} tickets to sync")
                                break
                    
                    else:
                        for ticket in tickets:
                            ticket_id = ticket.get('id')
                            if ticket_id not in seen_ticket_ids:
                                all_tickets.append(ticket)
                                seen_ticket_ids.add(ticket_id)
                    
                    response_list_info = result.get('list_info', {})
                    has_more = response_list_info.get('has_more_rows', False)
                    
                    if not has_more or len(tickets) == 0:
                        if incremental and pages_checked < MIN_PAGES_TO_CHECK:
                            logger.info(f"  ℹ️  API reports no more rows after {pages_checked} pages")
                        break
                    
                    start_index += TICKETS_PER_PAGE
                    time.sleep(0.5)
                    
                else:
                    logger.warning(f"  ⚠️  API returned status {response.status_code}")
                    break
                    
            except Exception as e:
                logger.error(f"  ⚠️  Error fetching tickets: {e}")
                break
        
        sync_type = "incremental" if incremental else "full"
        logger.info(f"✓ Fetched {len(all_tickets)} tickets ({sync_type} sync)")
        return all_tickets

def fetch_with_retry(url, headers, max_retries=MAX_RETRIES, timeout=30):
    """Fetch URL with retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 401:
                access_token = get_access_token(force_refresh=True)
                headers['Authorization'] = f'Zoho-oauthtoken {access_token}'
                continue
            elif response.status_code == 429:
                time.sleep(5)
                continue
            else:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
        except requests.exceptions.Timeout:
            logger.warning(f"  ⏱️  Timeout on attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
    
    return None

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket with timeout"""
    try:
        with perf.time_operation('fetch_ticket_details'):
            headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
            url = f"{SDP_API_BASE}/requests/{ticket_id}"
            
            response = fetch_with_retry(url, headers, timeout=20)
            
            if response and response.status_code == 200:
                return response.json().get('request', {})
            return None
    except Exception as e:
        logger.error(f"  ⚠️  Error fetching details for {ticket_id}: {str(e)[:100]}")
        return None

def fetch_ticket_conversations_single(ticket_id, access_token):
    """Fetch ticket conversations for a single ticket with timeout"""
    try:
        headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
        
        url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            return []
        
        notifications = response.json().get('notifications', [])
        conversations = []
        
        for notif in notifications[:20]:
            notif_id = notif.get('id')
            notif_type = notif.get('type', '')
            
            if any(keyword in notif_type.lower() for keyword in ['conversation', 'reply', 'reqreply', 'technician']):
                try:
                    detail_url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications/{notif_id}"
                    detail_response = requests.get(detail_url, headers=headers, timeout=10)
                    
                    if detail_response.status_code == 200:
                        detail_data = detail_response.json()
                        notification = detail_data.get('notification', {})
                        
                        description = notification.get('description', '')
                        if description and len(description.strip()) > 0:
                            sender = notification.get('sender', {})
                            time_obj = notification.get('time', {})
                            
                            conversations.append({
                                'id': notif_id,
                                'type': notif_type,
                                'sender': sender.get('name', 'Unknown'),
                                'sender_email': sender.get('email_id', ''),
                                'time': time_obj.get('display_value', 'Unknown'),
                                'time_ms': time_obj.get('value'),
                                'description': description,
                                'is_public': notification.get('is_public', True)
                            })
                    
                    time.sleep(0.1)
                    
                except requests.exceptions.Timeout:
                    continue
                except Exception as e:
                    continue
        
        return conversations
    except requests.exceptions.Timeout:
        return []
    except Exception as e:
        logger.error(f"  ⚠️  Error fetching conversations for {ticket_id}: {str(e)[:50]}")
        return []

def strip_html(html_text):
    """Remove HTML tags and decode HTML entities"""
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

def extract_keywords(text):
    """Extract key issue keywords from text"""
    if not text:
        return []
    
    text_lower = text.lower()
    
    keywords = []
    patterns = {
        'access': ['cannot access', 'access denied', 'login', 'password', 'locked out', 'permission'],
        'performance': ['slow', 'hanging', 'freeze', 'timeout', 'not responding', 'lag'],
        'error': ['error', 'exception', 'failed', 'crash', 'bug', 'issue'],
        'hardware': ['laptop', 'computer', 'printer', 'monitor', 'keyboard', 'mouse', 'device'],
        'software': ['application', 'program', 'software', 'app', 'system'],
        'network': ['network', 'internet', 'wifi', 'connection', 'vpn', 'offline'],
        'email': ['email', 'outlook', 'mail', 'inbox', 'send', 'receive'],
        'account': ['account', 'user', 'profile', 'credentials']
    }
    
    for category, terms in patterns.items():
        if any(term in text_lower for term in terms):
            keywords.append(category)
            analytics_cache['issue_keywords'][category] += 1
    
    return keywords

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
    
    return min(score, 10)

def predict_sla_breach_likelihood(ticket_data, hours_until_due, complexity_score, requester_history, category_stats):
    """Predict likelihood of SLA breach as percentage"""
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
    
    if category_stats and category_stats.get('total') > 10:
        breach_rate = category_stats.get('breached', 0) / category_stats['total']
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
    """Get or create requester statistics from cache"""
    key = requester_email if requester_email else requester_name
    return analytics_cache['requester_stats'][key]

def predict_resolution_time(category, priority, complexity_score):
    """Predict resolution time based on historical patterns"""
    
    if category in analytics_cache['category_stats']:
        cat_stats = analytics_cache['category_stats'][category]
        if cat_stats['avg_resolution_days']:
            avg_days = statistics.mean(cat_stats['avg_resolution_days'])
        else:
            avg_days = 3.0
    else:
        avg_days = 3.0
    
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
            "Document any dependencies or blockers",
            "Schedule follow-up checkpoints"
        ],
        'IT - General IT Support': [
            "Gather detailed information (screenshots, error messages)",
            "Search for similar past tickets for resolution patterns",
            "Attempt basic troubleshooting steps",
            "Check knowledge base for documented solutions",
            "Escalate if issue persists after initial troubleshooting"
        ],
        'IT - Network/Infrastructure': [
            "Check network connectivity and DNS resolution",
            "Verify firewall rules and security groups",
            "Test from different network/location",
            "Review recent infrastructure changes",
            "Check service health dashboard"
        ]
    }
    
    if category in kb_templates:
        suggestions.extend(kb_templates[category])
    
    if 'access' in keywords or 'password' in keywords:
        suggestions.append("🔐 Reset user credentials and verify account is not locked")
    
    if 'performance' in keywords:
        suggestions.append("⚡ Check system resources, clear cache, and restart application")
    
    if 'error' in keywords:
        suggestions.append("🐛 Collect error logs and error codes for troubleshooting")
    
    if 'network' in keywords:
        suggestions.append("🌐 Test connectivity, check DNS, and verify VPN status")
    
    if similar_count > 3:
        suggestions.append(f"📊 {similar_count} similar tickets found - review for common solutions")
    
    return suggestions[:7]

def generate_auto_assignment_suggestion(category, group, current_workload):
    """Suggest best technician for assignment"""
    
    available_techs = []
    
    for tech, stats in analytics_cache['technician_stats'].items():
        if stats['active'] < 10:
            available_techs.append({
                'name': tech,
                'active_tickets': stats['active'],
                'total_experience': stats['total'],
                'avg_response': statistics.mean(stats['avg_response_hours']) if stats['avg_response_hours'] else 24
            })
    
    if not available_techs:
        return "All technicians at capacity - consider load balancing"
    
    available_techs.sort(key=lambda x: (x['active_tickets'], x['avg_response']))
    
    best_tech = available_techs[0]
    
    return f"Suggest: {best_tech['name']} (Active: {best_tech['active_tickets']}, Avg Response: {best_tech['avg_response']:.1f}h)"

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

def update_analytics_cache(ticket_data, conversations, age_days, is_closed):
    """Update global analytics cache including requester tracking"""
    
    category = ticket_data.get('category', 'Unknown')
    technician = ticket_data.get('technician_name', 'Unassigned')
    requester_name = ticket_data.get('requester_name', 'Unknown')
    requester_email = ticket_data.get('requester_email', '')
    
    analytics_cache['category_stats'][category]['count'] += 1
    if ticket_data.get('is_overdue'):
        analytics_cache['category_stats'][category]['overdue_count'] += 1
    if is_closed and age_days:
        analytics_cache['category_stats'][category]['avg_resolution_days'].append(age_days)
    
    category_key = f"{category}_{ticket_data.get('priority', 'normal')}"
    analytics_cache['sla_breach_patterns'][category_key]['total'] += 1
    if ticket_data.get('is_overdue'):
        analytics_cache['sla_breach_patterns'][category_key]['breached'] += 1
    
    if technician != 'Unassigned':
        analytics_cache['technician_stats'][technician]['total'] += 1
        if not is_closed:
            analytics_cache['technician_stats'][technician]['active'] += 1
        
        if conversations and len(conversations) > 0:
            first_conv = conversations[-1]
            first_response_ms = safe_int_conversion(first_conv.get('time_ms'))
            created_ms = ticket_data.get('created_time_ms')
            if first_response_ms and created_ms:
                response_hours = (first_response_ms - created_ms) / (1000 * 60 * 60)
                if response_hours > 0:
                    analytics_cache['technician_stats'][technician]['avg_response_hours'].append(response_hours)
    
    req_key = requester_email if requester_email else requester_name
    analytics_cache['requester_stats'][req_key]['total_count'] += 1
    if not is_closed:
        analytics_cache['requester_stats'][req_key]['open_count'] += 1
    if is_closed and age_days:
        analytics_cache['requester_stats'][req_key]['avg_resolution_days'].append(age_days)
    
    created_ms = ticket_data.get('created_time_ms')
    if created_ms:
        created_dt = datetime.fromtimestamp(created_ms / 1000)
        analytics_cache['hourly_distribution'][created_dt.hour] += 1
        analytics_cache['daily_distribution'][created_dt.strftime('%A')] += 1
def format_ticket_with_intelligence(ticket, details, conversations):
    """Format ticket with MAXIMUM intelligence - v2.5 RESILIENT"""
    
    with perf.time_operation('format_ticket'):
        try:
            internal_id = safe_string(ticket.get('id'))
            display_id = get_display_id(details)
            subject = safe_string(details.get('subject', 'No Subject'))
            
            status_obj = details.get('status', {})
            status = safe_get_name(status_obj)
            status_lower = status.lower()
            status_internal_id = status_obj.get('id') if isinstance(status_obj, dict) else None
            status_category = get_status_category(status_lower)
            
            priority = safe_get_name(details.get('priority'))
            
            requester_obj = details.get('requester', {})
            requester_name = safe_get_name(requester_obj)
            requester_email = safe_string(requester_obj.get('email_id', '') if isinstance(requester_obj, dict) else '')
            
            technician_obj = details.get('technician', {})
            technician_name = safe_get_name(technician_obj, 'Unassigned')
            technician_email = safe_string(technician_obj.get('email_id', '') if isinstance(technician_obj, dict) else '')
            
            category = safe_get_name(details.get('category'), 'Uncategorized')
            subcategory = safe_get_name(details.get('subcategory'), 'None')
            
            group = safe_get_name(details.get('group'), 'Unassigned')
            site = safe_get_name(details.get('site'), 'No site')
            
            request_type = safe_get_name(details.get('request_type'), 'Incident')
            
            created_time_ms = safe_get_value(details.get('created_time'))
            due_by_time_ms = safe_get_value(details.get('due_by_time'))
            resolved_time_ms = safe_get_value(details.get('resolved_time'))
            last_updated_time_ms = safe_get_value(details.get('last_updated_time'))
            
            created_time_display = safe_get_display_value(details.get('created_time'))
            due_by_time_display = safe_get_display_value(details.get('due_by_time'), 'Not set')
            resolved_time_display = safe_get_display_value(details.get('resolved_time'), 'Not resolved')
            
            is_overdue = bool(details.get('is_overdue', False))
            is_escalated = bool(details.get('is_escalated', False))
            
            raw_description = details.get('description', '')
            clean_description = strip_html(raw_description)
            
            resolution_obj = details.get('resolution')
            resolution_text = ''
            if resolution_obj and isinstance(resolution_obj, dict):
                resolution_content = resolution_obj.get('content', '')
                if resolution_content:
                    resolution_text = strip_html(resolution_content)
            
            is_open = status_category == 'open'
            is_closed = status_category == 'closed'
            is_waiting = status_category == 'waiting'
            is_cancelled = status_category == 'cancelled'
            is_active = status_category in ['open', 'waiting']
            is_resolved = status_category == 'closed'
            
            now_ms = datetime.now().timestamp() * 1000
            age_days = None
            if created_time_ms:
                age_days = (now_ms - created_time_ms) / (1000 * 60 * 60 * 24)
            
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
            
            resolution_time_days = None
            if is_closed and created_time_ms and resolved_time_ms:
                resolution_time_days = (resolved_time_ms - created_time_ms) / (1000 * 60 * 60 * 24)
            
            conversations_structured = []
            conversations_text = ""
            conversation_search_text = ""
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
                
                conversations_text = "\n\n## 💬 Conversations & Replies\n\n"
                conversations_text += f"Total: {len(conversations_sorted)} (newest first)\n\n"
                
                for idx, conv in enumerate(conversations_sorted, 1):
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
                            'text': clean_text[:500]
                        })
                        
                        conversation_search_text += f"{sender} said: {clean_text[:200]} | "
                        
                        visibility = "Public" if is_public else "Private"
                        conversations_text += f"### Reply {idx} - {time_str}\n"
                        conversations_text += f"**From:** {sender}"
                        if sender_email:
                            conversations_text += f" ({sender_email})"
                        conversations_text += f"\n**Type:** {conv_type} ({visibility})\n\n"
                        conversations_text += f"{clean_text}\n\n"
                        conversations_text += "---\n\n"
                        
                    except Exception as e:
                        continue
            
            conversation_count = len(conversations_structured)
            
            keywords = extract_keywords(f"{subject} {clean_description}")
            
            complexity_score = calculate_complexity_score(
                {'is_escalated': is_escalated, 'is_overdue': is_overdue, 'priority': priority},
                conversations_structured,
                age_days or 0
            )
            
            sentiment, sentiment_score = analyze_sentiment_risk(conversations)
            
            predicted_resolution_days = predict_resolution_time(category, priority, complexity_score)
            
            similar_ticket_count = analytics_cache['category_stats'][category]['count']
            
            resolution_suggestions = generate_resolution_suggestions(category, keywords, similar_ticket_count)
            
            assignment_suggestion = generate_auto_assignment_suggestion(category, group, analytics_cache['technician_stats'])
            
            requester_stats = get_requester_statistics(requester_name, requester_email)
            
            conv_intelligence = analyze_conversation_intelligence(conversations, created_time_ms, technician_name)
            
            category_sla_key = f"{category}_{priority.lower()}"
            category_sla_stats = analytics_cache['sla_breach_patterns'].get(category_sla_key)
            
            sla_breach_likelihood = 0
            sla_risk_level = 'unknown'
            sla_risk_factors = []
            
            if is_active and hours_until_due is not None:
                sla_breach_likelihood, sla_risk_level, sla_risk_factors = predict_sla_breach_likelihood(
                    {'priority': priority, 'is_overdue': is_overdue, 'is_escalated': is_escalated},
                    hours_until_due,
                    complexity_score,
                    requester_stats,
                    category_sla_stats
                )
            
            update_analytics_cache(
                {
                    'category': category,
                    'technician_name': technician_name,
                    'is_overdue': is_overdue,
                    'created_time_ms': created_time_ms,
                    'priority': priority,
                    'requester_name': requester_name,
                    'requester_email': requester_email
                },
                conversations_structured,
                age_days,
                is_closed
            )
            
            created_dt = None
            if created_time_ms:
                created_dt = datetime.fromtimestamp(created_time_ms / 1000)
            
            structured_data = {
                "ticket_id": display_id,
                "internal_id": internal_id,
                "subject": subject,
                "status": status,
                "status_lower": status_lower,
                "status_category": status_category,
                "status_internal_id": status_internal_id,
                "is_open": is_open,
                "is_closed": is_closed,
                "is_waiting": is_waiting,
                "is_cancelled": is_cancelled,
                "is_active": is_active,
                "is_resolved": is_resolved,
                "priority": priority,
                "request_type": request_type,
                "requester_name": requester_name,
                "requester_email": requester_email,
                "technician_name": technician_name,
                "technician_email": technician_email,
                "group": group,
                "site": site,
                "category": category,
                "subcategory": subcategory,
                "category_path": f"{category}/{subcategory}",
                "created_time": created_time_display,
                "due_by_time": due_by_time_display,
                "resolved_time": resolved_time_display,
                "is_overdue": is_overdue,
                "is_escalated": is_escalated,
                "description": clean_description[:1000],
                "resolution": resolution_text[:1000] if resolution_text else "",
                "conversations": conversations_structured,
                "conversation_count": conversation_count,
                "age_days": round(age_days, 2) if age_days else None,
                "sla_status": sla_status,
                "hours_until_due": round(hours_until_due, 2) if hours_until_due else None,
                "resolution_time_days": round(resolution_time_days, 2) if resolution_time_days else None,
                "first_response_time_hours": round(first_response_time_hours, 2) if first_response_time_hours else None,
                "days_since_last_activity": round(days_since_last_activity, 2) if days_since_last_activity else None,
                "keywords": keywords,
                "complexity_score": complexity_score,
                "complexity_level": "high" if complexity_score >= 7 else "medium" if complexity_score >= 4 else "low",
                "sentiment": sentiment,
                "sentiment_score": round(sentiment_score, 1),
                "satisfaction_risk": sentiment == 'high_risk',
                "predicted_resolution_days": predicted_resolution_days,
                "similar_ticket_count": similar_ticket_count,
                "created_hour": created_dt.hour if created_dt else None,
                "created_day_of_week": created_dt.strftime('%A') if created_dt else None,
                "created_month": created_dt.strftime('%B') if created_dt else None,
                "is_business_hours": (created_dt.hour >= 9 and created_dt.hour <= 17) if created_dt else None,
                "is_weekend": (created_dt.strftime('%A') in ['Saturday', 'Sunday']) if created_dt else None,
                "needs_immediate_attention": (is_overdue and is_active and priority.lower() in ['urgent', 'high']),
                "needs_followup": (is_active and days_since_last_activity and days_since_last_activity > 3),
                "auto_assignment_suggestion": assignment_suggestion if technician_name == 'Unassigned' else None,
                "sla_breach_likelihood_percent": sla_breach_likelihood,
                "sla_breach_risk_level": sla_risk_level,
                "sla_breach_factors": sla_risk_factors,
                "requester_open_tickets": requester_stats.get('open_count', 0),
                "requester_total_tickets": requester_stats.get('total_count', 0),
                "requester_avg_resolution_days": round(statistics.mean(requester_stats['avg_resolution_days']), 1) if requester_stats.get('avg_resolution_days') else None,
                "is_frequent_requester": requester_stats.get('total_count', 0) > 10,
                "is_repeat_requester": requester_stats.get('open_count', 0) > 3,
                "conversation_intelligence": conv_intelligence,
                "awaiting_response_from": conv_intelligence['awaiting'],
                "conversation_pattern": conv_intelligence['response_pattern'],
                "conversation_engagement_score": conv_intelligence['engagement_score'],
                "unanswered_questions": conv_intelligence['unanswered_questions'],
                "avg_response_time_hours": conv_intelligence['avg_response_time_hours'],
                "longest_conversation_gap_hours": conv_intelligence['longest_gap_hours'],
                "search_all_text": f"{subject} {clean_description} {category} {subcategory} {requester_name} {technician_name}",
                "search_keywords": ' '.join(keywords),
                "search_people": f"{requester_name} {requester_email} {technician_name} {technician_email}",
                "search_classification": f"{category} {subcategory} {group} {site}",
                "searchable_status": f"{status} {status_category}",
                "status_search_terms": f"status:{status_lower} category:{status_category} {'open' if is_open else 'closed' if is_closed else ''}",
                "conversation_search_text": conversation_search_text,
                "has_conversations": len(conversations_structured) > 0,
            }
            
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
            
            dust_tags = [
                f"status:{status_lower.replace(' ', '-')}",
                f"status-category:{status_category}",
                f"ticket-state:{status_category}",
                f"priority:{priority.lower().replace(' ', '-')}",
                f"ticket-id:{display_id}",
                f"category:{category.lower().replace(' ', '-').replace('/', '-')}",
                f"complexity:{structured_data['complexity_level']}",
            ]
            
            if status_lower in ['open', 'new', 'reopened']:
                dust_tags.extend(['status:open', 'status:new', 'state:open', 'not-resolved'])
            elif status_lower in ['in progress', 'assigned']:
                dust_tags.extend(['status:in-progress', 'status:working', 'state:active'])
            elif status_lower in ['resolved', 'closed']:
                dust_tags.extend(['status:resolved', 'status:closed', 'state:closed'])
            elif 'waiting' in status_lower or 'pending' in status_lower:
                dust_tags.extend(['status:waiting', 'status:pending', 'state:waiting'])
            
            if group and group != 'Unassigned':
                dust_tags.append(f"group:{group.lower().replace(' ', '-')}")
            if technician_name and technician_name != 'Unassigned':
                dust_tags.append(f"technician:{technician_name.lower().replace(' ', '-')}")
            
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
            
            if sla_status != 'unknown':
                dust_tags.append(f"sla:{sla_status}")
            
            if sla_risk_level != 'unknown':
                dust_tags.append(f"sla-risk:{sla_risk_level}")
                if sla_breach_likelihood > 75:
                    dust_tags.append('high-breach-risk')
            
            if first_response_time_hours:
                if first_response_time_hours < 1:
                    dust_tags.append('response:immediate')
                elif first_response_time_hours < 24:
                    dust_tags.append('response:same-day')
                else:
                    dust_tags.append('response:delayed')
            
            if days_since_last_activity:
                if days_since_last_activity > 14 and is_active:
                    dust_tags.append('inactive:2-weeks')
                elif days_since_last_activity > 7 and is_active:
                    dust_tags.append('inactive:1-week')
            
            for keyword in keywords:
                dust_tags.append(f"issue-type:{keyword}")
            
            if sentiment == 'high_risk':
                dust_tags.append('satisfaction:at-risk')
            elif sentiment == 'positive':
                dust_tags.append('satisfaction:positive')
            
            if conv_intelligence['awaiting'] == 'customer':
                dust_tags.append('awaiting-customer-response')
            elif conv_intelligence['awaiting'] == 'technician':
                dust_tags.append('awaiting-technician-response')
            
            if conv_intelligence['unanswered_questions'] > 0:
                dust_tags.append('has-unanswered-questions')
            
            if requester_stats.get('open_count', 0) > 3:
                dust_tags.append('repeat-requester')
            if requester_stats.get('total_count', 0) > 10:
                dust_tags.append('frequent-requester')
            
            if created_dt:
                if created_dt.hour >= 9 and created_dt.hour <= 17:
                    dust_tags.append('created:business-hours')
                else:
                    dust_tags.append('created:after-hours')
                
                if created_dt.strftime('%A') in ['Saturday', 'Sunday']:
                    dust_tags.append('created:weekend')
            
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
            
            status_indicator = "🔴 ACTIVE TICKET" if is_active else "✅ CLOSED"
            status_emoji = "🔴" if is_active else "✅"
            complexity_emoji = "🔥" if complexity_score >= 7 else "⚠️" if complexity_score >= 4 else "📝"
            
            text_content = f"""
## 🔍 SEARCHABLE TICKET SUMMARY
Ticket: #{display_id} | Status: {status} | Category: {status_category.upper()}
Subject: {subject}
Requester: {requester_name} ({requester_email})
Technician: {technician_name}
Category: {category} / {subcategory}
Priority: {priority} | Type: {request_type}
Created: {created_time_display}
Age: {age_days:.1f} days if age_days else 'New'

Status Details: {status} - {'ACTIVE/OPEN' if status_category == 'open' else 'CLOSED/RESOLVED' if status_category == 'closed' else status_category.upper()}
Active: {'YES' if is_active else 'NO'}
Closed: {'YES' if is_closed else 'NO'}
Needs Action: {'YES' if structured_data.get('needs_immediate_attention') else 'NO'}

Keywords: {', '.join(keywords) if keywords else 'None'}
---

{status_indicator} - Ticket #{display_id}: {subject}

## 🎯 STATUS INFORMATION
CURRENT_STATUS: {status.upper()}
STATUS_VALUE: {status}
STATUS_CATEGORY: {status_category.upper()}
IS_ACTIVE: {'YES' if is_active else 'NO'}
IS_CLOSED: {'YES' if is_closed else 'NO'}
IS_OPEN: {'YES' if is_open else 'NO'}
IS_WAITING: {'YES' if is_waiting else 'NO'}
IS_RESOLVED: {'YES' if is_resolved else 'NO'}
TICKET_STATE: {status_category.upper()}

---

{status_emoji} **Ticket ID:** {display_id}
📊 **Status:** {status} ({status_category}) | **Priority:** {priority}
{complexity_emoji} **Complexity:** {structured_data['complexity_level'].upper()} ({complexity_score}/10)
📋 **Type:** {request_type}
"""

            if age_days:
                text_content += f"📅 **Age:** {age_days:.1f} days\n"
            
            if sla_status != 'unknown':
                sla_emoji = "🔥" if sla_status == 'breached' else "⚠️" if sla_status == 'critical' else "✅"
                text_content += f"{sla_emoji} **SLA Status:** {sla_status.upper()}\n"
                if hours_until_due:
                    text_content += f"⏰ **Time Until Due:** {abs(hours_until_due):.1f} hours {'(OVERDUE)' if hours_until_due < 0 else ''}\n"
                
                if sla_breach_likelihood > 0 and is_active:
                    risk_emoji = "🚨" if sla_risk_level == 'critical' else "⚠️" if sla_risk_level == 'high' else "⚡"
                    text_content += f"{risk_emoji} **SLA Breach Risk:** {sla_breach_likelihood}% ({sla_risk_level.upper()})\n"
                    if sla_risk_factors:
                        text_content += f"  Risk Factors: {', '.join(sla_risk_factors[:3])}\n"
            
            text_content += f"""
## 👥 Assignment
- **Requester:** {requester_name}
  - Email: {requester_email}
"""
            
            if requester_stats.get('total_count', 0) > 0:
                text_content += f"  - **Requester History:** {requester_stats['open_count']} open / {requester_stats['total_count']} total tickets\n"
                if requester_stats.get('open_count', 0) > 3:
                    text_content += f"  - ⚠️ **Repeat Requester:** Multiple open tickets\n"
            
            text_content += f"""- **Technician:** {technician_name}
  - Email: {technician_email}
- **Group:** {group}
- **Site:** {site}
"""

            if technician_name == 'Unassigned' and assignment_suggestion:
                text_content += f"\n💡 **Assignment Recommendation:** {assignment_suggestion}\n"

            text_content += f"""
## 📁 Classification
- **Category:** {category}
- **Subcategory:** {subcategory}
- **Keywords:** {', '.join(keywords) if keywords else 'None detected'}
"""

            text_content += f"""
## 📅 Timeline
- **Created:** {created_time_display}
- **Due By:** {due_by_time_display}
"""

            if resolved_time_display != 'Not resolved':
                text_content += f"- **Resolved:** {resolved_time_display}\n"
            
            if resolution_time_days:
                text_content += f"- **Resolution Time:** {resolution_time_days:.1f} days\n"
            
            if first_response_time_hours:
                text_content += f"- **First Response:** {first_response_time_hours:.1f} hours\n"
            
            if days_since_last_activity:
                text_content += f"- **Last Activity:** {days_since_last_activity:.1f} days ago\n"

            text_content += f"""
- **Overdue:** {'⚠️ YES' if is_overdue else 'No'}
- **Escalated:** {'🔥 YES' if is_escalated else 'No'}

## 📝 Description
{clean_description}
"""

            text_content += "\n## 🤖 AI-Powered Insights & Recommendations (v2.5 RESILIENT)\n\n"
            
            if is_active:
                text_content += f"### 📊 Predictive Analytics\n"
                text_content += f"- **Predicted Resolution Time:** {predicted_resolution_days} days\n"
                text_content += f"- **Similar Tickets in Category:** {similar_ticket_count}\n"
                text_content += f"- **Complexity Assessment:** {structured_data['complexity_level'].upper()} complexity\n"
                
                if sla_breach_likelihood > 0:
                    text_content += f"- **SLA Breach Likelihood:** {sla_breach_likelihood}% ({sla_risk_level.upper()} risk)\n"
            
            if conv_intelligence['total_exchanges'] > 0:
                text_content += f"\n### 💬 Conversation Intelligence\n"
                text_content += f"- **Exchanges:** {conv_intelligence['total_exchanges']} (Customer: {conv_intelligence['customer_messages']}, Tech: {conv_intelligence['technician_messages']})\n"
                text_content += f"- **Pattern:** {conv_intelligence['response_pattern'].replace('-', ' ').title()}\n"
                text_content += f"- **Engagement Score:** {conv_intelligence['engagement_score']}/10\n"
                if conv_intelligence['avg_response_time_hours']:
                    text_content += f"- **Avg Response Time:** {conv_intelligence['avg_response_time_hours']:.1f} hours\n"
                if conv_intelligence['awaiting'] != 'unknown':
                    awaiting_emoji = "👤" if conv_intelligence['awaiting'] == 'customer' else "🔧"
                    text_content += f"- {awaiting_emoji} **Awaiting:** {conv_intelligence['awaiting'].title()} response\n"
                if conv_intelligence['unanswered_questions'] > 0:
                    text_content += f"- ❓ **Unanswered Questions:** {conv_intelligence['unanswered_questions']}\n"
            
            if sentiment != 'unknown':
                sentiment_emoji = "🚨" if sentiment == 'high_risk' else "⚠️" if sentiment == 'moderate_risk' else "😊" if sentiment == 'positive' else "😐"
                text_content += f"\n- **Customer Sentiment:** {sentiment_emoji} {sentiment.replace('_', ' ').title()} ({sentiment_score:.0f}% risk)\n"
            
            text_content += "\n"
            
            alerts = []
            
            if structured_data['needs_immediate_attention']:
                alerts.append("🔥 **CRITICAL:** This ticket requires IMMEDIATE attention (overdue + high priority)")
            
            if sla_breach_likelihood > 75 and is_active:
                alerts.append(f"🚨 **HIGH SLA BREACH RISK:** {sla_breach_likelihood}% likelihood - {', '.join(sla_risk_factors[:2])}")
            
            if is_active and conversation_count == 0 and age_days and age_days > 2:
                alerts.append("💬 **NO RESPONSES YET:** Ticket created {:.0f} days ago with no conversation - reach out to requester".format(age_days))
            
            if is_active and days_since_last_activity and days_since_last_activity > 7:
                alerts.append("⏰ **STALE TICKET:** No activity in {:.0f} days - needs follow-up".format(days_since_last_activity))
            
            if conv_intelligence['unanswered_questions'] > 0:
                alerts.append(f"❓ **UNANSWERED QUESTIONS:** {conv_intelligence['unanswered_questions']} questions need response")
            
            if conversation_count > 10:
                alerts.append("🔄 **HIGH CONVERSATION VOLUME:** Multiple exchanges detected - consider scheduling a call/meeting")
            
            if sentiment == 'high_risk':
                alerts.append("🚨 **SATISFACTION RISK:** Negative sentiment detected - prioritize customer communication")
            
            if complexity_score >= 7:
                alerts.append("🧩 **COMPLEX ISSUE:** High complexity score - may require senior technician or escalation")
            
            if technician_name == 'Unassigned':
                alerts.append("👤 **UNASSIGNED:** No technician assigned - review priority and assign appropriate team member")
            
            if requester_stats.get('open_count', 0) > 5:
                alerts.append(f"⚠️ **MULTIPLE OPEN ISSUES:** Requester has {requester_stats['open_count']} open tickets - may indicate systemic problem")
            
            if alerts:
                text_content += "### ⚠️ Action Required\n"
                for alert in alerts:
                    text_content += f"{alert}\n\n"
            
            if resolution_suggestions and is_active:
                text_content += "### 💡 Suggested Resolution Steps\n"
                for idx, suggestion in enumerate(resolution_suggestions, 1):
                    text_content += f"{idx}. {suggestion}\n"
                text_content += "\n"
            
            cat_stats = analytics_cache['category_stats'].get(category, {})
            if cat_stats.get('count', 0) > 5:
                text_content += f"### 📈 Category Insights\n"
                text_content += f"- **Total tickets in {category}:** {cat_stats['count']}\n"
                if cat_stats.get('avg_resolution_days'):
                    avg_res = statistics.mean(cat_stats['avg_resolution_days'])
                    text_content += f"- **Average resolution time:** {avg_res:.1f} days\n"
                if cat_stats.get('overdue_count'):
                    overdue_pct = (cat_stats['overdue_count'] / cat_stats['count']) * 100
                    text_content += f"- **Overdue rate:** {overdue_pct:.0f}%\n"
                text_content += "\n"
            
            if resolution_text:
                text_content += f"\n## ✅ Resolution\n{resolution_text}\n"
            
            text_content += conversations_text
            
            text_content += f"""

---
## 🔍 Quick Reference (v2.5 RESILIENT)
- Ticket: #{display_id}
- Current Status: {status} ({status_category})
- Active: {'Yes' if is_active else 'No'}
- Closed: {'Yes' if is_closed else 'No'}
- Open: {'Yes' if is_open else 'No'}
- Resolved: {'Yes' if is_resolved else 'No'}
- Priority: {priority}
- Complexity: {structured_data['complexity_level']} ({complexity_score}/10)
- Assigned To: {technician_name}
- Category: {category}
- Age: {age_days:.1f} days if age_days else 'N/A'
- Conversations: {conversation_count} ({conv_intelligence['response_pattern']})
- Awaiting: {conv_intelligence['awaiting'].title()}
- Keywords: {', '.join(keywords) if keywords else 'None'}
- Sentiment: {sentiment}
- Predicted Resolution: {predicted_resolution_days} days
- SLA Breach Risk: {sla_breach_likelihood}% ({sla_risk_level}) if is_active and sla_breach_likelihood > 0 else 'N/A'
- Requester History: {requester_stats['open_count']} open / {requester_stats['total_count']} total
"""
            
            timestamp = last_updated_time_ms if last_updated_time_ms else int(time.time() * 1000)
            
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

def upload_to_dust(document):
    """Upload single document to Dust with timeout"""
    try:
        with perf.time_operation('upload_to_dust'):
            if not document:
                return False
            
            headers = {
                'Authorization': f'Bearer {DUST_API_KEY}',
                'Content-Type': 'application/json'
            }
            url = f"{DUST_API_BASE}/{document['document_id']}"
            
            for attempt in range(MAX_RETRIES):
                try:
                    response = requests.post(url, headers=headers, json=document, timeout=20)
                    if response.status_code in [200, 201]:
                        return True
                    
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(2)
                        continue
                    return False
                except requests.exceptions.Timeout:
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(2)
                        continue
                    return False
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(2)
                        continue
                    return False
            
            return False
    except Exception as e:
        logger.error(f"  ⚠️  Upload error: {str(e)[:50]}")
        return False

def upload_batch_to_dust(documents, max_workers=MAX_PARALLEL_WORKERS):
    """Upload multiple documents in parallel with timeout"""
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
                        logger.error(f"  ⚠️  Upload failed: {str(e)[:50]}")
            except concurrent.futures.TimeoutError:
                logger.error("  ⏱️  Batch upload timed out")
        
        return success

def save_analytics_report():
    """Save analytics cache to JSON for pattern analysis"""
    
    try:
        report = {
            'generated_at': datetime.now().isoformat(),
            'category_statistics': dict(analytics_cache['category_stats']),
            'technician_statistics': dict(analytics_cache['technician_stats']),
            'requester_statistics': dict(analytics_cache['requester_stats']),
            'sla_breach_patterns': dict(analytics_cache['sla_breach_patterns']),
            'hourly_distribution': dict(analytics_cache['hourly_distribution']),
            'daily_distribution': dict(analytics_cache['daily_distribution']),
            'top_issue_keywords': dict(analytics_cache['issue_keywords'].most_common(20))
        }
        
        filename = f"analytics_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        logger.info(f"\n📊 Analytics report saved: {filename}")
        
    except Exception as e:
        logger.error(f"Could not save analytics report: {e}")

def print_analytics_summary():
    """Print analytics summary to console"""
    
    logger.info("\n" + "="*60)
    logger.info("📊 ANALYTICS SUMMARY (v2.5 RESILIENT)")
    logger.info("="*60)
    
    logger.info("\n🏆 Top Categories:")
    top_cats = sorted(analytics_cache['category_stats'].items(), 
                     key=lambda x: x[1]['count'], reverse=True)[:5]
    for cat, stats in top_cats:
        logger.info(f"  {cat}: {stats['count']} tickets")
        if stats['avg_resolution_days']:
            avg = statistics.mean(stats['avg_resolution_days'])
            logger.info(f"    Avg resolution: {avg:.1f} days")
    
    logger.info("\n👥 Technician Workload:")
    for tech, stats in sorted(analytics_cache['technician_stats'].items(), 
                             key=lambda x: x[1]['active'], reverse=True)[:5]:
        logger.info(f"  {tech}: {stats['active']} active / {stats['total']} total")
        if stats['avg_response_hours']:
            avg = statistics.mean(stats['avg_response_hours'])
            logger.info(f"    Avg response: {avg:.1f} hours")
    
    logger.info("\n👤 Top Requesters:")
    top_req = sorted(analytics_cache['requester_stats'].items(),
                    key=lambda x: x[1]['total_count'], reverse=True)[:5]
    for req, stats in top_req:
        logger.info(f"  {req}: {stats['open_count']} open / {stats['total_count']} total")
    
    logger.info("\n⏰ Peak Hours:")
    top_hours = sorted(analytics_cache['hourly_distribution'].items(), 
                      key=lambda x: x[1], reverse=True)[:5]
    for hour, count in top_hours:
        logger.info(f"  {hour:02d}:00 - {count} tickets")
    
    logger.info("\n🔍 Top Issue Types:")
    for keyword, count in analytics_cache['issue_keywords'].most_common(5):
        logger.info(f"  {keyword}: {count} tickets")
    
    logger.info("="*60 + "\n")        
def main():
    logger.info("=" * 60)
    logger.info(f"🚀 SERVICEDESK INTELLIGENCE SUITE v2.5 - RESILIENT")
    logger.info(f"Starting at {datetime.now()}")
    logger.info("=" * 60)
    logger.info("✨ v2.5 RESILIENCE FEATURES:")
    logger.info("   • Individual ticket timeouts (skip stuck tickets)")
    logger.info("   • Resume capability with checkpointing")
    logger.info("   • Better error isolation per ticket")
    logger.info("   • Per-ticket progress logging")
    logger.info("   • Graceful degradation on failures")
    logger.info("   • Smaller batches for better isolation (25 tickets)")
    logger.info("")
    
    try:
        config = validate_environment()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return
    
    with perf.time_operation('total_execution'):
        # Check for existing checkpoint
        processed_ids, success_count, error_count = checkpoint_mgr.load()
        
        tickets = fetch_all_tickets()
        
        # Filter out already processed tickets
        if processed_ids:
            original_count = len(tickets)
            tickets = [t for t in tickets if t.get('id') not in processed_ids]
            logger.info(f"ℹ️  Resuming: {len(processed_ids)} already processed, {len(tickets)} remaining")
        
        logger.info(f"\nTotal tickets to process: {len(tickets)}\n")
        
        if not tickets:
            logger.info("✅ No tickets to process - system is up to date!")
            current_time_ms = int(datetime.now().timestamp() * 1000)
            with open('last_sync_time.txt', 'w') as f:
                f.write(str(current_time_ms))
            logger.info(f"\n📅 Timestamp updated: {current_time_ms}")
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
        
        # Process in smaller batches
        batch_size = BATCH_SIZE
        document_batch = []
        
        for batch_start in range(0, len(tickets), batch_size):
            batch_end = min(batch_start + batch_size, len(tickets))
            batch = tickets[batch_start:batch_end]
            
            logger.info(f"\n[Batch {batch_start//batch_size + 1}] Processing tickets {batch_start+1}-{batch_end}...")
            
            # Process each ticket with individual timeout
            for idx, ticket in enumerate(batch, start=batch_start+1):
                ticket_id = ticket.get('id')
                
                if ticket_id in processed_ids:
                    continue
                
                logger.info(f"  [{idx}/{len(tickets)}] Processing ticket {ticket_id}...")
                
                try:
                    details = fetch_ticket_details(ticket_id, access_token)
                    
                    if not details:
                        logger.error(f"  ⚠️  Failed to fetch details for {ticket_id} - skipping")
                        error_count += 1
                        processed_ids.add(ticket_id)
                        continue
                    
                    conversations = fetch_ticket_conversations_single(ticket_id, access_token)
                    
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
                        
                        document_batch.append(document)
                        processed_ids.add(ticket_id)
                        
                        # Upload batch when it reaches size limit
                        if len(document_batch) >= DUST_UPLOAD_BATCH_SIZE:
                            batch_success = upload_batch_to_dust(document_batch)
                            success_count += batch_success
                            document_batch = []
                            
                            logger.info(f"  ✓ Uploaded batch | Total: {success_count}/{idx} | Active: {active_count} | Errors: {error_count}")
                    else:
                        error_count += 1
                        processed_ids.add(ticket_id)
                
                except requests.exceptions.Timeout:
                    logger.error(f"  ⏱️  TIMEOUT: {ticket_id} - skipping")
                    error_count += 1
                    processed_ids.add(ticket_id)
                    continue
                
                except Exception as e:
                    logger.error(f"  ⚠️  ERROR: {ticket_id} - {str(e)[:100]}")
                    error_count += 1
                    processed_ids.add(ticket_id)
                    continue
                
                # Save checkpoint every 50 tickets
                if idx % 50 == 0:
                    checkpoint_mgr.save(processed_ids, success_count, error_count)
                    
                    # Refresh token periodically
                    if idx % 250 == 0:
                        access_token = get_access_token(force_refresh=True)
                        logger.info(f"  ℹ️  Token refreshed at checkpoint: {idx}")
        
        # Upload remaining documents
        if document_batch:
            batch_success = upload_batch_to_dust(document_batch)
            success_count += batch_success
            logger.info(f"  ✓ Final batch uploaded: {batch_success} documents")
        
        logger.info(f"\n{'='*60}")
        logger.info(f"✅ INTELLIGENCE SYNC COMPLETED (v2.5 RESILIENT)")
        logger.info(f"  Total Processed: {success_count}/{len(tickets)}")
        logger.info(f"  Active Tickets: {active_count}")
        logger.info(f"  Closed Tickets: {closed_count}")
        logger.info(f"  With Conversations: {conv_count}")
        logger.info(f"  High Complexity: {high_complexity_count}")
        logger.info(f"  Satisfaction At-Risk: {at_risk_count}")
        logger.info(f"  High SLA Breach Risk: {high_breach_risk_count}")
        logger.info(f"  Awaiting Customer: {awaiting_customer_count}")
        logger.info(f"  Awaiting Technician: {awaiting_tech_count}")
        logger.info(f"  Errors/Skipped: {error_count}")
        logger.info(f"  Finished: {datetime.now()}")
        logger.info(f"{'='*60}")
        
        print_analytics_summary()
        save_analytics_report()
        
        current_time_ms = int(datetime.now().timestamp() * 1000)
        logger.info(f"\n📅 Current sync timestamp: {current_time_ms}")
        logger.info(f"   (Will be used for next incremental sync)")
        
        with open('last_sync_time.txt', 'w') as f:
            f.write(str(current_time_ms))
        
        # Clear checkpoint on successful completion
        checkpoint_mgr.clear()
        
        logger.info("\n🎯 v2.5 Resilience Features Active:")
        logger.info("  ✓ Individual ticket timeouts (20s per API call)")
        logger.info("  ✓ Conversation fetch timeouts (15s)")
        logger.info("  ✓ Resume capability with checkpointing")
        logger.info("  ✓ Per-ticket error isolation")
        logger.info("  ✓ Graceful degradation on failures")
        logger.info("  ✓ Smaller batches (25 tickets for better isolation)")
        logger.info("  ✓ Thread-safe timeout handling")
        logger.info("")
        
        # Print performance report
        perf.print_report()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n⚠️  Sync interrupted by user - checkpoint saved")
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
