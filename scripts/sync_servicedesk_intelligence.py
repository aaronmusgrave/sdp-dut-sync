#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.22-PRODUCTION
=================================================

MAJOR FEATURES:
- FIXED: Status detection logic corrected
- NEW: Incremental sync - only fetches new/updated tickets
- NEW: Checkpoint system tracks last sync time
- NEW: Force full sync option via environment variable
- Automatic cleanup of orphaned tickets
- Rate-limited uploads for 99%+ success
"""

import os
import sys
import requests
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from html import unescape

# ============================================================
# VERSION & CONFIGURATION
# ============================================================

VERSION = "3.1.22-PRODUCTION"

# Validate environment variables first
def validate_required_env_vars():
    """Validate all required environment variables are set"""
    required = {
        'DUST_API_KEY': 'Dust API key',
        'DUST_WORKSPACE_ID': 'Dust workspace ID',
        'SDP_DOMAIN': 'ServiceDesk Plus domain',
        'SDP_CLIENT_ID': 'ServiceDesk Plus OAuth client ID',
        'SDP_CLIENT_SECRET': 'ServiceDesk Plus OAuth client secret',
        'SDP_REFRESH_TOKEN': 'ServiceDesk Plus OAuth refresh token'
    }
    
    missing = []
    for var, description in required.items():
        value = os.environ.get(var)
        if not value or not value.strip():
            missing.append(f"{var} ({description})")
    
    if missing:
        print("=" * 60)
        print("❌ MISSING REQUIRED ENVIRONMENT VARIABLES")
        print("=" * 60)
        for var in missing:
            print(f"  • {var}")
        print("=" * 60)
        sys.exit(1)
    
    sdp_domain = os.environ.get('SDP_DOMAIN', '').strip()
    if sdp_domain:
        sdp_domain = sdp_domain.replace('https://', '').replace('http://', '')
        sdp_domain = sdp_domain.lstrip('\\').lstrip('/').rstrip('/').strip()
        if not sdp_domain or len(sdp_domain) < 5:
            print("❌ INVALID SDP_DOMAIN")
            sys.exit(1)
        os.environ['SDP_DOMAIN'] = sdp_domain
    
    return True

print("=" * 60)
print("VALIDATING ENVIRONMENT VARIABLES")
print("=" * 60)
validate_required_env_vars()
print("=" * 60)
print("")

# Configuration
DUST_API_KEY = os.environ.get("DUST_API_KEY", "").strip().lstrip('\\').lstrip('/')
DUST_WORKSPACE_ID = os.environ.get("DUST_WORKSPACE_ID", "MotoRad").strip().lstrip('\\').lstrip('/')
DUST_DATA_SOURCE_ID = "servicedesk"
DUST_API_URL = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_DATA_SOURCE_ID}"

SDP_DOMAIN = os.environ.get("SDP_DOMAIN", "").strip()
SDP_CLIENT_ID = os.environ.get("SDP_CLIENT_ID", "").strip().lstrip('\\').lstrip('/')
SDP_CLIENT_SECRET = os.environ.get("SDP_CLIENT_SECRET", "").strip().lstrip('\\').lstrip('/')
SDP_REFRESH_TOKEN = os.environ.get("SDP_REFRESH_TOKEN", "").strip().lstrip('\\').lstrip('/')
SDP_BASE_URL = f"https://{SDP_DOMAIN}"

ZOHO_OAUTH_URL = os.environ.get("ZOHO_OAUTH_URL", "https://accounts.zoho.com/oauth/v2/token")

# Sync Configuration
FORCE_FULL_SYNC = os.environ.get("FORCE_FULL_SYNC", "false").lower() == "true"
CHECKPOINT_FILE = "sync_checkpoint.json"

print(f"✓ Configuration loaded")
print(f"  Workspace: {DUST_WORKSPACE_ID}")
print(f"  Data Source: {DUST_DATA_SOURCE_ID}")
print(f"  SDP Domain: {SDP_DOMAIN}")
print(f"  Version: v{VERSION}")
print(f"  Sync Mode: {'FULL SYNC (forced)' if FORCE_FULL_SYNC else 'INCREMENTAL'}")
print("")

# Processing Configuration
BATCH_SIZE = 50
SUB_BATCH_SIZE = 10
MAX_CONCURRENT_CONVERSATIONS = 8
MAX_CONCURRENT_UPLOADS = 3
MAX_CONCURRENT_DELETES = 20
RETRY_ATTEMPTS = 5
RETRY_DELAY = 3
UPLOAD_DELAY = 2.0
BATCH_DELAY = 3.0
MAX_429_RETRIES = 3

# Cleanup Configuration
CLEANUP_INTERVAL_HOURS = 24
LAST_CLEANUP_FILE = "last_cleanup.json"

# Debug Configuration
DEBUG_STATUS_DETECTION = True

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
# METRICS TRACKING
# ============================================================

METRICS = {
    'total_tickets_fetched': 0,
    'new_tickets': 0,
    'updated_tickets': 0,
    'total_processed': 0,
    'total_uploaded': 0,
    'skipped': 0,
    'failed_uploads': 0,
    'retried_uploads': 0,
    'rate_limited': 0,
    'conversations_fetched': 0,
    'active_tickets': 0,
    'closed_tickets': 0,
    'service_requests': 0,
    'incidents': 0,
    'total_conversations': 0,
    'api_calls': 0,
    'dust_documents_found': 0,
    'orphaned_tickets_found': 0,
    'orphaned_tickets_deleted': 0,
    'cleanup_skipped': False,
    'sync_mode': 'full' if FORCE_FULL_SYNC else 'incremental',
    'status_detection_debug': [],
    'upload_errors': {}
}

# ============================================================
# CHECKPOINT MANAGEMENT
# ============================================================

def load_checkpoint() -> Optional[datetime]:
    """Load last sync timestamp from checkpoint file"""
    if not os.path.exists(CHECKPOINT_FILE):
        logger.info("  No checkpoint found - will perform full sync")
        return None
    
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            data = json.load(f)
            last_sync = datetime.fromisoformat(data['last_sync_time'])
            logger.info(f"  Checkpoint found: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
            return last_sync
    except Exception as e:
        logger.warning(f"  Error reading checkpoint: {e} - will perform full sync")
        return None

def save_checkpoint(sync_time: datetime):
    """Save sync timestamp to checkpoint file"""
    try:
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump({
                'last_sync_time': sync_time.isoformat(),
                'version': VERSION,
                'sync_completed': datetime.now().isoformat()
            }, f, indent=2)
        logger.info(f"  ✓ Checkpoint saved: {sync_time.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        logger.warning(f"Failed to save checkpoint: {e}")

def get_sync_parameters() -> Tuple[Optional[datetime], str]:
    """
    Determine sync mode and parameters
    Returns: (last_sync_time, sync_mode)
    """
    if FORCE_FULL_SYNC:
        logger.info("  🔄 FORCE_FULL_SYNC enabled - performing full sync")
        return None, 'full'
    
    last_sync = load_checkpoint()
    if last_sync is None:
        logger.info("  🔄 No previous sync - performing full sync")
        return None, 'full'
    
    # Check if checkpoint is too old (>7 days = force full sync)
    age = datetime.now() - last_sync
    if age > timedelta(days=7):
        logger.info(f"  ⚠️  Checkpoint is {age.days} days old - performing full sync")
        return None, 'full'
    
    logger.info(f"  ✓ Performing incremental sync since {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
    return last_sync, 'incremental'

# ============================================================
# TOKEN MANAGER
# ============================================================

class TokenManager:
    """Manages ServiceDesk Plus OAuth tokens with automatic refresh"""
    
    def __init__(self):
        self.access_token = None
        self.token_expiry = None
        self.refresh_buffer = timedelta(minutes=5)
        
    def get_token(self) -> str:
        """Get valid access token, refreshing if needed"""
        now = datetime.now(timezone.utc)
        
        if (self.access_token is None or 
            self.token_expiry is None or 
            now >= (self.token_expiry - self.refresh_buffer)):
            self._refresh_token()
        
        return self.access_token
    
    def _refresh_token(self):
        """Refresh the access token"""
        if not self.access_token:
            logger.info("🔑 Fetching new token...")
        else:
            logger.info("🔄 Token expiring soon, refreshing...")
        
        payload = {
            'client_id': SDP_CLIENT_ID,
            'client_secret': SDP_CLIENT_SECRET,
            'refresh_token': SDP_REFRESH_TOKEN,
            'grant_type': 'refresh_token'
        }
        
        try:
            response = requests.post(ZOHO_OAUTH_URL, data=payload, timeout=30)
            response.raise_for_status()
            
            token_data = response.json()
            self.access_token = token_data['access_token']
            expires_in = token_data.get('expires_in', 3300)
            self.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            
            logger.info(f"✓ Token refreshed successfully (expires in {expires_in/60:.0f} minutes)")
            
        except Exception as e:
            logger.error(f"❌ Failed to refresh token: {e}")
            raise

token_manager = TokenManager()

# ============================================================
# HTTP SESSION
# ============================================================

def create_session() -> requests.Session:
    """Create requests session with retry logic"""
    session = requests.Session()
    retry_strategy = Retry(
        total=RETRY_ATTEMPTS,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE"]
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=50,
        pool_maxsize=50
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()

# ============================================================
# CLEANUP TRACKING
# ============================================================

def should_run_cleanup() -> bool:
    """Check if cleanup should run based on last cleanup time"""
    if not os.path.exists(LAST_CLEANUP_FILE):
        return True
    
    try:
        with open(LAST_CLEANUP_FILE, 'r') as f:
            data = json.load(f)
            last_cleanup = datetime.fromisoformat(data['last_cleanup'])
            hours_since = (datetime.now() - last_cleanup).total_seconds() / 3600
            
            if hours_since >= CLEANUP_INTERVAL_HOURS:
                logger.info(f"  Last cleanup was {hours_since:.1f} hours ago - running cleanup")
                return True
            else:
                logger.info(f"  Last cleanup was {hours_since:.1f} hours ago - skipping cleanup")
                METRICS['cleanup_skipped'] = True
                return False
    except Exception as e:
        logger.warning(f"  Error reading cleanup file: {e} - running cleanup")
        return True

def save_cleanup_time():
    """Save current time as last cleanup time"""
    try:
        with open(LAST_CLEANUP_FILE, 'w') as f:
            json.dump({
                'last_cleanup': datetime.now().isoformat(),
                'version': VERSION
            }, f)
    except Exception as e:
        logger.warning(f"Failed to save cleanup time: {e}")

# ============================================================
# DUST API - FETCH & DELETE
# ============================================================

def fetch_dust_documents(limit: int = 1000, offset: int = 0) -> List[str]:
    """Fetch document IDs from Dust data source"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_URL}/documents"
    params = {
        'limit': limit,
        'offset': offset
    }
    
    try:
        response = session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        documents = data.get('documents', [])
        doc_ids = [doc.get('document_id') for doc in documents if doc.get('document_id')]
        
        return doc_ids
    except Exception as e:
        logger.error(f"Failed to fetch Dust documents: {e}")
        return []

