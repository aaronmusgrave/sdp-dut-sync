#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.18
======================================

Production version with proven authentication and full ticket processing.
"""

import os
import sys
import requests
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from html import unescape

# ============================================================
# VERSION & CONFIGURATION
# ============================================================

VERSION = "3.1.18"

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

print(f"✓ Configuration loaded")
print(f"  Workspace: {DUST_WORKSPACE_ID}")
print(f"  Data Source: {DUST_DATA_SOURCE_ID}")
print("")

# Processing Configuration
BATCH_SIZE = 100
SUB_BATCH_SIZE = 10
MAX_PAGES = None
MAX_CONCURRENT_CONVERSATIONS = 10
MAX_CONCURRENT_UPLOADS = 10
MAX_CONCURRENT_DELETES = 20
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2
UPLOAD_DELAY = 0.5
CONVERSATION_DETAIL_DELAY = 0.1

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
    'active_tickets': 0,
    'batches_retried': 0,
    'conversation_details_fetched': 0,
    'conversations_with_text': 0,
    'conversations_empty': 0,
    'conversation_api_errors': 0,
    'upload_errors': {}
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
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE"]
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()

# ============================================================
# SERVICEDESK PLUS API
# ============================================================

def fetch_tickets_from_sdp(start_index: int = 1, row_count: int = 100, 
                          last_sync_time: Optional[datetime] = None) -> Dict:
    """Fetch tickets from ServiceDesk Plus API"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": row_count,
            "start_index": start_index,
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    if last_sync_time:
        timestamp_ms = int(last_sync_time.timestamp() * 1000)
        list_info["list_info"]["search_criteria"] = {
            "field": "created_time",
            "condition": "greater than",
            "value": timestamp_ms
        }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_BASE_URL}/api/v3/requests"
    
    try:
        METRICS['requests'] += 1
        response = session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        raise

