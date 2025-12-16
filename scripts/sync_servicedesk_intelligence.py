#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.6 - PRODUCTION READY
Fixes checkpoint race condition using sync start time

Changes in v3.1.6:
- FIXED: Checkpoint uses sync START time to prevent race condition gaps
- Prevents missing updates that occur during sync processing
- Accepts minor duplicate processing (idempotent uploads)

Changes in v3.1.5:
- FIXED: Incremental syncs check BOTH created_time AND last_updated_time
- Uses OR condition to catch new tickets AND updates to existing tickets
- Proper checkpoint saving with latest updated time
- Date range logging shows both created and updated times

Changes in v3.1.4:
- FIXED: Sort by created_time instead of last_updated_time
- FIXED: Use display_id field from API for correct ticket numbers
- Date range logging for verification
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

VERSION = "3.1.6"
BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY = 5

# Checkpoint files
SCRIPT_DIR = Path(__file__).resolve().parent.parent
TIMESTAMP_FILE = str(SCRIPT_DIR / 'last_sync_time.txt')
CHECKPOINT_FILE = str(SCRIPT_DIR / 'checkpoint.json')
SKIPPED_TICKETS_FILE = str(SCRIPT_DIR / 'skipped_tickets.json')

REQUEST_TIMEOUT = 30
MAX_PAGES = None

# Parallel upload configuration
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

# Skip tracking
SKIPPED_TICKETS = []
SKIP_LOCK = Lock()

# Cache
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
            'resolution_categories': defaultdict(int),
            'module_distribution': defaultdict(int),
            'auto_categorization': {'assigned': 0, 'high_confidence': 0, 'low_confidence': 0}
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
    
    def update_module_distribution(self, module_type):
        self.raw_data['module_distribution'][module_type] += 1
    
    def update_auto_categorization(self, was_auto: bool, confidence: float):
        if was_auto:
            self.raw_data['auto_categorization']['assigned'] += 1
            if confidence >= 80:
                self.raw_data['auto_categorization']['high_confidence'] += 1
            elif confidence < 50:
                self.raw_data['auto_categorization']['low_confidence'] += 1

analytics_cache = AnalyticsCache()