def fetch_all_dust_documents() -> Set[str]:
    """Fetch all document IDs from Dust"""
    all_doc_ids = set()
    offset = 0
    limit = 1000
    
    logger.info("  Fetching existing documents from Dust...")
    
    while True:
        doc_ids = fetch_dust_documents(limit=limit, offset=offset)
        
        if not doc_ids:
            break
        
        all_doc_ids.update(doc_ids)
        logger.info(f"    Fetched {len(doc_ids)} documents (total: {len(all_doc_ids)})")
        
        if len(doc_ids) < limit:
            break
        
        offset += limit
        time.sleep(0.5)
    
    METRICS['dust_documents_found'] = len(all_doc_ids)
    logger.info(f"  ✓ Found {len(all_doc_ids)} documents in Dust")
    return all_doc_ids

def delete_dust_document(doc_id: str) -> bool:
    """Delete a document from Dust"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_URL}/documents/{doc_id}"
    
    try:
        response = session.delete(url, headers=headers, timeout=30)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to delete document {doc_id}: {e}")
        return False

def delete_orphaned_tickets(orphaned_ids: Set[str]) -> int:
    """Delete orphaned tickets from Dust in parallel"""
    if not orphaned_ids:
        return 0
    
    logger.info(f"  Deleting {len(orphaned_ids)} orphaned tickets...")
    deleted_count = 0
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DELETES) as executor:
        futures = {executor.submit(delete_dust_document, doc_id): doc_id for doc_id in orphaned_ids}
        
        for future in as_completed(futures):
            try:
                if future.result():
                    deleted_count += 1
                    if deleted_count % 50 == 0:
                        logger.info(f"    Deleted {deleted_count}/{len(orphaned_ids)} orphaned tickets...")
            except Exception as e:
                logger.error(f"Exception deleting document: {e}")
    
    METRICS['orphaned_tickets_deleted'] = deleted_count
    logger.info(f"  ✓ Deleted {deleted_count}/{len(orphaned_ids)} orphaned tickets")
    return deleted_count

# ============================================================
# CLEANUP ORCHESTRATION
# ============================================================

def cleanup_orphaned_tickets(current_ticket_ids: Set[str]):
    """Find and delete tickets in Dust that no longer exist in ServiceDesk Plus"""
    logger.info("\n" + "=" * 60)
    logger.info("CLEANUP: Removing Orphaned Tickets")
    logger.info("=" * 60)
    
    if not should_run_cleanup():
        logger.info("  Cleanup skipped (ran recently)")
        logger.info("=" * 60)
        return
    
    dust_doc_ids = fetch_all_dust_documents()
    
    orphaned_ids = dust_doc_ids - current_ticket_ids
    METRICS['orphaned_tickets_found'] = len(orphaned_ids)
    
    if orphaned_ids:
        logger.info(f"  Found {len(orphaned_ids)} orphaned tickets to delete")
        delete_orphaned_tickets(orphaned_ids)
    else:
        logger.info(f"  ✓ No orphaned tickets found - Dust is in sync")
    
    save_cleanup_time()
    
    logger.info("=" * 60)

# ============================================================
# SERVICEDESK PLUS API - WITH INCREMENTAL SUPPORT
# ============================================================

def convert_datetime_to_sdp_timestamp(dt: datetime) -> int:
    """Convert datetime to ServiceDesk Plus millisecond timestamp"""
    return int(dt.timestamp() * 1000)

def fetch_tickets_from_sdp(start_index: int = 1, row_count: int = 100, last_sync_time: Optional[datetime] = None) -> Dict:
    """
    Fetch tickets from ServiceDesk Plus API
    
    If last_sync_time is provided, filters for tickets created or modified since that time
    """
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": row_count,
            "start_index": start_index,
            "sort_field": "last_updated_time",
            "sort_order": "desc"
        }
    }
    
    # Add filter for incremental sync
    if last_sync_time is not None:
        # ServiceDesk Plus expects milliseconds since epoch
        timestamp_ms = convert_datetime_to_sdp_timestamp(last_sync_time)
        
        # Filter for tickets created or modified after last sync
        list_info["list_info"]["filter_by"] = {
            "name": "last_updated_time",
            "value": timestamp_ms,
            "comparator": "greater_than"
        }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_BASE_URL}/api/v3/requests"
    
    try:
        METRICS['api_calls'] += 1
        response = session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        raise

def fetch_ticket_conversations(ticket_id: str) -> List[Dict]:
    """Fetch conversation metadata for a ticket"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations"
    
    try:
        METRICS['api_calls'] += 1
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        conversations = data.get('conversations', [])
        METRICS['conversations_fetched'] += 1
        METRICS['total_conversations'] += len(conversations)
        return conversations
        
    except Exception as e:
        logger.warning(f"Failed to fetch conversations for ticket {ticket_id}: {e}")
        return []