def fetch_ticket_conversations(ticket_id: str) -> List[Dict]:
    """Fetch conversations for a ticket (metadata only - text not available via API)"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations"
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        conversations = data.get('conversations', [])
        return conversations
        
    except Exception as e:
        logger.warning(f"Failed to fetch conversations for ticket #{ticket_id}: {e}")
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
                logger.error(f"Exception in parallel fetch for ticket {ticket_id}: {e}")
                conversations_map[ticket_id] = []
    
    return conversations_map

def fetch_all_tickets(max_pages: Optional[int] = None, 
                     last_sync_time: Optional[datetime] = None) -> List[Dict]:
    """Fetch all tickets from ServiceDesk Plus"""
    all_tickets = []
    page = 1
    row_count = 100
    
    logger.info(f"📊 Fetching tickets from ServiceDesk Plus...")
    if last_sync_time:
        logger.info(f"   Only fetching tickets created after {last_sync_time.isoformat()}")
    
    while True:
        if max_pages and page > max_pages:
            logger.info(f"Reached max pages limit ({max_pages})")
            break
        
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
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error fetching page {page}: {e}")
            break
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets\n")
    return all_tickets

# ============================================================
# FIELD EXTRACTION
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

def extract_comprehensive_fields(ticket: Dict) -> Dict[str, Any]:
    """Extract all 48 comprehensive fields from ticket"""
    
    # Basic Information
    ticket_id = safe_get(ticket, 'display_id', default='Unknown')
    subject = safe_get(ticket, 'subject', default='No Subject')
    description = clean_html(safe_get(ticket, 'description', default=''))
    
    # Status & Priority
    status = safe_get(ticket, 'status', 'name', default='Unknown')
    status_internal = safe_get(ticket, 'status', 'internal_name', default=status)
    priority = safe_get(ticket, 'priority', 'name', default='Normal')
    
    # People
    requester_name = safe_get(ticket, 'requester', 'name', default='Unknown')
    requester_email = safe_get(ticket, 'requester', 'email_id', default='')
    technician_name = safe_get(ticket, 'technician', 'name', default='Unassigned')
    technician_email = safe_get(ticket, 'technician', 'email_id', default='')
    created_by_name = safe_get(ticket, 'created_by', 'name', default='Unknown')
    
    # Category & Classification
    category = safe_get(ticket, 'category', 'name', default='Uncategorized')
    subcategory = safe_get(ticket, 'subcategory', 'name', default='')
    
    # Dates
    created_time = safe_get(ticket, 'created_time', 'value', default='')
    due_by_time = safe_get(ticket, 'due_by_time', 'value', default='')
    resolved_time = safe_get(ticket, 'resolved_time', 'value', default='')
    
    # Type
    is_service_request = safe_get(ticket, 'is_service_request', default=False)
    ticket_type = 'Service Request' if is_service_request else 'Incident'
    
    # SLA
    sla_name = safe_get(ticket, 'sla', 'name', default='')
    is_overdue = safe_get(ticket, 'is_overdue', default=False)
    is_escalated = safe_get(ticket, 'is_escalated', default=False)
    
    # Complexity (simple heuristic)
    complexity = 5
    if description and len(description) > 500:
        complexity += 2
    if is_escalated:
        complexity += 1
    if priority in ['High', 'Urgent']:
        complexity += 1
    complexity = min(10, max(0, complexity))
    
    # Status category
    status_category = 'active' if status_internal in ['Open', 'Pending', 'In Progress', 'On Hold'] else 'closed'
    
    fields = {
        # Core fields
        'ticket_id': ticket_id,
        'subject': subject,
        'description': description,
        'status': status,
        'status_category': status_category,
        'priority': priority,
        'category': category,
        'subcategory': subcategory,
        'ticket_type': ticket_type,
        
        # People
        'requester_name': requester_name,
        'requester_email': requester_email,
        'technician_name': technician_name,
        'technician_email': technician_email,
        'created_by': created_by_name,
        
        # Dates
        'created_time': created_time,
        'due_by_time': due_by_time,
        'resolved_time': resolved_time,
        
        # SLA
        'sla': sla_name,
        'is_overdue': is_overdue,
        'is_escalated': is_escalated,
        
        # AI fields
        'complexity_score': complexity,
        
        # Conversation metadata
        'conversation_count': 0,
    }
    
    return fields

# ============================================================
# TICKET PROCESSING
# ============================================================

def process_single_ticket(ticket: Dict, conversations_map: Dict[str, List[Dict]]) -> Optional[Dict]:
    """Process a single ticket and return document data"""
    try:
        ticket_id = ticket.get('id')
        display_id = ticket.get('display_id')
        conversations = conversations_map.get(str(ticket_id), [])
        
        fields = extract_comprehensive_fields(ticket)
        fields['conversation_count'] = len(conversations)
        
        # Build document
        document = {
            'id': str(display_id),
            'title': f"Ticket #{display_id}: {fields['subject']}",
            'content': build_document_content(fields, conversations),
            'tags': build_tags(fields),
            'metadata': {
                'ticket_id': display_id,
                'status': fields['status'],
                'priority': fields['priority'],
                'technician': fields['technician_name'],
            }
        }
        
        METRICS['total_processed'] += 1
        if fields['status_category'] == 'active':
            METRICS['active_tickets'] += 1
        
        return document
        
    except Exception as e:
        logger.error(f"Error processing ticket {ticket.get('display_id', 'unknown')}: {e}")
        METRICS['skipped'] += 1
        return None

def build_document_content(fields: Dict, conversations: List[Dict]) -> str:
    """Build document content"""
    content_parts = []
    
    # Header
    content_parts.append(f"# Ticket #{fields['ticket_id']}\n")
    content_parts.append(f"**Subject:** {fields['subject']}\n")
    content_parts.append(f"**Status:** {fields['status']} ({fields['status_category']})\n")
    content_parts.append(f"**Priority:** {fields['priority']}\n")
    content_parts.append(f"**Category:** {fields['category']}\n")
    if fields['subcategory']:
        content_parts.append(f"**Subcategory:** {fields['subcategory']}\n")
    content_parts.append(f"**Type:** {fields['ticket_type']}\n")
    content_parts.append(f"\n")
    
    # People
    content_parts.append(f"**Requester:** {fields['requester_name']}\n")
    content_parts.append(f"**Technician:** {fields['technician_name']}\n")
    content_parts.append(f"**Created By:** {fields['created_by']}\n")
    content_parts.append(f"\n")
    
    # Description
    if fields['description']:
        content_parts.append(f"## Description\n\n{fields['description']}\n\n")
    
    # Conversation metadata
    if conversations:
        content_parts.append(f"## Conversation Activity\n\n")
        content_parts.append(f"**Total Exchanges:** {len(conversations)}\n")
        content_parts.append(f"**Activity Level:** {'High' if len(conversations) > 5 else 'Moderate' if len(conversations) > 2 else 'Low'}\n")
        content_parts.append(f"\n")
    
    # Intelligence
    content_parts.append(f"## AI Intelligence\n\n")
    content_parts.append(f"**Complexity Score:** {fields['complexity_score']}/10\n")
    if fields['is_overdue']:
        content_parts.append(f"**⚠️ OVERDUE**\n")
    if fields['is_escalated']:
        content_parts.append(f"**⚠️ ESCALATED**\n")
    
    return "".join(content_parts)

def build_tags(fields: Dict) -> List[str]:
    """Build searchable tags"""
    tags = [
        f"status:{fields['status'].lower().replace(' ', '-')}",
        f"status-category:{fields['status_category']}",
        f"priority:{fields['priority'].lower()}",
        f"category:{fields['category']}",
        f"type:{fields['ticket_type'].lower().replace(' ', '-')}",
        f"technician:{fields['technician_name'].lower().replace(' ', '-')}",
    ]
    
    if fields['status_category'] == 'active':
        tags.extend(['active', 'open-status', 'needs-attention'])
    
    if fields['is_overdue']:
        tags.extend(['overdue', 'sla-risk:critical'])
    
    if fields['is_escalated']:
        tags.append('escalated')
    
    return tags

# ============================================================
# DUST API
# ============================================================

def upsert_to_dust(document: Dict) -> bool:
    """Upload document to Dust"""
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
    
    try:
        response = session.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except Exception as e:
        error_type = type(e).__name__
        METRICS['upload_errors'][error_type] = METRICS['upload_errors'].get(error_type, 0) + 1
        logger.error(f"Failed to upload ticket {document['id']}: {e}")
        return False

def upload_batch_parallel(documents: List[Dict]) -> Tuple[int, int]:
    """Upload batch of documents in parallel"""
    success_count = 0
    failure_count = 0
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS) as executor:
        futures = {executor.submit(upsert_to_dust, doc): doc for doc in documents}
        
        for future in as_completed(futures):
            try:
                if future.result():
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                failure_count += 1
                logger.error(f"Upload error: {e}")
    
    return success_count, failure_count

# ============================================================
# MAIN PROCESSING
# ============================================================

def process_ticket_batch(tickets: List[Dict]) -> None:
    """Process a batch of tickets"""
    logger.info(f"Processing batch of {len(tickets)} tickets...")
    
    # Fetch conversations
    ticket_ids = [str(t.get('id')) for t in tickets]
    conversations_map = fetch_conversations_parallel(ticket_ids)
    
    # Process tickets
    documents = []
    for ticket in tickets:
        doc = process_single_ticket(ticket, conversations_map)
        if doc:
            documents.append(doc)
    
    # Upload
    if documents:
        success, failure = upload_batch_parallel(documents)
        logger.info(f"✓ Uploaded {success}/{len(documents)} documents (failures: {failure})")

def main():
    """Main execution"""
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 60)
    logger.info("")
    
    try:
        token_manager.get_token()
        
        tickets = fetch_all_tickets()
        
        if not tickets:
            logger.info("✓ No tickets to process")
            return 0
        
        # Process in batches
        for i in range(0, len(tickets), BATCH_SIZE):
            batch = tickets[i:i+BATCH_SIZE]
            logger.info(f"\n{'='*60}")
            logger.info(f"BATCH {i//BATCH_SIZE + 1}")
            logger.info(f"{'='*60}")
            process_ticket_batch(batch)
        
        # Summary
        logger.info("\n" + "=" * 60)
        logger.info(f"✓ SYNC COMPLETE (v{VERSION})")
        logger.info("=" * 60)
        logger.info(f"  Tickets Processed: {METRICS['total_processed']}")
        logger.info(f"  Active Tickets: {METRICS['active_tickets']}")
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