# Metrics counters
METRICS = {
    'total_processed': 0,
    'requests': 0,
    'service_requests': 0,
    'incidents': 0,
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
    'linked_tickets': 0,
    'auto_categorized': 0,
    'skipped': 0,
    'retried': 0,
    'failed_after_retry': 0
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
# SKIP TRACKING
# ============================================================

def track_skipped_ticket(ticket_id: str, ticket_subject: str, reason: str, stage: str):
    """Track a skipped ticket with details"""
    global SKIPPED_TICKETS
    
    skip_info = {
        'ticket_id': ticket_id,
        'subject': ticket_subject,
        'reason': reason,
        'stage': stage,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    
    with SKIP_LOCK:
        SKIPPED_TICKETS.append(skip_info)
        METRICS['skipped'] += 1
    
    logger.warning(f"⚠️  SKIPPED Ticket {ticket_id} ({stage}): {reason}")

def save_skipped_tickets_report():
    """Save skipped tickets to JSON file"""
    if not SKIPPED_TICKETS:
        return
    
    try:
        report = {
            'sync_time': datetime.now(timezone.utc).isoformat(),
            'version': VERSION,
            'total_skipped': len(SKIPPED_TICKETS),
            'skipped_tickets': SKIPPED_TICKETS
        }
        
        with open(SKIPPED_TICKETS_FILE, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"✓ Saved skipped tickets report: {SKIPPED_TICKETS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save skipped tickets report: {e}")

def print_skip_summary():
    """Print summary of skipped tickets"""
    if not SKIPPED_TICKETS:
        logger.info("✓ No tickets were skipped!")
        return
    
    logger.warning("")
    logger.warning("=" * 60)
    logger.warning(f"⚠️  SKIPPED TICKETS SUMMARY")
    logger.warning("=" * 60)
    logger.warning(f"Total skipped: {len(SKIPPED_TICKETS)}")
    logger.warning("")
    
    reasons = defaultdict(list)
    for skip in SKIPPED_TICKETS:
        reasons[skip['reason']].append(skip['ticket_id'])
    
    logger.warning("By Reason:")
    for reason, ticket_ids in sorted(reasons.items(), key=lambda x: len(x[1]), reverse=True):
        logger.warning(f"  • {reason}: {len(ticket_ids)} tickets")
    
    logger.warning("")
    logger.warning("First 10 skipped tickets:")
    for skip in SKIPPED_TICKETS[:10]:
        logger.warning(f"  • #{skip['ticket_id']}: {skip['subject'][:50]}")
        logger.warning(f"    Reason: {skip['reason']} (Stage: {skip['stage']})")
    
    if len(SKIPPED_TICKETS) > 10:
        logger.warning(f"  ... and {len(SKIPPED_TICKETS) - 10} more")
    
    logger.warning("")
    logger.warning(f"Full report saved to: {SKIPPED_TICKETS_FILE}")
    logger.warning("=" * 60)

# ============================================================
# CREDENTIAL MANAGEMENT
# ============================================================

def clean_credential(value: str) -> str:
    """Remove quotes and escape characters"""
    if not value:
        return value
    value = value.strip().strip('"').strip("'")
    value = value.replace('\\', '')
    return value.strip()

SDP_DOMAIN = clean_credential(os.environ.get('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_credential(os.environ.get('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_credential(os.environ.get('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_credential(os.environ.get('SDP_REFRESH_TOKEN', ''))
DUST_API_KEY = clean_credential(os.environ.get('DUST_API_KEY', ''))
DUST_WORKSPACE_ID = clean_credential(os.environ.get('DUST_WORKSPACE_ID', ''))

SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def extract_short_ticket_number(ticket: Dict) -> str:
    """Extract short ticket number using display_id field"""
    try:
        display_id = ticket.get('display_id')
        if display_id:
            short_id = str(display_id).lstrip('#').strip()
            if short_id:
                return short_id
        
        ticket_id = ticket.get('id', '')
        ticket_id_str = str(ticket_id)
        
        if len(ticket_id_str) > 10:
            for length in [5, 6, 7, 8]:
                short_num = ticket_id_str[-length:].lstrip('0')
                if short_num and len(short_num) >= 4:
                    return short_num
            short_num = ticket_id_str[-8:].lstrip('0')
            return short_num if short_num else ticket_id_str[-5:]
        
        return ticket_id_str
    except Exception as e:
        logger.warning(f"Error extracting ticket number: {e}")
        return str(ticket.get('id', 'unknown'))

# ============================================================
# CHECKPOINT MANAGEMENT
# ============================================================

def load_checkpoint() -> Optional[datetime]:
    """Load last sync timestamp"""
    if os.path.exists(TIMESTAMP_FILE):
        try:
            with open(TIMESTAMP_FILE, 'r') as f:
                timestamp_str = f.read().strip()
                if timestamp_str:
                    try:
                        last_sync = datetime.fromisoformat(timestamp_str)
                        logger.info(f"✓ Loaded timestamp from: {TIMESTAMP_FILE}")
                        logger.info(f"  Last sync: {last_sync}")
                        return last_sync
                    except ValueError:
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
    """Save checkpoint"""
    try:
        with open(TIMESTAMP_FILE, 'w') as f:
            if last_sync_time:
                f.write(last_sync_time.isoformat())
            else:
                f.write('')
        
        if total_processed % 200 == 0 or total_processed < 100:  # Log less frequently
            logger.info(f"✓ SAVED Timestamp to: {TIMESTAMP_FILE}")
            if last_sync_time:
                logger.info(f"        {last_sync_time.isoformat()}")
    except Exception as e:
        logger.error(f"✗ Failed to save timestamp file: {e}")
    
    checkpoint = {
        'last_sync': last_sync_time.isoformat() if last_sync_time else None,
        'last_run': datetime.now(timezone.utc).isoformat(),
        'processed': total_processed,
        'errors': total_errors,
        'skipped': METRICS['skipped'],
        'version': VERSION
    }
    
    try:
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoint, f, indent=2)
        
        if total_processed % 200 == 0 or total_processed < 100:  # Log less frequently
            logger.info(f"✓ SAVED Stats to: {CHECKPOINT_FILE}")
            logger.info(f"        Processed: {total_processed}, Errors: {total_errors}, Skipped: {METRICS['skipped']}")
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
# TICKET TYPE CLASSIFICATION
# ============================================================

def classify_ticket_type(ticket: Dict) -> str:
    """Classify ticket type"""
    template = ticket.get('template', {})
    template_name = template.get('name', '').lower() if template else ''
    
    request_type = ticket.get('request_type', {})
    req_type_name = request_type.get('name', '').lower() if request_type else ''
    
    category = ticket.get('category', {})
    category_name = category.get('name', '').lower() if category else ''
    
    service_keywords = ['service', 'catalog', 'service request', 'service catalog']
    if any(keyword in template_name for keyword in service_keywords):
        return 'service_requests'
    if any(keyword in req_type_name for keyword in service_keywords):
        return 'service_requests'
    if ticket.get('is_service_request', False):
        return 'service_requests'
    
    incident_keywords = ['incident', 'major incident', 'critical incident']
    if any(keyword in template_name for keyword in incident_keywords):
        return 'incidents'
    if any(keyword in req_type_name for keyword in incident_keywords):
        return 'incidents'
    if any(keyword in category_name for keyword in incident_keywords):
        return 'incidents'
    if ticket.get('is_incident', False):
        return 'incidents'
    
    return 'requests'

# ============================================================
# TICKET FETCHING - v3.1.5+ (CORRECT INCREMENTAL)
# ============================================================

def fetch_tickets_from_api(access_token: str, last_sync_time: Optional[datetime], 
                           page_limit: Optional[int] = None) -> List[Dict]:
    """Fetch all tickets with OR condition for incremental sync"""
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    search_criteria = []
    
    if last_sync_time:
        last_sync_ms = int(last_sync_time.timestamp() * 1000)
        
        search_criteria = [
            {
                "field": "created_time",
                "condition": "greater than",
                "value": str(last_sync_ms),
                "logical_operator": "OR"
            },
            {
                "field": "last_updated_time",
                "condition": "greater than",
                "value": str(last_sync_ms)
            }
        ]
        logger.info(f"  Filtering: Tickets created OR updated after {last_sync_time}")
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "get_total_count": True,
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    if search_criteria:
        list_info["list_info"]["search_criteria"] = search_criteria
    
    url = f"{SDP_API_BASE}/requests"
    all_tickets = []
    page = 1
    
    while True:
        if page_limit and page > page_limit:
            logger.warning(f"  Reached page limit of {page_limit}")
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
                
                if page <= 3 and tickets:
                    created_dates = []
                    updated_dates = []
                    for ticket in tickets:
                        created = ticket.get('created_time', {})
                        if created:
                            created_display = created.get('display_value', '')
                            if created_display:
                                created_dates.append(created_display)
                        
                        updated = ticket.get('last_updated_time', {})
                        if updated:
                            updated_display = updated.get('display_value', '')
                            if updated_display:
                                updated_dates.append(updated_display)
                    
                    if created_dates and updated_dates:
                        newest_created = max(created_dates) if created_dates else 'Unknown'
                        oldest_created = min(created_dates) if created_dates else 'Unknown'
                        newest_updated = max(updated_dates) if updated_dates else 'Unknown'
                        oldest_updated = min(updated_dates) if updated_dates else 'Unknown'
                        logger.info(f"  Page {page}: {len(tickets)} tickets")
                        logger.info(f"    Created: {newest_created} to {oldest_created}")
                        logger.info(f"    Updated: {newest_updated} to {oldest_updated}")
                else:
                    logger.info(f"  Page {page}: Found {len(tickets)} tickets (Total: {len(all_tickets) + len(tickets)})")
                
                all_tickets.extend(tickets)
                
                list_info_response = data.get('list_info', {})
                has_more = list_info_response.get('has_more_rows', False)
                
                if not has_more:
                    logger.info(f"  No more pages. Total fetched: {len(all_tickets)}")
                    break
                
                page += 1
                time.sleep(0.1)
                
            elif response.status_code == 429:
                logger.warning(f"  Rate limited on page {page}. Waiting 30s...")
                time.sleep(30)
                continue
            else:
                logger.error(f"  API error: {response.status_code}")
                break
                
        except Exception as e:
            logger.error(f"  Error fetching page {page}: {e}")
            break
    
    return all_tickets

def fetch_all_tickets(access_token: str, last_sync_time: Optional[datetime], 
                     page_limit: Optional[int] = None) -> List[Dict]:
    """Fetch all tickets and classify them"""
    
    if last_sync_time:
        logger.info(f"📊 INCREMENTAL SYNC: Fetching tickets created OR updated since {last_sync_time}")
    else:
        logger.info("📊 FULL SYNC: Fetching ALL tickets (no checkpoint found)")
    
    if page_limit:
        logger.info(f"  Page limit: {page_limit}")
    else:
        logger.info("  No page limit - fetching ALL tickets")
    
    all_tickets = fetch_tickets_from_api(access_token, last_sync_time, page_limit)
    
    if not all_tickets:
        logger.info("✓ Fetched 0 tickets")
        return []
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets")
    logger.info("")
    logger.info("📋 Classifying tickets by type...")
    
    module_counts = {'requests': 0, 'service_requests': 0, 'incidents': 0}
    for ticket in all_tickets:
        ticket_type = classify_ticket_type(ticket)
        ticket['_module_type'] = ticket_type
        module_counts[ticket_type] += 1
    
    logger.info("✓ Classification complete:")
    for module_type in ['requests', 'service_requests', 'incidents']:
        count = module_counts[module_type]
        if count > 0:
            module_name = module_type.replace('_', ' ').title()
            logger.info(f"  • {module_name}: {count}")
    
    logger.info(f"✓ Total tickets fetched: {len(all_tickets)}")
    
    return all_tickets

# ============================================================
# CONVERSATION FETCHING WITH RETRY
# ============================================================

def fetch_conversation_single_with_retry(ticket_id: str, ticket_subject: str, access_token: str, max_attempts: int = 3) -> Tuple[List[Dict], Optional[str]]:
    """Fetch conversations with retry"""
    url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    for attempt in range(max_attempts):
        try:
            response = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                conversations = response.json().get('conversations', [])
                if attempt > 0:
                    METRICS['retried'] += 1
                return conversations, None
            
            elif response.status_code == 404:
                return [], None
            
            elif response.status_code == 429:
                wait_time = (attempt + 1) * 5
                logger.warning(f"  Rate limited for ticket {ticket_id}, waiting {wait_time}s")
                time.sleep(wait_time)
                continue
            
            else:
                error_msg = f"HTTP {response.status_code}"
                if attempt < max_attempts - 1:
                    time.sleep(2)
                    continue
                else:
                    return [], error_msg
                    
        except requests.exceptions.Timeout:
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            else:
                return [], "Request timeout"
        
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            else:
                return [], f"Exception: {str(e)}"
    
    METRICS['failed_after_retry'] += 1
    return [], "All retry attempts failed"

def fetch_conversations_parallel(tickets: List[Dict], access_token: str, max_workers: int = 10) -> Dict[str, Tuple[List[Dict], Optional[str]]]:
    """Fetch conversations in parallel with retry"""
    conversations_map = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticket = {
            executor.submit(
                fetch_conversation_single_with_retry, 
                ticket['id'],
                ticket.get('subject', 'No subject'),
                access_token
            ): ticket['id']
            for ticket in tickets
        }
        
        for future in as_completed(future_to_ticket):
            ticket_id = future_to_ticket[future]
            try:
                conversations, error = future.result()
                conversations_map[ticket_id] = (conversations, error)
            except Exception as e:
                conversations_map[ticket_id] = ([], f"Future exception: {str(e)}")
    
    return conversations_map

# ============================================================
# AUTO-CATEGORIZATION
# ============================================================

def auto_categorize_ticket(ticket: Dict, existing_category: str = None) -> Dict[str, Any]:
    """Auto-categorize ticket based on content"""
    
    if existing_category and existing_category.lower() not in ['uncategorized', 'general', 'other', '', 'none']:
        return {
            'suggested_category': existing_category,
            'confidence': 100,
            'keywords_matched': [],
            'use_suggested': False,
            'original_category': existing_category
        }
    
    subject = ticket.get('subject', '').lower()
    description = ticket.get('description', '').lower()
    combined_text = f"{subject} {description}"
    
    category_patterns = {
        'Hardware Issues': {
            'primary': ['laptop', 'desktop', 'computer', 'monitor', 'keyboard', 'mouse', 'hardware', 
                       'screen', 'display', 'device', 'pc', 'workstation', 'dock', 'docking'],
            'secondary': ['broken', 'damaged', 'not working', 'replacement', 'upgrade', 'new computer'],
            'weight': 1.0
        },
        'Network & Connectivity': {
            'primary': ['network', 'internet', 'wifi', 'wireless', 'connection', 'connectivity', 
                       'lan', 'ethernet', 'dns', 'ip address', 'router', 'switch'],
            'secondary': ['cannot connect', 'no internet', 'slow connection', 'disconnect'],
            'weight': 1.0
        },
        'VPN & Remote Access': {
            'primary': ['vpn', 'remote', 'forticlient', 'fortitoken', 'remote access', 'work from home',
                       'remote desktop', 'rdp', 'citrix', 'virtual desktop'],
            'secondary': ['cannot connect remotely', 'vpn error', 'token', 'two factor'],
            'weight': 1.2
        },
        'Email & Communication': {
            'primary': ['email', 'outlook', 'mail', 'mailbox', 'exchange', 'inbox', 'calendar',
                       'teams', 'skype', 'zoom', 'webex', 'meeting'],
            'secondary': ['cannot send', 'cannot receive', 'email not working', 'calendar invite'],
            'weight': 1.0
        },
        'Software & Applications': {
            'primary': ['software', 'application', 'program', 'app', 'install', 'installation',
                       'update', 'upgrade', 'license', 'adobe', 'office', 'excel', 'word'],
            'secondary': ['cannot open', 'not working', 'crash', 'error message', 'uninstall'],
            'weight': 0.9
        },
        'Access & Permissions': {
            'primary': ['access', 'permission', 'rights', 'share', 'folder', 'file access',
                       'denied', 'restricted', 'cannot access', 'locked out'],
            'secondary': ['need access', 'request access', 'remove access', 'grant access'],
            'weight': 1.0
        },
        'Password & Authentication': {
            'primary': ['password', 'login', 'credential', 'authentication', 'forgot password',
                       'reset password', 'cannot login', 'locked account', 'mfa', '2fa'],
            'secondary': ['username', 'sign in', 'log in', 'account locked'],
            'weight': 1.1
        },
        'Printer & Peripherals': {
            'primary': ['printer', 'print', 'printing', 'scanner', 'scan', 'copier', 'fax',
                       'paper jam', 'ink', 'toner'],
            'secondary': ['cannot print', 'print queue', 'printer offline', 'print error'],
            'weight': 0.9
        },
        'Performance & Speed': {
            'primary': ['slow', 'performance', 'freeze', 'frozen', 'hang', 'lag', 'sluggish',
                       'running slow', 'takes forever', 'not responding'],
            'secondary': ['speed', 'fast', 'optimize', 'cleanup'],
            'weight': 0.8
        },
        'Security & Antivirus': {
            'primary': ['security', 'antivirus', 'malware', 'virus', 'threat', 'suspicious',
                       'phishing', 'spam', 'quarantine', 'blocked'],
            'secondary': ['security alert', 'suspicious email', 'cannot download'],
            'weight': 1.1
        },
        'Account Management': {
            'primary': ['new user', 'new employee', 'onboarding', 'offboarding', 'termination',
                       'create account', 'disable account', 'employee setup'],
            'secondary': ['new hire', 'user account', 'account creation'],
            'weight': 0.9
        },
        'Mobile & Devices': {
            'primary': ['mobile', 'phone', 'iphone', 'android', 'tablet', 'ipad', 'cell phone',
                       'smartphone', 'mobile device'],
            'secondary': ['cell', 'bring', 'sync', 'mobile app'],
            'weight': 0.8
        },
        'Database & Systems': {
            'primary': ['database', 'sql', 'server', 'system', 'erp', 'crm', 'backup',
                       'data', 'oracle', 'mysql'],
            'secondary': ['query', 'report', 'data access', 'system error'],
            'weight': 0.9
        }
    }
    
    category_scores = {}
    matched_keywords = {}
    
    for category, patterns in category_patterns.items():
        score = 0
        keywords = []
        
        for keyword in patterns['primary']:
            if keyword in combined_text:
                score += 10 * patterns['weight']
                keywords.append(keyword)
        
        for keyword in patterns['secondary']:
            if keyword in combined_text:
                score += 5 * patterns['weight']
                keywords.append(keyword)
        
        if score > 0:
            category_scores[category] = score
            matched_keywords[category] = keywords
    
    if not category_scores:
        return {
            'suggested_category': 'General Support',
            'confidence': 0,
            'keywords_matched': [],
            'use_suggested': True,
            'original_category': existing_category or 'Uncategorized'
        }
    
    best_category = max(category_scores, key=category_scores.get)
    best_score = category_scores[best_category]
    
    confidence = min(best_score * 2, 100)
    
    sorted_scores = sorted(category_scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[0] - sorted_scores[1] < 5:
        confidence *= 0.7
    
    return {
        'suggested_category': best_category,
        'confidence': round(confidence, 1),
        'keywords_matched': matched_keywords.get(best_category, []),
        'use_suggested': True,
        'original_category': existing_category or 'Uncategorized'
    }

# ============================================================
# AI ANALYSIS FUNCTIONS
# ============================================================

def calculate_complexity_score(ticket: Dict, conversations: List[Dict]) -> int:
    """Calculate complexity score 0-10"""
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
    """Analyze customer sentiment from conversations"""
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
    """Predict SLA breach risk"""
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
    """Identify issue types from description"""
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
    """Extract all comprehensive fields from ticket"""
    fields = {}
    
    def safe_get(obj, key, default=''):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default
    
    long_ticket_id = ticket.get('id', 'unknown')
    short_ticket_number = extract_short_ticket_number(ticket)
    module_type = ticket.get('_module_type', 'request')
    
    fields['ticket_id_long'] = long_ticket_id
    fields['ticket_id'] = short_ticket_number
    fields['module_type'] = module_type
    fields['display_id'] = f"#{short_ticket_number}"
    
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
    original_category = safe_get(category, 'name', 'Uncategorized')
    fields['category_original'] = original_category
    
    auto_cat = auto_categorize_ticket(ticket, original_category)
    fields['category'] = auto_cat['suggested_category']
    fields['category_confidence'] = auto_cat['confidence']
    fields['category_auto_assigned'] = auto_cat['use_suggested']
    fields['category_keywords'] = auto_cat['keywords_matched']
    
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
    
    fields['impact'] = safe_get(ticket.get('impact') or {}, 'name', '')
    fields['urgency'] = safe_get(ticket.get('urgency') or {}, 'name', '')
    fields['level'] = safe_get(ticket.get('level') or {}, 'name', '')
    fields['tier'] = fields['level']
    fields['service'] = safe_get(ticket.get('service') or {}, 'name', '')
    fields['time_spent'] = ticket.get('time_spent', 0) or 0
    fields['time_spent_hours'] = round(fields['time_spent'] / 3600000, 2) if fields['time_spent'] else 0
    fields['resolution_category'] = safe_get(ticket.get('resolution') or {}, 'name', '')
    fields['reopened_count'] = ticket.get('reopened_count', 0) or 0
    fields['has_been_reopened'] = fields['reopened_count'] > 0
    fields['is_first_call_resolution'] = ticket.get('is_fcr', False) or False
    
    return fields

# ============================================================
# ANALYTICS & METRICS TRACKING
# ============================================================

def track_analytics(ticket: Dict, conversations: List[Dict], complexity: int, comprehensive_fields: Dict):
    """Track analytics data"""
    module_type = comprehensive_fields.get('module_type', 'unknown')
    analytics_cache.update_module_distribution(module_type)
    
    was_auto = comprehensive_fields.get('category_auto_assigned', False)
    confidence = comprehensive_fields.get('category_confidence', 0)
    analytics_cache.update_auto_categorization(was_auto, confidence)
    
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

def track_metrics(comprehensive_fields: Dict, conversations: List[Dict], 
                 complexity: int, sentiment: str, sla_risk: str, response_status: Dict):
    """Track metrics"""
    METRICS['total_processed'] += 1
    
    module_type = comprehensive_fields.get('module_type', 'unknown')
    if module_type == 'requests':
        METRICS['requests'] += 1
    elif module_type == 'service_requests':
        METRICS['service_requests'] += 1
    elif module_type == 'incidents':
        METRICS['incidents'] += 1
    
    if comprehensive_fields.get('category_auto_assigned'):
        METRICS['auto_categorized'] += 1
    
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

# ============================================================
# TAG GENERATION
# ============================================================

def generate_comprehensive_tags(ticket: Dict, conversations: List[Dict], 
                                comprehensive_fields: Dict, complexity: int,
                                sentiment: str, sla_risk: str, response_status: Dict) -> List[str]:
    """Generate comprehensive tags for searchability"""
    tags = []
    
    short_id = comprehensive_fields.get('ticket_id')
    tags.extend([
        f"ticket:{short_id}",
        f"#{short_id}",
        comprehensive_fields.get('display_id', '')
    ])
    
    module_type = comprehensive_fields.get('module_type', 'unknown')
    tags.append(f"module:{module_type}")
    
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
    
    category = comprehensive_fields['category'].lower().replace(' ', '-').replace('&', 'and')
    tags.append(f"category:{category}")
    if comprehensive_fields.get('category_auto_assigned'):
        tags.append('auto-categorized')
        confidence = comprehensive_fields.get('category_confidence', 0)
        if confidence >= 80:
            tags.append('high-confidence-category')
        elif confidence < 50:
            tags.append('low-confidence-category')
    
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
    
    tags.append(f"awaiting:{response_status['awaiting_response']}")
    if response_status['awaiting_response'] == 'customer':
        tags.append('awaiting-customer-response')
    elif response_status['awaiting_response'] == 'technician':
        tags.extend(['awaiting-technician-response', 'actionable'])
    
    return list(set(tags))

# ============================================================
# DOCUMENT CREATION
# ============================================================

def create_dust_document(ticket: Dict, conversations: List[Dict], 
                        comprehensive_fields: Dict, complexity: int,
                        sentiment: str, sla_risk: str, sla_breach_likelihood: int,
                        estimated_resolution_hours: float, issue_types: List[str],
                        response_status: Dict) -> Dict:
    """Create Dust document from ticket data"""
    
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
    
    short_id = comprehensive_fields.get('display_id', f"#{comprehensive_fields.get('ticket_id')}")
    module_type = comprehensive_fields.get('module_type', 'request').replace('_', ' ').title()
    
    category_display = comprehensive_fields['category']
    if comprehensive_fields.get('category_auto_assigned'):
        confidence = comprehensive_fields.get('category_confidence', 0)
        category_display += f" (Auto-assigned: {confidence:.0f}% confidence)"
        if comprehensive_fields.get('category_keywords'):
            keywords = ', '.join(comprehensive_fields['category_keywords'][:5])
            category_display += f"\n  Keywords: {keywords}"
    
    document_text = f"""# Ticket {short_id}: {comprehensive_fields['subject']}

## Type & Overview
- **Type:** {module_type}
- **Status:** {comprehensive_fields['status']} ({comprehensive_fields['status_category']})
- **Priority:** {comprehensive_fields['priority']}
- **Category:** {category_display}

## People
- **Requester:** {comprehensive_fields['requester_name']}
- **Technician:** {comprehensive_fields['technician_name']}

## Timeline
- **Created:** {comprehensive_fields['created_time']}
- **Due By:** {comprehensive_fields['due_by_time'] or 'No due date'}
- **Last Updated:** {comprehensive_fields['last_updated_time']}

## Description
{comprehensive_fields['description']}

## AI Intelligence
- **Complexity:** {complexity}/10
- **Sentiment:** {sentiment}
- **SLA Risk:** {sla_risk} ({sla_breach_likelihood}%)
- **Estimated Resolution:** {estimated_resolution_hours} hours

## Conversations ({len(conversations)} total)
{conversation_text}

## Tags
{', '.join(tags[:50])}

---
*Intelligence Suite v{VERSION} - Sync Start Time Checkpoint*
*Module: {module_type} | Ticket ID: {short_id} | Category Confidence: {comprehensive_fields.get('category_confidence', 0):.0f}%*
"""
    
    short_ticket_num = comprehensive_fields.get('ticket_id')
    document = {
        "document_id": f"ticket-{short_ticket_num}",
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
    """Process a batch of tickets"""
    logger.info(f"\n[Batch {batch_num}] Processing tickets {(batch_num-1)*BATCH_SIZE + 1}-{min(batch_num*BATCH_SIZE, total_tickets)}...")
    
    documents = []
    errors = 0
    
    conversations_map = fetch_conversations_parallel(tickets, access_token)
    
    for i, ticket in enumerate(tickets, 1):
        ticket_id = ticket['id']
        ticket_subject = ticket.get('subject', 'No subject')
        short_id = extract_short_ticket_number(ticket)
        
        try:
            conversations, conv_error = conversations_map.get(ticket_id, ([], None))
            if conv_error:
                track_skipped_ticket(short_id, ticket_subject, f"Conversation fetch failed: {conv_error}", "conversation_fetch")
                continue
            
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
            track_skipped_ticket(short_id, ticket_subject, f"Processing error: {str(e)}", "ticket_processing")
            errors += 1
    
    uploaded, upload_errors = upload_to_dust_parallel(documents)
    errors += upload_errors
    
    return documents, errors

def print_analytics_summary():
    """Print analytics summary"""
    logger.info("=" * 60)
    logger.info(f"ANALYTICS SUMMARY (v{VERSION})")
    logger.info("=" * 60)
    logger.info("")
    
    logger.info("📦 Module Distribution:")
    module_dist = analytics_cache.raw_data['module_distribution']
    if module_dist:
        for module, count in sorted(module_dist.items()):
            module_name = module.replace('_', ' ').title()
            logger.info(f"  {module_name}: {count} tickets")
    
    logger.info("")
    
    logger.info("🏷️  Auto-Categorization:")
    auto_cat_stats = analytics_cache.raw_data['auto_categorization']
    logger.info(f"  Tickets auto-categorized: {auto_cat_stats['assigned']}")
    logger.info(f"  High confidence (80%+): {auto_cat_stats['high_confidence']}")
    logger.info(f"  Low confidence (<50%): {auto_cat_stats['low_confidence']}")
    
    logger.info("")
    
    logger.info("🏆 Top Categories:")
    category_stats = analytics_cache.raw_data['category_stats']
    if category_stats:
        sorted_categories = sorted(category_stats.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
        for cat, data in sorted_categories:
            logger.info(f"  {cat}: {data['count']} tickets")
    
    logger.info("")

# ============================================================
# MAIN FUNCTION - v3.1.6 FIXED CHECKPOINT
# ============================================================

def main():
    """Main execution function"""
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info(f"Started: {datetime.now()}")
    logger.info("=" * 60)
    logger.info("✨ v3.1.6 FEATURES:")
    logger.info("   • FIXED: Checkpoint uses sync START time (prevents race condition)")
    logger.info("   • No missed updates during sync processing")
    logger.info("   • Incremental syncs check BOTH created AND updated times")
    logger.info("   • Sort by created_time (catches recent tickets)")
    logger.info("   • Use display_id field for correct ticket numbers")
    logger.info("   • OR condition: Gets new tickets AND updates to existing tickets")
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
    
    # Load previous checkpoint
    last_sync_time = load_checkpoint()
    
    # CRITICAL: Record sync START time BEFORE fetching tickets
    sync_start_time = datetime.now(timezone.utc)
    logger.info(f"📍 Sync start time (will be checkpoint): {sync_start_time}")
    
    # Fetch tickets using PREVIOUS checkpoint as filter
    try:
        tickets = fetch_all_tickets(access_token, last_sync_time, MAX_PAGES)
    except Exception as e:
        logger.error(f"FATAL: Failed to fetch tickets: {e}")
        sys.exit(1)
    
    if not tickets:
        # No tickets but still update checkpoint to sync start time
        save_checkpoint(sync_start_time, 0, 0)
        logger.info("✓ No new tickets. Sync complete.")
        logger.info(f"  ✓ Checkpoint updated to: {sync_start_time}")
        return
    
    # Process tickets in batches
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
            
            # Save checkpoint with SYNC START TIME (not current time, not latest ticket)
            save_checkpoint(sync_start_time, total_processed, total_errors)
            
        except Exception as e:
            logger.error(f"FATAL: Batch {batch_num} error: {e}")
            total_errors += len(batch_tickets)
    
    # Final checkpoint
    logger.info(f"  ✓ Final checkpoint: {sync_start_time}")
    
    save_skipped_tickets_report()
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✓ SYNC COMPLETE (v{VERSION})")
    logger.info("=" * 60)
    logger.info(f"  Total Fetched: {len(tickets)}")
    logger.info(f"  Successfully Processed: {METRICS['total_processed']}")
    logger.info(f"  Skipped: {METRICS['skipped']}")
    logger.info(f"  Retried: {METRICS['retried']}")
    logger.info(f"  Failed After Retry: {METRICS['failed_after_retry']}")
    logger.info(f"  Requests: {METRICS['requests']}")
    logger.info(f"  Service Requests: {METRICS['service_requests']}")
    logger.info(f"  Incidents: {METRICS['incidents']}")
    logger.info(f"  Auto-categorized: {METRICS['auto_categorized']}")
    logger.info(f"  Active: {METRICS['active_tickets']}")
    logger.info(f"  Errors: {total_errors}")
    logger.info(f"  Finished: {datetime.now()}")
    logger.info("=" * 60)
    logger.info("")
    
    print_analytics_summary()
    print_skip_summary()

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        save_skipped_tickets_report()
        sys.exit(1)
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        save_skipped_tickets_report()
        sys.exit(1)