def fetch_conversations_parallel(ticket_ids: List[str]) -> Dict[str, List[Dict]]:
    """Fetch conversations for multiple tickets in parallel"""
    conversations_map = {}
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CONVERSATIONS) as executor:
        future_to_ticket = {
            executor.submit(fetch_ticket_conversations, ticket_id): ticket_id 
            for ticket_id in ticket_ids
        }
        
        for future in as_completed(future_to_ticket):
            ticket_id = future_to_ticket[future]
            try:
                conversations = future.result()
                conversations_map[ticket_id] = conversations
            except Exception as e:
                logger.error(f"Exception fetching conversations for {ticket_id}: {e}")
                conversations_map[ticket_id] = []
    
    return conversations_map

def fetch_all_tickets(last_sync_time: Optional[datetime] = None) -> List[Dict]:
    """
    Fetch tickets from ServiceDesk Plus
    
    If last_sync_time is provided, only fetches new/updated tickets
    """
    all_tickets = []
    page = 1
    row_count = 100
    
    if last_sync_time:
        logger.info(f"📊 Fetching tickets updated since {last_sync_time.strftime('%Y-%m-%d %H:%M:%S')}...")
    else:
        logger.info(f"📊 Fetching all tickets (full sync)...")
    
    while True:
        start_index = (page - 1) * row_count + 1
        
        try:
            data = fetch_tickets_from_sdp(start_index, row_count, last_sync_time)
            tickets = data.get('requests', [])
            
            if not tickets:
                logger.info(f"  Page {page}: No more tickets")
                break
            
            all_tickets.extend(tickets)
            logger.info(f"  Page {page}: {len(tickets)} tickets (total: {len(all_tickets)})")
            
            list_info = data.get('list_info', {})
            has_more = list_info.get('has_more_rows', False)
            
            if not has_more:
                logger.info(f"  Page {page}: Last page reached")
                break
            
            page += 1
            time.sleep(0.3)
            
        except Exception as e:
            logger.error(f"Error fetching page {page}: {e}")
            break
    
    METRICS['total_tickets_fetched'] = len(all_tickets)
    
    # Classify tickets as new or updated
    if last_sync_time:
        for ticket in all_tickets:
            created_time_ms = safe_get(ticket, 'created_time', 'value', default=0)
            created_time = datetime.fromtimestamp(created_time_ms / 1000) if created_time_ms else None
            
            if created_time and created_time > last_sync_time:
                METRICS['new_tickets'] += 1
            else:
                METRICS['updated_tickets'] += 1
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets total")
    if last_sync_time:
        logger.info(f"  New tickets: {METRICS['new_tickets']}")
        logger.info(f"  Updated tickets: {METRICS['updated_tickets']}")
    logger.info("")
    return all_tickets

