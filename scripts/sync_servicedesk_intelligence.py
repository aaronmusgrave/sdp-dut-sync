#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.10
=====================================

Enhanced tagging for open/active ticket search reliability.

New in v3.1.10:
- Enhanced status tagging with prominent ACTIVE-TICKET and IS-OPEN flags
- Multiple redundant tags for better open ticket search
- Improved closed ticket tagging with NOT-ACTIVE flags
- Status-specific tags (STATUS-OPEN, CURRENTLY-OPEN, etc.)
- Environment variable validation with helpful error messages

Retained from v3.1.9:
- Token Manager with auto-refresh (prevents 401 failures)
- Parallel cleanup deletion (51 min → 3 min)
- Smart cleanup detection (skip when unnecessary)
- Increased parallel workers (20 conversation, 10 upload)
- Progress tracking and speed metrics
"""

import os
import sys
import requests
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# VERSION & CONFIGURATION
# ============================================================

VERSION = "3.1.10"

# Validate environment variables first
def validate_required_env_vars():
    """Validate all required environment variables are set"""
    required = {
        'DUST_API_KEY': 'Dust API key',
        'DUST_WORKSPACE_ID': 'Dust workspace ID',
        'SDP_DOMAIN': 'ServiceDesk Plus domain (e.g., motorad.sdpondemand.com)',
        'SDP_CLIENT_ID': 'ServiceDesk Plus OAuth client ID',
        'SDP_CLIENT_SECRET': 'ServiceDesk Plus OAuth client secret',
        'SDP_REFRESH_TOKEN': 'ServiceDesk Plus OAuth refresh token'
    }
    
    missing = []
    empty = []
    
    for var, description in required.items():
        value = os.environ.get(var)
        if not value:
            missing.append(f"{var} ({description})")
        elif not value.strip():
            empty.append(f"{var} ({description})")
    
    if missing:
        print("=" * 60)
        print("❌ MISSING REQUIRED ENVIRONMENT VARIABLES")
        print("=" * 60)
        for var in missing:
            print(f"  • {var}")
        print("=" * 60)
        print("\nPlease set these variables in your GitHub repository secrets:")
        print("Settings → Secrets and variables → Actions → New repository secret")
        print("=" * 60)
        sys.exit(1)
    
    if empty:
        print("=" * 60)
        print("❌ EMPTY ENVIRONMENT VARIABLES")
        print("=" * 60)
        for var in empty:
            print(f"  • {var}")
        print("=" * 60)
        sys.exit(1)
    
    # Validate SDP_DOMAIN format
    sdp_domain = os.environ.get('SDP_DOMAIN', '').strip()
    if sdp_domain:
        # Remove https:// or http:// if present
        sdp_domain = sdp_domain.replace('https://', '').replace('http://', '')
        # Remove trailing slash if present
        sdp_domain = sdp_domain.rstrip('/')
        
        if not sdp_domain or sdp_domain == '***' or len(sdp_domain) < 5:
            print("=" * 60)
            print("❌ INVALID SDP_DOMAIN")
            print("=" * 60)
            print(f"Current value appears invalid")
            print("\nSDP_DOMAIN should be just the domain name, for example:")
            print("  motorad.sdpondemand.com")
            print("\nDO NOT include:")
            print("  ❌ https://")
            print("  ❌ http://")
            print("  ❌ Trailing slashes")
            print("=" * 60)
            sys.exit(1)
        
        # Update the environment variable with cleaned value
        os.environ['SDP_DOMAIN'] = sdp_domain
        # Show first 20 chars for verification (security)
        domain_display = sdp_domain[:20] + "..." if len(sdp_domain) > 20 else sdp_domain
        print(f"✓ SDP_DOMAIN validated: {domain_display}")
    
    return True

# Validate before setting configuration
print("Validating environment variables...")
validate_required_env_vars()
print("")

# Dust Configuration
DUST_API_KEY = os.environ.get("DUST_API_KEY")
DUST_WORKSPACE_ID = os.environ.get("DUST_WORKSPACE_ID", "MotoRad")
DUST_DATA_SOURCE_ID = "servicedesk_intelligence"
DUST_API_URL = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_DATA_SOURCE_ID}"

# ServiceDesk Plus Configuration
SDP_DOMAIN = os.environ.get("SDP_DOMAIN").strip()
SDP_CLIENT_ID = os.environ.get("SDP_CLIENT_ID")
SDP_CLIENT_SECRET = os.environ.get("SDP_CLIENT_SECRET")
SDP_REFRESH_TOKEN = os.environ.get("SDP_REFRESH_TOKEN")
SDP_BASE_URL = f"https://{SDP_DOMAIN}"

print(f"✓ Configuration loaded")
print(f"  Workspace: {DUST_WORKSPACE_ID}")
print(f"  Data Source: {DUST_DATA_SOURCE_ID}")
print(f"  SDP Base URL: {SDP_BASE_URL}")
print("")

# Processing Configuration
BATCH_SIZE = 50
MAX_PAGES = None
MAX_CONCURRENT_CONVERSATIONS = 20
MAX_CONCURRENT_UPLOADS = 10
MAX_CONCURRENT_DELETES = 20
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2

# File paths
CHECKPOINT_FILE = "checkpoint.json"
LAST_SYNC_FILE = "last_sync_time.txt"
SKIP_REPORT_FILE = "skipped_tickets_report.txt"
LAST_CLEANUP_FILE = "last_cleanup.json"

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
    'total_processed': 0,
    'skipped': 0,
    'retried': 0,
    'failed_after_retry': 0,
    'requests': 0,
    'service_requests': 0,
    'incidents': 0,
    'auto_categorized': 0,
    'active_tickets': 0
}

SKIPPED_TICKETS = []

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
        
        token_url = f"{SDP_BASE_URL}/oauth/v1/token"
        
        logger.info(f"  Token URL: {token_url}")
        
        payload = {
            'client_id': SDP_CLIENT_ID,
            'client_secret': SDP_CLIENT_SECRET,
            'refresh_token': SDP_REFRESH_TOKEN,
            'grant_type': 'refresh_token'
        }
        
        try:
            response = requests.post(token_url, data=payload, timeout=30)
            response.raise_for_status()
            
            token_data = response.json()
            self.access_token = token_data['access_token']
            expires_in = token_data.get('expires_in', 3300)
            self.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            
            logger.info(f"✓ Token refreshed successfully (expires in {expires_in/60:.0f} minutes)")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to refresh token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"  Response status: {e.response.status_code}")
                logger.error(f"  Response body: {e.response.text[:500]}")
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
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()

# ============================================================
# CHECKPOINT MANAGEMENT
# ============================================================

def load_checkpoint() -> Optional[Dict]:
    """Load checkpoint data if exists"""
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load checkpoint: {e}")
    return None

def save_checkpoint(timestamp: str, stats: Dict):
    """Save checkpoint data"""
    try:
        checkpoint_data = {
            'last_sync_time': timestamp,
            'processed': stats.get('total_processed', 0),
            'errors': stats.get('failed_after_retry', 0),
            'skipped': stats.get('skipped', 0),
            'saved_at': datetime.now(timezone.utc).isoformat()
        }
        
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)
        
        logger.info(f"✓ Checkpoint saved: {checkpoint_data['processed']} processed")
        
    except Exception as e:
        logger.error(f"Failed to save checkpoint: {e}")

def load_last_sync_time() -> Optional[datetime]:
    """Load last successful sync timestamp"""
    if os.path.exists(LAST_SYNC_FILE):
        try:
            with open(LAST_SYNC_FILE, 'r') as f:
                timestamp_str = f.read().strip()
                return datetime.fromisoformat(timestamp_str)
        except Exception as e:
            logger.warning(f"Could not load last sync time: {e}")
    return None

def save_last_sync_time(timestamp: datetime):
    """Save sync start timestamp"""
    try:
        with open(LAST_SYNC_FILE, 'w') as f:
            f.write(timestamp.isoformat())
        logger.info(f"✓ Sync timestamp saved")
    except Exception as e:
        logger.error(f"Failed to save sync time: {e}")

def load_last_cleanup() -> Optional[Dict]:
    """Load last cleanup timestamp"""
    if os.path.exists(LAST_CLEANUP_FILE):
        try:
            with open(LAST_CLEANUP_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load cleanup tracking: {e}")
    return None

def save_cleanup_tracking(deleted_count: int):
    """Save cleanup completion tracking"""
    try:
        cleanup_data = {
            'last_cleanup': datetime.now(timezone.utc).isoformat(),
            'documents_deleted': deleted_count,
            'next_cleanup_needed': False
        }
        
        with open(LAST_CLEANUP_FILE, 'w') as f:
            json.dump(cleanup_data, f, indent=2)
        
    except Exception as e:
        logger.error(f"Failed to save cleanup tracking: {e}")

def should_run_cleanup() -> bool:
    """Determine if cleanup should run"""
    last_cleanup = load_last_cleanup()
    checkpoint = load_checkpoint()
    
    if not last_cleanup:
        logger.info("🧹 No cleanup record - cleanup recommended")
        return True
    
    try:
        last_cleanup_time = datetime.fromisoformat(last_cleanup['last_cleanup'])
        age = datetime.now(timezone.utc) - last_cleanup_time
        
        if age.days > 7:
            logger.info(f"🧹 Last cleanup was {age.days} days ago - cleanup recommended")
            return True
    except Exception as e:
        logger.warning(f"Could not determine cleanup age: {e}")
        return True
    
    if not checkpoint:
        logger.info("🧹 Full sync detected - cleanup recommended")
        return True
    
    logger.info("✓ Recent cleanup found, skipping cleanup")
    return False

# ============================================================
# DUST API FUNCTIONS
# ============================================================

def fetch_all_dust_documents() -> List[str]:
    """Fetch all document IDs from Dust"""
    logger.info("🔍 Fetching existing documents from Dust...")
    
    document_ids = []
    offset = 0
    limit = 100
    
    headers = {
        "Authorization": f"Bearer {DUST_API_KEY}",
        "Content-Type": "application/json"
    }
    
    while True:
        try:
            url = f"{DUST_API_URL}/documents?offset={offset}&limit={limit}"
            response = session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            documents = data.get('documents', [])
            
            if not documents:
                break
            
            for doc in documents:
                document_ids.append(doc['document_id'])
            
            offset += len(documents)
            
            if len(documents) < limit:
                break
                
        except Exception as e:
            logger.error(f"Error fetching documents at offset {offset}: {e}")
            break
    
    logger.info(f"✓ Found {len(document_ids)} existing documents")
    return document_ids

def delete_dust_document(doc_id: str) -> bool:
    """Delete a single document from Dust"""
    headers = {
        "Authorization": f"Bearer {DUST_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        url = f"{DUST_API_URL}/documents/{doc_id}"
        response = session.delete(url, headers=headers, timeout=30)
        response.raise_for_status()
        return True
    except Exception:
        return False

def cleanup_dust_data_source_parallel(document_ids: List[str]) -> tuple:
    """Delete all documents from Dust in parallel"""
    if not document_ids:
        return 0, 0
    
    logger.info(f"🗑️  Deleting {len(document_ids)} documents (parallel with {MAX_CONCURRENT_DELETES} workers)...")
    
    successful = 0
    failed = 0
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DELETES) as executor:
        future_to_doc = {
            executor.submit(delete_dust_document, doc_id): doc_id 
            for doc_id in document_ids
        }
        
        for future in as_completed(future_to_doc):
            if future.result():
                successful += 1
            else:
                failed += 1
            
            total = successful + failed
            if total % 100 == 0:
                elapsed = time.time() - start_time
                rate = total / elapsed if elapsed > 0 else 0
                logger.info(f"  Progress: {total}/{len(document_ids)} ({rate:.1f}/sec)...")
    
    elapsed = time.time() - start_time
    rate = len(document_ids) / elapsed if elapsed > 0 else 0
    
    logger.info(f"✓ Cleanup complete: {successful} deleted ({rate:.1f}/sec)")
    return successful, failed

def cleanup_dust_data_source():
    """Main cleanup function"""
    logger.info("\n" + "=" * 60)
    logger.info(f"🧹 DUST DATA SOURCE CLEANUP (v{VERSION})")
    logger.info("=" * 60 + "\n")
    
    document_ids = fetch_all_dust_documents()
    
    if not document_ids:
        logger.info("✓ No documents to clean up")
        save_cleanup_tracking(0)
        return
    
    logger.info(f"⚠️  Proceeding with cleanup in 3 seconds...")
    time.sleep(3)
    
    successful, failed = cleanup_dust_data_source_parallel(document_ids)
    
    if failed > 0:
        logger.warning(f"⚠️  {failed} documents could not be deleted")
    
    save_cleanup_tracking(successful)
    logger.info("")

# ============================================================
# SERVICEDESK PLUS API
# ============================================================

def fetch_tickets_from_sdp(start_index: int = 1, row_count: int = 100, 
                          last_sync_time: Optional[datetime] = None) -> Dict:
    """Fetch tickets from ServiceDesk Plus API"""
    access_token = token_manager.get_token()
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    list_info = {
        "row_count": row_count,
        "start_index": start_index,
        "get_total_count": True,
        "sort_field": "created_time",
        "sort_order": "desc"
    }
    
    if last_sync_time:
        timestamp_ms = int(last_sync_time.timestamp() * 1000)
        list_info["search_criteria"] = {
            "field": "created_time",
            "condition": "greater than",
            "value": timestamp_ms
        }
    
    input_data = {"list_info": list_info}
    url = f"{SDP_BASE_URL}/api/v3/requests"
    
    try:
        response = session.get(
            url,
            headers=headers,
            params={"input_data": json.dumps(input_data)},
            timeout=30
        )
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"  Response status: {e.response.status_code}")
            logger.error(f"  Response body: {e.response.text[:500]}")
        raise

def fetch_ticket_conversations(ticket_id: str) -> List[Dict]:
    """Fetch conversation history for a ticket"""
    access_token = token_manager.get_token()
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations"
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data.get('conversations', [])
    except Exception:
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
            except Exception:
                conversations_map[ticket_id] = []
    
    return conversations_map

def fetch_all_tickets(max_pages: Optional[int] = None, 
                     last_sync_time: Optional[datetime] = None) -> List[Dict]:
    """Fetch all tickets with pagination"""
    all_tickets = []
    page = 1
    row_count = 100
    
    if last_sync_time:
        logger.info(f"📊 INCREMENTAL SYNC: Fetching tickets modified after {last_sync_time}")
    else:
        logger.info(f"📊 FULL SYNC: Fetching ALL tickets")
    
    while True:
        start_index = (page - 1) * row_count + 1
        
        try:
            data = fetch_tickets_from_sdp(start_index, row_count, last_sync_time)
            tickets = data.get('requests', [])
            
            if not tickets:
                break
            
            all_tickets.extend(tickets)
            logger.info(f"  Page {page}: {len(tickets)} tickets (Total: {len(all_tickets)})")
            
            page += 1
            
            if max_pages and page > max_pages:
                break
            
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error fetching page {page}: {e}")
            break
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets\n")
    return all_tickets

# ============================================================
# TICKET PROCESSING
# ============================================================

def safe_get(obj, *keys, default=None):
    """Safely get nested dictionary values"""
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key, default)
        else:
            return default
    return obj if obj is not None else default

def extract_comprehensive_fields(ticket: Dict) -> Dict[str, Any]:
    """Extract all fields from ticket"""
    fields = {
        'ticket_id': safe_get(ticket, 'display_id', default='Unknown'),
        'subject': safe_get(ticket, 'subject', default='No Subject'),
        'description': safe_get(ticket, 'description', default=''),
        'status': safe_get(ticket, 'status', 'name', default='Unknown'),
        'status_category': 'active' if safe_get(ticket, 'status', 'name', default='').lower() not in ['closed', 'resolved'] else 'closed',
        'priority': safe_get(ticket, 'priority', 'name', default='Normal'),
        'category': safe_get(ticket, 'category', 'name', default='General'),
        'subcategory': safe_get(ticket, 'subcategory', 'name', default=''),
        'requester': safe_get(ticket, 'requester', 'name', default='Unknown'),
        'requester_email': safe_get(ticket, 'requester', 'email_id', default=''),
        'technician': safe_get(ticket, 'technician', 'name', default='Unassigned'),
        'technician_email': safe_get(ticket, 'technician', 'email_id', default=''),
        'created_time': safe_get(ticket, 'created_time', 'display_value', default='Unknown'),
        'created_time_epoch': safe_get(ticket, 'created_time', 'value', default=0),
        'due_by_time': safe_get(ticket, 'due_by_time', 'display_value', default=''),
        'resolved_time': safe_get(ticket, 'resolved_time', 'display_value', default=''),
        'closed_time': safe_get(ticket, 'closed_time', 'display_value', default=''),
        'last_updated_time': safe_get(ticket, 'last_updated_time', 'display_value', default=''),
        'group': safe_get(ticket, 'group', 'name', default='Unassigned'),
        'site': safe_get(ticket, 'site', 'name', default=''),
        'department': safe_get(ticket, 'department', 'name', default=''),
        'impact': safe_get(ticket, 'impact', 'name', default=''),
        'urgency': safe_get(ticket, 'urgency', 'name', default=''),
        'mode': safe_get(ticket, 'mode', 'name', default=''),
        'source': safe_get(ticket, 'request_type', 'name', default=''),
        'level': safe_get(ticket, 'level', 'name', default=''),
        'item': safe_get(ticket, 'item', 'name', default=''),
        'service': safe_get(ticket, 'service_category', 'name', default=''),
        'time_elapsed': safe_get(ticket, 'time_elapsed', default=''),
        'is_overdue': safe_get(ticket, 'is_overdue', default=False),
        'has_notes': safe_get(ticket, 'has_notes', default=False),
        'is_service_request': safe_get(ticket, 'is_service_request', default=False),
        'resolution': safe_get(ticket, 'resolution', default=''),
        'resolution_category': safe_get(ticket, 'resolution', 'name', default=''),
        'closure_code': safe_get(ticket, 'closure_code', 'name', default=''),
        'closure_comments': safe_get(ticket, 'closure_comments', default=''),
        'assets': safe_get(ticket, 'assets', default=[]),
        'linked_requests': safe_get(ticket, 'linked_requests', default=[]),
        'template': safe_get(ticket, 'template', 'name', default=''),
        'project': safe_get(ticket, 'project', 'name', default=''),
        'display_id_str': f"#{safe_get(ticket, 'display_id', default='Unknown')}",
        'id_long': safe_get(ticket, 'id', default=''),
        'is_escalated': safe_get(ticket, 'is_escalated', default=False),
        'maintenance': safe_get(ticket, 'maintenance', 'name', default=''),
        'approval_status': safe_get(ticket, 'approval_status', default=''),
        'request_type': safe_get(ticket, 'request_type', 'name', default=''),
        'created_by': safe_get(ticket, 'created_by', 'name', default=''),
        'cancellation_requested': safe_get(ticket, 'cancellation_requested', default=False),
    }
    
    if fields['is_service_request']:
        fields['module_type'] = 'service_request'
    else:
        fields['module_type'] = 'request'
    
    return fields

def calculate_complexity_score(ticket: Dict, comprehensive_fields: Dict, conversations: List[Dict]) -> int:
    """Calculate complexity score 0-10"""
    score = 0
    
    priority = comprehensive_fields.get('priority', '').lower()
    if 'urgent' in priority or 'critical' in priority:
        score += 3
    elif 'high' in priority:
        score += 2
    elif 'medium' in priority or 'normal' in priority:
        score += 1
    
    category = comprehensive_fields.get('category', '').lower()
    complex_categories = ['network', 'server', 'database', 'application', 'security', 'integration', 'infrastructure']
    simple_categories = ['password', 'access', 'account', 'printer', 'email', 'software installation']
    
    if any(cat in category for cat in complex_categories):
        score += 2
    elif any(cat in category for cat in simple_categories):
        score += 0
    else:
        score += 1
    
    description = comprehensive_fields.get('description', '')
    if len(description) > 500:
        score += 2
    elif len(description) > 200:
        score += 1
    
    conv_count = len(conversations)
    if conv_count > 10:
        score += 2
    elif conv_count > 5:
        score += 1
    
    return min(max(score, 0), 10)

def analyze_sentiment(ticket: Dict, comprehensive_fields: Dict, conversations: List[Dict]) -> str:
    """Analyze sentiment"""
    negative_keywords = ['urgent', 'critical', 'emergency', 'broken', 'not working', 'failed', 'error']
    positive_keywords = ['thank', 'thanks', 'appreciate', 'great', 'working', 'resolved', 'fixed']
    
    text = ' '.join([
        comprehensive_fields.get('subject', ''),
        comprehensive_fields.get('description', ''),
        comprehensive_fields.get('priority', ''),
    ]).lower()
    
    negative_count = sum(1 for keyword in negative_keywords if keyword in text)
    positive_count = sum(1 for keyword in positive_keywords if keyword in text)
    
    if negative_count > positive_count + 2:
        if comprehensive_fields.get('is_escalated') or comprehensive_fields.get('is_overdue'):
            return 'at-risk'
        return 'negative'
    elif positive_count > negative_count + 1:
        return 'positive'
    else:
        return 'neutral'

def predict_sla_risk(ticket: Dict, comprehensive_fields: Dict, complexity: int) -> str:
    """Predict SLA breach risk"""
    if comprehensive_fields.get('is_overdue'):
        return 'critical'
    
    if comprehensive_fields.get('status_category') == 'closed':
        return 'on-track'
    
    risk_score = 0
    
    priority = comprehensive_fields.get('priority', '').lower()
    if 'urgent' in priority or 'critical' in priority:
        risk_score += 3
    elif 'high' in priority:
        risk_score += 2
    
    if complexity >= 8:
        risk_score += 3
    elif complexity >= 6:
        risk_score += 2
    
    if risk_score >= 5:
        return 'critical'
    elif risk_score >= 3:
        return 'high'
    elif risk_score >= 1:
        return 'medium'
    else:
        return 'low'

def analyze_response_status(conversations: List[Dict], comprehensive_fields: Dict) -> Dict[str, Any]:
    """Analyze response status"""
    if not conversations:
        return {
            'awaiting_response': 'customer',
            'last_responder': 'system',
            'response_time_indicator': 'none',
            'has_unanswered_questions': False
        }
    
    last_conv = conversations[-1]
    last_sender = last_conv.get('sender', {}).get('name', '').lower()
    
    requester = comprehensive_fields.get('requester', '').lower()
    
    awaiting = 'technician' if requester in last_sender else 'customer'
    
    return {
        'awaiting_response': awaiting,
        'last_responder': last_sender,
        'response_time_indicator': 'normal',
        'has_unanswered_questions': False
    }

def auto_categorize_ticket(comprehensive_fields: Dict, conversations: List[Dict]) -> Dict[str, Any]:
    """Auto-categorize ticket"""
    return {
        'suggested_category': 'General Support',
        'confidence': 'medium',
        'match_score': 1
    }

def generate_comprehensive_tags(ticket: Dict, conversations: List[Dict], 
                                comprehensive_fields: Dict, complexity: int,
                                sentiment: str, sla_risk: str, 
                                response_status: Dict) -> List[str]:
    """
    Generate comprehensive tags - v3.1.10 ENHANCED
    """
    tags = []
    
    short_id = comprehensive_fields.get('ticket_id')
    tags.extend([
        f"ticket:{short_id}",
        f"#{short_id}",
        comprehensive_fields.get('display_id_str', '')
    ])
    
    module_type = comprehensive_fields.get('module_type', 'unknown')
    tags.append(f"module:{module_type}")
    
    # ============================================================
    # STATUS TAGS - v3.1.10 ENHANCED
    # ============================================================
    status = comprehensive_fields['status'].lower().replace(' ', '-')
    status_category = comprehensive_fields['status_category']
    
    tags.extend([
        f"status:{status}",
        f"status-category:{status_category}"
    ])
    
    if status_category == 'active':
        tags.extend([
            'ACTIVE-TICKET',
            'IS-OPEN',
            'NOT-CLOSED',
            'OPEN-STATUS',
            'active',
            'open-status',
            'needs-attention',
            'actionable-ticket',
            'requires-work'
        ])
        
        if 'open' in status:
            tags.extend(['STATUS-OPEN', 'CURRENTLY-OPEN'])
        if 'progress' in status or 'in-progress' in status:
            tags.extend(['STATUS-IN-PROGRESS', 'BEING-WORKED'])
        if 'pending' in status:
            tags.extend(['STATUS-PENDING', 'WAITING'])
        if 'hold' in status or 'on-hold' in status:
            tags.extend(['STATUS-ON-HOLD', 'PAUSED'])
            
    else:
        tags.extend([
            'CLOSED-TICKET',
            'IS-CLOSED',
            'NOT-ACTIVE',
            'completed',
            'resolved'
        ])
    # ============================================================
    
    priority = comprehensive_fields['priority'].lower()
    tags.append(f"priority:{priority}")
    if 'urgent' in priority:
        tags.extend(['urgent-attention', 'critical-action-required'])
    
    category = comprehensive_fields.get('category', 'general')
    if category and category != 'General':
        tags.append(f"category:{category.lower().replace(' ', '-')}")
    
    technician = comprehensive_fields.get('technician', 'Unassigned')
    if technician and technician.lower() not in ['unassigned', 'unknown', '']:
        tech_tag = technician.lower().replace(' ', '-').replace('.', '-')
        tags.extend([
            f"technician:{tech_tag}",
            f"assigned-to:{tech_tag}"
        ])
    else:
        tags.extend(['unassigned', 'needs-assignment'])
    
    requester = comprehensive_fields.get('requester', '')
    if requester:
        req_tag = requester.lower().replace(' ', '-').replace('.', '-')
        tags.extend([f"requester:{req_tag}"])
    
    tags.extend([
        f"complexity:{complexity}",
        f"sentiment:{sentiment}",
        f"sla-risk:{sla_risk}"
    ])
    
    if comprehensive_fields.get('is_overdue'):
        tags.extend(['overdue', 'past-due', 'sla-breached'])
    
    conv_count = len(conversations)
    if conv_count == 0:
        tags.append('no-conversation')
    elif conv_count > 10:
        tags.append('high-conversation-volume')
    
    awaiting = response_status.get('awaiting_response')
    tags.append(f"awaiting:{awaiting}")
    
    if comprehensive_fields.get('is_service_request'):
        tags.append('service-request')
    else:
        tags.append('request')
    
    tags = list(dict.fromkeys([tag.strip() for tag in tags if tag and tag.strip()]))
    return tags

def format_conversations_readable(conversations: List[Dict]) -> str:
    """Format conversations"""
    if not conversations:
        return "No conversations"
    
    formatted = []
    for i, conv in enumerate(conversations, 1):
        sender = conv.get('sender', {}).get('name', 'Unknown')
        time_val = conv.get('created_time', {}).get('display_value', 'Unknown')
        text = conv.get('text', 'No content')
        text = re.sub(r'<[^>]+>', '', text).strip() or 'No content'
        
        formatted.append(f"--- Conversation {i} ---\nFrom: {sender}\nTime: {time_val}\nMessage:\n{text}\n")
    
    return "\n".join(formatted)

def generate_ticket_document(ticket: Dict, conversations: List[Dict],
                            comprehensive_fields: Dict, complexity: int,
                            sentiment: str, sla_risk: str, 
                            response_status: Dict,
                            auto_category: Dict) -> Dict[str, Any]:
    """Generate complete document"""
    tags = generate_comprehensive_tags(
        ticket, conversations, comprehensive_fields,
        complexity, sentiment, sla_risk, response_status
    )
    
    readable_conversations = format_conversations_readable(conversations)
    
    document_parts = [
        f"TICKET #{comprehensive_fields['ticket_id']}",
        f"Status: {comprehensive_fields['status']} ({comprehensive_fields['status_category']})",
        f"Priority: {comprehensive_fields['priority']}",
        f"Category: {comprehensive_fields['category']}",
        "",
        f"SUBJECT: {comprehensive_fields['subject']}",
        "",
        "DESCRIPTION:",
        comprehensive_fields.get('description', 'No description'),
        "",
        f"REQUESTER: {comprehensive_fields['requester']}",
        f"TECHNICIAN: {comprehensive_fields['technician']}",
        "",
        f"CREATED: {comprehensive_fields['created_time']}",
        "",
        "=== AI INTELLIGENCE ===",
        f"Complexity: {complexity}/10",
        f"Sentiment: {sentiment}",
        f"SLA Risk: {sla_risk}",
        "",
        "=== CONVERSATIONS ===",
        readable_conversations,
        ""
    ]
    
    document_text = '\n'.join(document_parts)
    
    document = {
        'document_id': f"ticket-{comprehensive_fields['ticket_id']}",
        'timestamp': int(datetime.now(timezone.utc).timestamp() * 1000),
        'tags': tags,
        'parents': None,
        'source_url': f"{SDP_BASE_URL}/WorkOrder.do?woMode=viewWO&woID={comprehensive_fields.get('id_long')}",
        'text': document_text,
        'section': {
            'prefix': None,
            'content': document_text,
            'sections': []
        }
    }
    
    return document

def upload_to_dust(document: Dict) -> bool:
    """Upload document to Dust"""
    headers = {
        "Authorization": f"Bearer {DUST_API_KEY}",
        "Content-Type": "application/json"
    }
    
    url = f"{DUST_API_URL}/documents/{document['document_id']}"
    
    try:
        response = session.post(url, headers=headers, json=document, timeout=30)
        response.raise_for_status()
        return True
    except Exception:
        return False

def upload_documents_parallel(documents: List[Dict]) -> tuple:
    """Upload documents in parallel"""
    if not documents:
        return 0, 0
    
    successful = 0
    failed = 0
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS) as executor:
        future_to_doc = {
            executor.submit(upload_to_dust, doc): doc 
            for doc in documents
        }
        
        for future in as_completed(future_to_doc):
            if future.result():
                successful += 1
            else:
                failed += 1
    
    return successful, failed

def process_single_ticket(ticket: Dict, conversations_map: Dict[str, List[Dict]]) -> Optional[Dict]:
    """Process single ticket"""
    try:
        ticket_id = ticket.get('id')
        conversations = conversations_map.get(str(ticket_id), [])
        
        comprehensive_fields = extract_comprehensive_fields(ticket)
        complexity = calculate_complexity_score(ticket, comprehensive_fields, conversations)
        sentiment = analyze_sentiment(ticket, comprehensive_fields, conversations)
        sla_risk = predict_sla_risk(ticket, comprehensive_fields, complexity)
        response_status = analyze_response_status(conversations, comprehensive_fields)
        auto_category = auto_categorize_ticket(comprehensive_fields, conversations)
        
        document = generate_ticket_document(
            ticket, conversations, comprehensive_fields,
            complexity, sentiment, sla_risk,
            response_status, auto_category
        )
        
        METRICS['total_processed'] += 1
        
        if comprehensive_fields['is_service_request']:
            METRICS['service_requests'] += 1
        else:
            METRICS['requests'] += 1
        
        if comprehensive_fields['status_category'] == 'active':
            METRICS['active_tickets'] += 1
        
        return document
        
    except Exception as e:
        METRICS['skipped'] += 1
        return None

def process_ticket_batch(tickets: List[Dict], batch_num: int, total_tickets: int) -> tuple:
    """Process batch of tickets"""
    batch_start = (batch_num - 1) * BATCH_SIZE + 1
    batch_end = min(batch_start + len(tickets) - 1, total_tickets)
    
    logger.info(f"\n[Batch {batch_num}] Processing tickets {batch_start}-{batch_end}...")
    
    ticket_ids = [str(t.get('id')) for t in tickets]
    conversations_map = fetch_conversations_parallel(ticket_ids)
    
    documents = []
    for ticket in tickets:
        doc = process_single_ticket(ticket, conversations_map)
        if doc:
            documents.append(doc)
    
    if documents:
        successful, failed = upload_documents_parallel(documents)
        logger.info(f"  ✓ Batch complete: {successful} uploaded, {failed} failed")
        return documents, successful, failed
    
    return [], 0, 0

def process_all_tickets(tickets: List[Dict]) -> Dict[str, int]:
    """Process all tickets in batches"""
    total_tickets = len(tickets)
    logger.info(f"\n📋 Processing {total_tickets} tickets in batches of {BATCH_SIZE}...")
    
    stats = {'total': total_tickets, 'successful': 0, 'failed': 0, 'skipped': 0}
    
    for i in range(0, total_tickets, BATCH_SIZE):
        batch = tickets[i:i+BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        
        documents, successful, failed = process_ticket_batch(batch, batch_num, total_tickets)
        
        stats['successful'] += successful
        stats['failed'] += failed
    
    stats['skipped'] = METRICS['skipped']
    logger.info(f"\nn✓ Processing complete: {stats['successful']} successful, {stats['failed']} failed, {stats['skipped']} skipped\n")
    
    return stats

# ============================================================
# MAIN
# ============================================================

def main():
    """Main execution"""
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 60)
    logger.info("✨ v3.1.10: Enhanced tagging for open ticket search")
    logger.info("")
    
    try:
        # Environment variables already validated during import
        
        # Initialize token manager
        token_manager.get_token()
        
        checkpoint = load_checkpoint()
        last_sync_time = load_last_sync_time()
        
        if checkpoint and last_sync_time:
            logger.info(f"📍 Last sync: {last_sync_time}")
            logger.info(f"📊 INCREMENTAL SYNC")
        else:
            logger.info("📊 FULL SYNC (no checkpoint)")
            last_sync_time = None
        
        if should_run_cleanup():
            cleanup_dust_data_source()
        else:
            logger.info("✓ Skipping cleanup\n")
        
        sync_start_time = datetime.now(timezone.utc)
        
        tickets = fetch_all_tickets(max_pages=MAX_PAGES, last_sync_time=last_sync_time)
        
        if not tickets:
            logger.info("✓ No tickets to process")
            return 0
        
        logger.info(f"✓ Fetched {len(tickets)} tickets\n")
        
        stats = process_all_tickets(tickets)
        
        save_last_sync_time(sync_start_time)
        save_checkpoint(sync_start_time.isoformat(), METRICS)
        
        logger.info("=" * 60)
        logger.info(f"✓ SYNC COMPLETE (v{VERSION})")
        logger.info("=" * 60)
        logger.info(f"  Total Processed: {METRICS['total_processed']}")
        logger.info(f"  Active Tickets: {METRICS['active_tickets']}")
        logger.info(f"  Service Requests: {METRICS['service_requests']}")
        logger.info(f"  Requests: {METRICS['requests']}")
        logger.info(f"  Skipped: {METRICS['skipped']}")
        logger.info("=" * 60)
        
        return 0
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Sync interrupted")
        return 130
    
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