# ============================================================
# FIELD EXTRACTION & PROCESSING - FIXED STATUS DETECTION
# ============================================================

def safe_get(obj, *keys, default=None):
    """Safely get nested dictionary values"""
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key, default)
        else:
            return default
    return obj if obj is not None else default

def clean_html(html_text: str) -> str:
    """Clean HTML from text"""
    if not html_text:
        return ""
    text = re.sub(r'<[^>]+>', '', html_text)
    text = unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def normalize_status_string(status: str) -> str:
    """Normalize status string for comparison"""
    if not status:
        return ""
    return status.lower().replace('-', ' ').replace('_', ' ').strip()

def determine_status_category(status: str, status_internal: str, ticket_id: str = "unknown") -> str:
    """
    Determine if ticket is active or closed with enhanced detection
    
    FIXED: v3.1.21 - Properly detects Open, On Hold, Pending, In Progress as ACTIVE
    """
    status_norm = normalize_status_string(status)
    status_internal_norm = normalize_status_string(status_internal)
    
    closed_keywords = ['closed', 'resolved', 'completed', 'cancelled', 'rejected', 'declined']
    active_keywords = ['open', 'pending', 'progress', 'hold', 'waiting', 'assigned', 'new', 'acknowledged']
    
    for keyword in closed_keywords:
        if keyword in status_norm or keyword in status_internal_norm:
            if DEBUG_STATUS_DETECTION and len(METRICS['status_detection_debug']) < 20:
                METRICS['status_detection_debug'].append({
                    'ticket_id': ticket_id,
                    'status': status,
                    'internal': status_internal,
                    'category': 'closed',
                    'reason': f'matched closed keyword: {keyword}'
                })
            return 'closed'
    
    for keyword in active_keywords:
        if keyword in status_norm or keyword in status_internal_norm:
            if DEBUG_STATUS_DETECTION and len(METRICS['status_detection_debug']) < 20:
                METRICS['status_detection_debug'].append({
                    'ticket_id': ticket_id,
                    'status': status,
                    'internal': status_internal,
                    'category': 'active',
                    'reason': f'matched active keyword: {keyword}'
                })
            return 'active'
    
    if DEBUG_STATUS_DETECTION and len(METRICS['status_detection_debug']) < 20:
        METRICS['status_detection_debug'].append({
            'ticket_id': ticket_id,
            'status': status,
            'internal': status_internal,
            'category': 'closed',
            'reason': 'no keywords matched - defaulted to closed'
        })
    
    logger.warning(f"Ticket {ticket_id}: Could not determine status category for status='{status}', internal='{status_internal}' - defaulting to closed")
    return 'closed'

def extract_comprehensive_fields(ticket: Dict, conversations: List[Dict]) -> Dict[str, Any]:
    """Extract all comprehensive fields from ticket with FIXED status detection"""
    
    ticket_id = safe_get(ticket, 'display_id', default='Unknown')
    subject = safe_get(ticket, 'subject', default='No Subject')
    description = clean_html(safe_get(ticket, 'description', default=''))
    status = safe_get(ticket, 'status', 'name', default='Unknown')
    status_internal = safe_get(ticket, 'status', 'internal_name', default=status)
    priority = safe_get(ticket, 'priority', 'name', default='Normal')
    requester_name = safe_get(ticket, 'requester', 'name', default='Unknown')
    requester_email = safe_get(ticket, 'requester', 'email_id', default='')
    technician_name = safe_get(ticket, 'technician', 'name', default='Unassigned')
    technician_email = safe_get(ticket, 'technician', 'email_id', default='')
    created_by_name = safe_get(ticket, 'created_by', 'name', default='Unknown')
    category = safe_get(ticket, 'category', 'name', default='Uncategorized')
    subcategory = safe_get(ticket, 'subcategory', 'name', default='')
    group_name = safe_get(ticket, 'group', 'name', default='')
    created_time = safe_get(ticket, 'created_time', 'display_value', default='')
    due_by_time = safe_get(ticket, 'due_by_time', 'display_value', default='')
    resolved_time = safe_get(ticket, 'resolved_time', 'display_value', default='')
    is_service_request = safe_get(ticket, 'is_service_request', default=False)
    ticket_type = 'Service Request' if is_service_request else 'Incident'
    sla_name = safe_get(ticket, 'sla', 'name', default='')
    is_overdue = safe_get(ticket, 'is_overdue', default=False)
    is_escalated = safe_get(ticket, 'is_escalated', default=False)
    closure_comments = safe_get(ticket, 'closure_info', 'closure_comments', default='')
    closure_code = safe_get(ticket, 'closure_info', 'closure_code', 'name', default='')
    
    status_category = determine_status_category(status, status_internal, ticket_id)
    
    conversation_count = len(conversations)
    conversation_types = {}
    for conv in conversations:
        conv_type = conv.get('type', 'Unknown')
        conversation_types[conv_type] = conversation_types.get(conv_type, 0) + 1
    
    complexity = 5
    if len(description) > 500:
        complexity += 1
    if len(description) > 1000:
        complexity += 1
    if conversation_count > 5:
        complexity += 1
    if conversation_count > 10:
        complexity += 1
    if is_escalated:
        complexity += 1
    if priority in ['High', 'Urgent']:
        complexity += 1
    complexity = min(10, max(0, complexity))
    
    activity_level = 'Low'
    if conversation_count > 5:
        activity_level = 'Moderate'
    if conversation_count > 10:
        activity_level = 'High'
    
    fields = {
        'ticket_id': ticket_id,
        'subject': subject,
        'description': description,
        'status': status,
        'status_category': status_category,
        'priority': priority,
        'category': category,
        'subcategory': subcategory,
        'group': group_name,
        'ticket_type': ticket_type,
        'requester_name': requester_name,
        'requester_email': requester_email,
        'technician_name': technician_name,
        'technician_email': technician_email,
        'created_by': created_by_name,
        'created_time': created_time,
        'due_by_time': due_by_time,
        'resolved_time': resolved_time,
        'sla': sla_name,
        'is_overdue': is_overdue,
        'is_escalated': is_escalated,
        'closure_comments': clean_html(closure_comments),
        'closure_code': closure_code,
        'conversation_count': conversation_count,
        'conversation_types': conversation_types,
        'activity_level': activity_level,
        'complexity_score': complexity,
    }
    
    return fields

def build_document_content(fields: Dict) -> str:
    """Build comprehensive document content"""
    parts = []
    
    parts.append(f"# Ticket #{fields['ticket_id']}\n")
    parts.append(f"**Subject:** {fields['subject']}\n\n")
    parts.append(f"**Status:** {fields['status']} ({fields['status_category']})")
    if fields['is_overdue']:
        parts.append(" ⚠️ **OVERDUE**")
    if fields['is_escalated']:
        parts.append(" ⚠️ **ESCALATED**")
    parts.append("\n\n")
    parts.append(f"**Priority:** {fields['priority']}\n")
    parts.append(f"**Type:** {fields['ticket_type']}\n")
    parts.append(f"**Category:** {fields['category']}\n")
    if fields['subcategory']:
        parts.append(f"**Subcategory:** {fields['subcategory']}\n")
    if fields['group']:
        parts.append(f"**Group:** {fields['group']}\n")
    if fields['sla']:
        parts.append(f"**SLA:** {fields['sla']}\n")
    parts.append("\n")
    parts.append(f"**Requester:** {fields['requester_name']}\n")
    parts.append(f"**Technician:** {fields['technician_name']}\n")
    parts.append(f"**Created By:** {fields['created_by']}\n\n")
    parts.append(f"## Timeline\n\n")
    parts.append(f"**Created:** {fields['created_time']}\n")
    if fields['due_by_time']:
        parts.append(f"**Due:** {fields['due_by_time']}\n")
    if fields['resolved_time']:
        parts.append(f"**Resolved:** {fields['resolved_time']}\n")
    parts.append("\n")
    if fields['description']:
        parts.append(f"## Description\n\n{fields['description']}\n\n")
    if fields['conversation_count'] > 0:
        parts.append(f"## Conversation Activity\n\n")
        parts.append(f"**Total Exchanges:** {fields['conversation_count']}\n")
        parts.append(f"**Activity Level:** {fields['activity_level']}\n")
        if fields['conversation_types']:
            parts.append(f"**Exchange Types:**\n")
            for conv_type, count in fields['conversation_types'].items():
                parts.append(f"  - {conv_type}: {count}\n")
        parts.append("\n")
    if fields['status_category'] == 'closed':
        parts.append(f"## Resolution\n\n")
        if fields['closure_code']:
            parts.append(f"**Closure Code:** {fields['closure_code']}\n")
        if fields['closure_comments']:
            parts.append(f"**Comments:** {fields['closure_comments']}\n")
        parts.append("\n")
    parts.append(f"## AI Intelligence\n\n")
    parts.append(f"**Complexity Score:** {fields['complexity_score']}/10\n")
    parts.append(f"**Activity Level:** {fields['activity_level']}\n")
    if fields['complexity_score'] >= 8:
        parts.append(f"**Assessment:** High complexity - requires senior technical expertise\n")
    elif fields['complexity_score'] >= 6:
        parts.append(f"**Assessment:** Moderate complexity - standard technical issue\n")
    else:
        parts.append(f"**Assessment:** Low complexity - routine support request\n")
    
    return "".join(parts)

def build_tags(fields: Dict) -> List[str]:
    """Build comprehensive searchable tags"""
    tags = []
    tags.append(f"ticket-id:{fields['ticket_id']}")
    tags.append(f"status:{fields['status'].lower().replace(' ', '-')}")
    tags.append(f"status-category:{fields['status_category']}")
    tags.append(f"priority:{fields['priority'].lower()}")
    tags.append(f"type:{fields['ticket_type'].lower().replace(' ', '-')}")
    if fields['category']:
        tags.append(f"category:{fields['category'].lower().replace(' ', '-')}")
    if fields['technician_name'] != 'Unassigned':
        tags.append(f"technician:{fields['technician_name'].lower().replace(' ', '-')}")
    else:
        tags.append("unassigned")
    if fields['status_category'] == 'active':
        tags.extend(['active', 'open-status', 'needs-attention', 'ACTIVE-TICKET', 'IS-OPEN', 'NOT-CLOSED'])
    else:
        tags.extend(['closed', 'CLOSED-TICKET', 'IS-CLOSED', 'NOT-ACTIVE'])
    if fields['is_overdue']:
        tags.extend(['overdue', 'sla-risk:critical'])
    if fields['is_escalated']:
        tags.append('escalated')
    tags.append(f"complexity:{fields['complexity_score']}")
    if fields['complexity_score'] >= 8:
        tags.append('complexity:high')
    elif fields['complexity_score'] >= 6:
        tags.append('complexity:medium')
    else:
        tags.append('complexity:low')
    tags.append(f"activity:{fields['activity_level'].lower()}")
    if fields['conversation_count'] > 0:
        tags.append('has-conversations')
    
    return tags

def process_single_ticket(ticket: Dict, conversations: List[Dict]) -> Optional[Dict]:
    """Process a single ticket and return document data"""
    try:
        fields = extract_comprehensive_fields(ticket, conversations)
        
        document = {
            'id': str(fields['ticket_id']),
            'title': f"Ticket #{fields['ticket_id']}: {fields['subject']}",
            'content': build_document_content(fields),
            'tags': build_tags(fields),
            'metadata': {
                'ticket_id': fields['ticket_id'],
                'status': fields['status'],
                'status_category': fields['status_category'],
                'priority': fields['priority'],
                'technician': fields['technician_name'],
                'complexity': fields['complexity_score'],
                'conversation_count': fields['conversation_count'],
            }
        }
        
        METRICS['total_processed'] += 1
        if fields['status_category'] == 'active':
            METRICS['active_tickets'] += 1
        else:
            METRICS['closed_tickets'] += 1
        
        if fields['ticket_type'] == 'Service Request':
            METRICS['service_requests'] += 1
        else:
            METRICS['incidents'] += 1
        
        return document
        
    except Exception as e:
        logger.error(f"Error processing ticket {ticket.get('display_id', 'unknown')}: {e}")
        METRICS['skipped'] += 1
        return None

# ============================================================
# DUST API WITH RETRY LOGIC
# ============================================================

def upsert_to_dust_with_retry(document: Dict, max_retries: int = MAX_429_RETRIES) -> bool:
    """Upload document to Dust with exponential backoff for 429 errors"""
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    url = f"{DUST_API_URL}/documents/{document['id']}"
    
    payload = {
        'text': document['content'],
        'source_url': None,
        'timestamp': None,
        'tags': document['tags'],
        'parents': None,
        'light_document_output': True
    }
    
    for attempt in range(max_retries + 1):
        try:
            response = session.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            return True
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                METRICS['rate_limited'] += 1
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 2
                    logger.warning(f"Rate limited on ticket {document['id']}, waiting {wait_time}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    METRICS['retried_uploads'] += 1
                    continue
                else:
                    logger.error(f"Failed to upload ticket {document['id']} after {max_retries} retries (rate limited)")
                    METRICS['failed_uploads'] += 1
                    return False
            else:
                error_type = type(e).__name__
                METRICS['upload_errors'][error_type] = METRICS['upload_errors'].get(error_type, 0) + 1
                METRICS['failed_uploads'] += 1
                logger.error(f"Failed to upload ticket {document['id']}: {e}")
                return False
                
        except Exception as e:
            error_type = type(e).__name__
            METRICS['upload_errors'][error_type] = METRICS['upload_errors'].get(error_type, 0) + 1
            METRICS['failed_uploads'] += 1
            logger.error(f"Failed to upload ticket {document['id']}: {e}")
            return False
    
    return False

def upload_batch_parallel(documents: List[Dict]) -> Tuple[int, int]:
    """Upload batch of documents in parallel with rate limiting"""
    success_count = 0
    failure_count = 0
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS) as executor:
        futures = {executor.submit(upsert_to_dust_with_retry, doc): doc for doc in documents}
        
        for future in as_completed(futures):
            try:
                if future.result():
                    success_count += 1
                    METRICS['total_uploaded'] += 1
                else:
                    failure_count += 1
            except Exception as e:
                failure_count += 1
                METRICS['failed_uploads'] += 1
                logger.error(f"Upload exception: {e}")
            
            time.sleep(0.2)
    
    return success_count, failure_count

# ============================================================
# MAIN PROCESSING
# ============================================================

def process_ticket_batch(tickets: List[Dict], batch_num: int, total_batches: int) -> None:
    """Process a batch of tickets"""
    logger.info(f"\n{'='*60}")
    logger.info(f"BATCH {batch_num}/{total_batches} ({len(tickets)} tickets)")
    logger.info(f"{'='*60}")
    
    logger.info("  Fetching conversation metadata...")
    ticket_ids = [str(t.get('id')) for t in tickets]
    conversations_map = fetch_conversations_parallel(ticket_ids)
    logger.info(f"  ✓ Fetched conversations for {len(conversations_map)} tickets")
    
    logger.info("  Processing tickets...")
    documents = []
    for ticket in tickets:
        ticket_id = str(ticket.get('id'))
        conversations = conversations_map.get(ticket_id, [])
        doc = process_single_ticket(ticket, conversations)
        if doc:
            documents.append(doc)
    
    logger.info(f"  ✓ Processed {len(documents)} documents")
    
    if documents:
        logger.info("  Uploading to Dust (with rate limit protection)...")
        success, failure = upload_batch_parallel(documents)
        logger.info(f"  ✓ Uploaded {success}/{len(documents)} documents")
        if failure > 0:
            logger.warning(f"  ⚠️  {failure} upload failures")
    
    if batch_num < total_batches:
        logger.info(f"  ⏸️  Pausing {BATCH_DELAY}s before next batch...")
        time.sleep(BATCH_DELAY)

def print_status_detection_debug():
    """Print status detection debug information"""
    if not DEBUG_STATUS_DETECTION or not METRICS['status_detection_debug']:
        return
    
    logger.info("\n" + "=" * 60)
    logger.info("STATUS DETECTION DEBUG (Sample)")
    logger.info("=" * 60)
    
    for sample in METRICS['status_detection_debug']:
        logger.info(f"  Ticket {sample['ticket_id']}:")
        logger.info(f"    Status: '{sample['status']}'")
        logger.info(f"    Internal: '{sample['internal']}'")
        logger.info(f"    Category: {sample['category']}")
        logger.info(f"    Reason: {sample['reason']}")
    
    logger.info("=" * 60)

def print_final_summary():
    """Print comprehensive final summary"""
    logger.info("\n" + "=" * 60)
    logger.info("SYNC COMPLETE - FINAL SUMMARY")
    logger.info("=" * 60)
    
    logger.info(f"\n📊 TICKET STATISTICS:")
    logger.info(f"  Sync Mode:         {METRICS['sync_mode']}")
    logger.info(f"  Total Fetched:     {METRICS['total_tickets_fetched']}")
    if METRICS['sync_mode'] == 'incremental':
        logger.info(f"  New Tickets:       {METRICS['new_tickets']}")
        logger.info(f"  Updated Tickets:   {METRICS['updated_tickets']}")
    logger.info(f"  Total Processed:   {METRICS['total_processed']}")
    logger.info(f"  Total Uploaded:    {METRICS['total_uploaded']}")
    logger.info(f"  Skipped:           {METRICS['skipped']}")
    logger.info(f"  Upload Failures:   {METRICS['failed_uploads']}")
    logger.info(f"  Retried:           {METRICS['retried_uploads']}")
    logger.info(f"  Rate Limited:      {METRICS['rate_limited']}")
    
    if METRICS['total_processed'] > 0:
        success_rate = (METRICS['total_uploaded'] / METRICS['total_processed']) * 100
        logger.info(f"  Success Rate:      {success_rate:.1f}%")
    
    logger.info(f"\n📈 TICKET BREAKDOWN:")
    logger.info(f"  Active Tickets:    {METRICS['active_tickets']}")
    logger.info(f"  Closed Tickets:    {METRICS['closed_tickets']}")
    logger.info(f"  Service Requests:  {METRICS['service_requests']}")
    logger.info(f"  Incidents:         {METRICS['incidents']}")
    
    logger.info(f"\n💬 CONVERSATION DATA:")
    logger.info(f"  Conversations Fetched:     {METRICS['conversations_fetched']}")
    logger.info(f"  Total Exchanges:           {METRICS['total_conversations']}")
    if METRICS['conversations_fetched'] > 0:
        avg = METRICS['total_conversations'] / METRICS['conversations_fetched']
        logger.info(f"  Average per Ticket:        {avg:.1f}")
    
    logger.info(f"\n🧹 CLEANUP STATISTICS:")
    if METRICS['cleanup_skipped']:
        logger.info(f"  Cleanup: Skipped (ran recently)")
    else:
        logger.info(f"  Dust Documents Found:      {METRICS['dust_documents_found']}")
        logger.info(f"  Orphaned Tickets Found:    {METRICS['orphaned_tickets_found']}")
        logger.info(f"  Orphaned Tickets Deleted:  {METRICS['orphaned_tickets_deleted']}")
    
    logger.info(f"\n🔧 API STATISTICS:")
    logger.info(f"  Total API Calls:   {METRICS['api_calls']}")
    
    if METRICS['upload_errors']:
        logger.info(f"\n⚠️  UPLOAD ERRORS:")
        for error_type, count in METRICS['upload_errors'].items():
            logger.info(f"  {error_type}: {count}")
    
    print_status_detection_debug()
    
    logger.info("\n" + "=" * 60)
    logger.info(f"✓ ServiceDesk Intelligence Suite v{VERSION}")
    logger.info("  Status Fix + Incremental Sync")
    logger.info("=" * 60)

def main():
    """Main execution"""
    start_time = time.time()
    sync_start_time = datetime.now()
    
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 60)
    logger.info(f"Sync starting at {sync_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")
    
    try:
        token_manager.get_token()
        
        # Determine sync mode
        last_sync_time, sync_mode = get_sync_parameters()
        METRICS['sync_mode'] = sync_mode
        
        # Fetch tickets (filtered if incremental)
        tickets = fetch_all_tickets(last_sync_time)
        
        if not tickets:
            logger.info("✓ No tickets to process")
            # Still save checkpoint
            save_checkpoint(sync_start_time)
            return 0
        
        current_ticket_ids = {str(t.get('display_id')) for t in tickets}
        
        # Process tickets
        total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(f"Processing {len(tickets)} tickets in {total_batches} batches of {BATCH_SIZE}")
        
        if sync_mode == 'incremental':
            logger.info(f"Estimated time: {total_batches * 0.2:.0f} minutes (incremental)\n")
        else:
            logger.info(f"Estimated time: {total_batches * 0.7:.0f} minutes (full sync)\n")
        
        for i in range(0, len(tickets), BATCH_SIZE):
            batch = tickets[i:i+BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            
            try:
                process_ticket_batch(batch, batch_num, total_batches)
            except Exception as e:
                logger.error(f"Error processing batch {batch_num}: {e}")
                continue
        
        # Cleanup (only on full sync or periodically)
        if sync_mode == 'full':
            cleanup_orphaned_tickets(current_ticket_ids)
        
        # Save checkpoint for next incremental sync
        save_checkpoint(sync_start_time)
        
        elapsed_time = time.time() - start_time
        logger.info(f"\n⏱️  Total execution time: {elapsed_time/60:.1f} minutes")
        print_final_summary()
        
        return 0
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Sync interrupted by user")
        print_final_summary()
        return 130
    
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        print_final_summary()
        return 1

if __name__ == "__main__":
    sys.exit(main())
