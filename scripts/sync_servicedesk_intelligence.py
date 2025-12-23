#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.2.2-PRODUCTION-FIXED
Dual Data Source + Token Retry Logic + Rate Limit Handling + Secret Cleaning

Changes in v3.2.2-FIXED:
- Re-added secret cleaning (strip quotes, backslashes, whitespace)
- All other v3.2.2 features intact

Changes in v3.2.2:
- Added 401 error retry logic with automatic token refresh
- Proactive token refresh (when < 10 minutes remaining)
- Rate limit handling (delays between conversation fetches)
- Enhanced error tracking and recovery
- Improved logging for troubleshooting

Architecture:
- servicedesk: All tickets (active + closed) with UPSERT updates
- servicedesk-active: Only active tickets, completely rebuilt each sync
"""

import os
import sys
import json
import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIGURATION
# ============================================================
def clean_secret(value: str) -> str:
    """
    Remove quotes, backslashes, and whitespace from secrets.
    Handles common secret format issues from GitHub Actions.
    """
    if not value:
        return ''
    # Strip whitespace, then quotes, then backslashes, then whitespace again
    cleaned = value.strip()
    cleaned = cleaned.strip('"').strip("'")
    cleaned = cleaned.replace('\\', '')
    cleaned = cleaned.strip()
    return cleaned

# Load and clean all secrets
DUST_API_KEY = clean_secret(os.getenv('DUST_API_KEY', ''))
DUST_WORKSPACE_ID = clean_secret(os.getenv('DUST_WORKSPACE_ID', ''))
DUST_MAIN_DATA_SOURCE = 'servicedesk'          # All tickets
DUST_ACTIVE_DATA_SOURCE = 'servicedesk-active'  # Active only

SDP_DOMAIN = clean_secret(os.getenv('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_secret(os.getenv('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_secret(os.getenv('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_secret(os.getenv('SDP_REFRESH_TOKEN', ''))

CHECKPOINT_FILE = 'sync_checkpoint.json'
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 3.0
CONVERSATION_FETCH_DELAY = 0.3  # Delay every 10 conversations

# Version
VERSION = "v3.2.2-PRODUCTION-FIXED"

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
# GLOBAL STATE
# ============================================================
TOKEN = None
TOKEN_EXPIRY = None
CONVERSATION_401_ERRORS = 0
MAX_401_BEFORE_REFRESH = 5

# Statistics
stats = {
    'tickets_fetched': 0,
    'tickets_processed': 0,
    'main_uploaded': 0,
    'active_uploaded': 0,
    'main_deleted': 0,
    'active_deleted': 0,
    'new_tickets': 0,
    'updated_tickets': 0,
    'conversations_fetched': 0,
    'upload_failures': 0,
    'retries': 0,
    'rate_limited': 0,
    'api_calls': 0,
    'active_count': 0,
    'closed_count': 0,
    'service_requests': 0,
    'incidents': 0,
    'total_exchanges': 0
}

# ============================================================
# TOKEN MANAGEMENT
# ============================================================
def refresh_token():
    """Refresh OAuth token and update expiry."""
    global TOKEN, TOKEN_EXPIRY, CONVERSATION_401_ERRORS
    
    logger.info("🔑 Fetching new token...")
    
    url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        "refresh_token": SDP_REFRESH_TOKEN,
        "client_id": SDP_CLIENT_ID,
        "client_secret": SDP_CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    
    try:
        response = requests.post(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        TOKEN = data.get('access_token')
        expires_in = data.get('expires_in', 3600)
        TOKEN_EXPIRY = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        
        # Reset 401 error counter on successful refresh
        CONVERSATION_401_ERRORS = 0
        
        logger.info(f"✓ Token refreshed successfully (expires in {expires_in // 60} minutes)")
        
    except Exception as e:
        logger.error(f"Failed to refresh token: {e}")
        raise

def ensure_token_valid():
    """Ensure token is valid, refresh if expired or expiring soon."""
    global TOKEN, TOKEN_EXPIRY
    
    # Check if token is expired or expiring in next 10 minutes
    if not TOKEN or not TOKEN_EXPIRY:
        refresh_token()
    else:
        time_remaining = (TOKEN_EXPIRY - datetime.now(timezone.utc)).total_seconds()
        if time_remaining < 600:  # Less than 10 minutes
            logger.info(f"Token expiring in {int(time_remaining/60)} minutes - refreshing proactively...")
            refresh_token()

# ============================================================
# CHECKPOINT MANAGEMENT
# ============================================================
def load_checkpoint() -> Optional[datetime]:
    """Load last sync timestamp from checkpoint file."""
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                data = json.load(f)
                timestamp_str = data.get('last_sync_timestamp')
                if timestamp_str:
                    last_sync = datetime.fromisoformat(timestamp_str)
                    logger.info(f"  Checkpoint found: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
                    return last_sync
        except Exception as e:
            logger.warning(f"Failed to load checkpoint: {e}")
    
    logger.info("  No checkpoint found - will perform full sync")
    return None

def save_checkpoint(timestamp: datetime):
    """Save sync timestamp to checkpoint file."""
    try:
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump({
                'last_sync_timestamp': timestamp.isoformat(),
                'version': VERSION
            }, f, indent=2)
        logger.info(f"  ✓ Checkpoint saved: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        logger.warning(f"Failed to save checkpoint: {e}")

# ============================================================
# DUST API - DOCUMENT MANAGEMENT
# ============================================================
def fetch_all_documents_from_dust(data_source_id: str) -> List[str]:
    """Fetch all document IDs from a Dust data source."""
    logger.info(f"  Fetching existing documents from {data_source_id}...")
    
    all_doc_ids = []
    offset = 0
    limit = 1000
    
    while True:
        url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{data_source_id}/documents"
        params = {"limit": limit, "offset": offset}
        headers = {"Authorization": f"Bearer {DUST_API_KEY}"}
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            stats['api_calls'] += 1
            
            data = response.json()
            documents = data.get('documents', [])
            
            if not documents:
                break
            
            doc_ids = [doc['document_id'] for doc in documents]
            all_doc_ids.extend(doc_ids)
            
            logger.info(f"    Fetched {len(documents)} documents (total: {len(all_doc_ids)})")
            
            if len(documents) < limit:
                break
            
            offset += limit
            
        except Exception as e:
            logger.error(f"Failed to fetch documents from {data_source_id}: {e}")
            break
    
    logger.info(f"  ✓ Found {len(all_doc_ids)} documents in {data_source_id}")
    return all_doc_ids

def delete_documents_from_dust(data_source_id: str, document_ids: List[str]):
    """Delete documents from Dust data source."""
    if not document_ids:
        logger.info(f"  No documents to delete from {data_source_id}")
        return
    
    logger.info(f"  Deleting {len(document_ids)} existing documents from {data_source_id}...")
    
    deleted_count = 0
    for doc_id in document_ids:
        url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{data_source_id}/documents/{doc_id}"
        headers = {"Authorization": f"Bearer {DUST_API_KEY}"}
        
        try:
            response = requests.delete(url, headers=headers, timeout=30)
            response.raise_for_status()
            stats['api_calls'] += 1
            deleted_count += 1
            
            if deleted_count % 100 == 0:
                logger.info(f"    Deleted {deleted_count}/{len(document_ids)} documents...")
            
        except Exception as e:
            logger.warning(f"Failed to delete document {doc_id}: {e}")
    
    logger.info(f"  ✓ Deleted {deleted_count}/{len(document_ids)} documents from {data_source_id}")
    return deleted_count

def upload_document_to_dust(data_source_id: str, doc_id: str, content: dict, tags: list) -> bool:
    """Upload a single document to Dust."""
    url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{data_source_id}/documents/{doc_id}"
    headers = {
        "Authorization": f"Bearer {DUST_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Prepare text content
    text_content = f"""
Ticket ID: {content.get('ticket_id', 'N/A')}
Subject: {content.get('subject', 'N/A')}
Status: {content.get('status', 'N/A')}
Priority: {content.get('priority', 'N/A')}
Technician: {content.get('technician', 'Unassigned')}
Requester: {content.get('requester', 'N/A')}
Category: {content.get('category', 'N/A')}
Description: {content.get('description', 'N/A')}
Resolution: {content.get('resolution', 'N/A')}
"""
    
    payload = {
        "text": text_content,
        "source_url": content.get('source_url'),
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "tags": tags,
        "parents": None,
        "light_document_output": True,
        "upsert": True
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        stats['api_calls'] += 1
        return True
    except Exception as e:
        logger.warning(f"Failed to upload document {doc_id} to {data_source_id}: {e}")
        stats['upload_failures'] += 1
        return False

# ============================================================
# SERVICEDESK API - DATA FETCHING
# ============================================================
def fetch_conversations(ticket_id: str, retry_count: int = 0) -> dict:
    """
    Fetch conversation metadata for a ticket with retry logic.
    Automatically refreshes token and retries on 401 errors.
    """
    global TOKEN, TOKEN_EXPIRY, CONVERSATION_401_ERRORS
    
    # Check if we need to refresh token due to repeated 401s
    if CONVERSATION_401_ERRORS >= MAX_401_BEFORE_REFRESH:
        logger.warning(f"   🔑 {CONVERSATION_401_ERRORS} consecutive 401 errors - forcing token refresh...")
        refresh_token()
        CONVERSATION_401_ERRORS = 0
    
    url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/conversations"
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        stats['api_calls'] += 1
        
        # Success - reset 401 counter
        if CONVERSATION_401_ERRORS > 0:
            logger.info(f"   ✓ Recovered from 401 errors")
            CONVERSATION_401_ERRORS = 0
        
        data = response.json()
        conversations = data.get('conversations', [])
        
        # Count total exchanges
        total_exchanges = 0
        for conv in conversations:
            replies = conv.get('replies', [])
            total_exchanges += len(replies)
        
        return {
            "exchanges": conversations,
            "count": total_exchanges
        }
        
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            CONVERSATION_401_ERRORS += 1
            
            # Retry with token refresh (max 2 retries)
            if retry_count < 2:
                logger.warning(f"   ⚠️  401 error for ticket {ticket_id} (attempt {retry_count + 1}/2)")
                refresh_token()
                time.sleep(2)  # Brief pause after refresh
                return fetch_conversations(ticket_id, retry_count + 1)
            else:
                logger.warning(f"   ❌ Failed after 2 retries for ticket {ticket_id}: {e}")
                return {"exchanges": [], "count": 0}
        else:
            logger.warning(f"Failed to fetch conversations for ticket {ticket_id}: {e}")
            return {"exchanges": [], "count": 0}
            
    except Exception as e:
        logger.warning(f"Failed to fetch conversations for ticket {ticket_id}: {e}")
        return {"exchanges": [], "count": 0}

def fetch_tickets(last_sync: Optional[datetime] = None) -> List[dict]:
    """Fetch tickets from ServiceDesk Plus API."""
    ensure_token_valid()
    
    all_tickets = []
    page = 1
    
    # Build query based on sync mode
    if last_sync:
        # Incremental: fetch tickets modified since last sync
        last_sync_ms = int(last_sync.timestamp() * 1000)
        logger.info(f"📊 Fetching tickets updated since {last_sync.strftime('%Y-%m-%d %H:%M:%S')}...")
        list_info = json.dumps({
            "row_count": 100,
            "start_index": 1,
            "search_criteria": {
                "field": "last_updated_time",
                "condition": "greater than",
                "value": str(last_sync_ms)
            }
        })
    else:
        # Full sync: fetch all tickets
        logger.info(f"📊 Fetching all tickets (full sync)...")
        list_info = json.dumps({
            "row_count": 100,
            "start_index": 1
        })
    
    while True:
        url = f"https://{SDP_DOMAIN}/api/v3/requests"
        headers = {
            "Authorization": f"Zoho-oauthtoken {TOKEN}",
            "Content-Type": "application/json"
        }
        
        # Update page number in list_info
        list_info_dict = json.loads(list_info)
        list_info_dict['start_index'] = (page - 1) * 100 + 1
        
        params = {"input_data": json.dumps(list_info_dict)}
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            stats['api_calls'] += 1
            
            data = response.json()
            requests_data = data.get('requests', [])
            
            if not requests_data:
                logger.info(f"  Page {page}: Last page reached")
                break
            
            all_tickets.extend(requests_data)
            logger.info(f"  Page {page}: {len(requests_data)} tickets (total: {len(all_tickets)})")
            
            # Check if we got less than requested (last page)
            if len(requests_data) < 100:
                logger.info(f"  Page {page}: Last page reached")
                break
            
            page += 1
            
        except Exception as e:
            logger.error(f"Failed to fetch tickets on page {page}: {e}")
            break
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets total")
    stats['tickets_fetched'] = len(all_tickets)
    
    # Determine new vs updated
    if last_sync:
        new_count = 0
        updated_count = 0
        for ticket in all_tickets:
            created_time = ticket.get('created_time', {}).get('value', 0)
            if created_time > int(last_sync.timestamp() * 1000):
                new_count += 1
            else:
                updated_count += 1
        
        logger.info(f"  New tickets: {new_count}")
        logger.info(f"  Updated tickets: {updated_count}")
        stats['new_tickets'] = new_count
        stats['updated_tickets'] = updated_count
    
    return all_tickets

# ============================================================
# TICKET PROCESSING
# ============================================================
def determine_status_category(status: str, internal_status: str) -> Tuple[str, str]:
    """Determine if ticket is active or closed."""
    status_lower = status.lower()
    internal_lower = internal_status.lower()
    
    # Active keywords
    active_keywords = ['open', 'in progress', 'on hold', 'pending', 'waiting', 'assigned']
    # Closed keywords
    closed_keywords = ['closed', 'resolved', 'completed', 'cancelled', 'canceled']
    
    # Check status first
    for keyword in active_keywords:
        if keyword in status_lower:
            return 'active', f'matched active keyword: {keyword}'
    
    for keyword in closed_keywords:
        if keyword in status_lower:
            return 'closed', f'matched closed keyword: {keyword}'
    
    # Check internal status
    for keyword in active_keywords:
        if keyword in internal_lower:
            return 'active', f'matched active keyword in internal: {keyword}'
    
    for keyword in closed_keywords:
        if keyword in internal_lower:
            return 'closed', f'matched closed keyword in internal: {keyword}'
    
    # Default to closed if uncertain
    logger.warning(f"Could not determine status category for status='{status}', internal='{internal_status}' - defaulting to closed")
    return 'closed', 'defaulted to closed (uncertain)'

def process_ticket(ticket: dict, conversation_data: dict) -> Tuple[dict, list]:
    """Process a single ticket into Dust document format."""
    
    # Extract basic info
    ticket_id = str(ticket.get('id', ''))
    subject = ticket.get('subject', 'No Subject')
    
    # Status determination
    status_obj = ticket.get('status', {})
    status = status_obj.get('name', 'Unknown')
    internal_status = status_obj.get('internal_name', status)
    status_category, status_reason = determine_status_category(status, internal_status)
    
    # Timestamps
    created_time = ticket.get('created_time', {}).get('display_value', 'Unknown')
    due_by_time = ticket.get('due_by_time', {}).get('display_value', 'Not set')
    resolved_time = ticket.get('resolved_time', {}).get('display_value', 'Not resolved')
    closed_time = ticket.get('closed_time', {}).get('display_value', 'Not closed')
    
    # People
    technician_obj = ticket.get('technician', {})
    technician_name = technician_obj.get('name', 'Unassigned')
    technician_email = technician_obj.get('email_id', '')
    
    requester_obj = ticket.get('requester', {})
    requester_name = requester_obj.get('name', 'Unknown')
    requester_email = requester_obj.get('email_id', '')
    
    # Classification
    priority = ticket.get('priority', {}).get('name', 'Normal')
    category = ticket.get('category', {}).get('name', 'Uncategorized')
    subcategory = ticket.get('subcategory', {}).get('name', '')
    request_type = ticket.get('request_type', {}).get('name', 'Service Request')
    
    # Description and resolution
    description = ticket.get('description', 'No description')
    resolution = ticket.get('resolution', {}).get('content', '')
    
    # Conversation data
    conversation_count = conversation_data.get('count', 0)
    
    # Build tags
    tags = [
        f"ticket-id:{ticket_id}",
        f"status:{status}",
        f"status-category:{status_category}",
        f"priority:{priority}",
        f"category:{category}",
        f"request-type:{request_type}",
        f"technician:{technician_name.lower().replace(' ', '-')}",
        f"requester:{requester_name.lower().replace(' ', '-')}",
        f"conversations:{conversation_count}"
    ]
    
    if status_category == 'closed':
        tags.append("CLOSED-TICKET")
    else:
        tags.append("ACTIVE-TICKET")
        tags.append("IS-OPEN")
        tags.append("NOT-CLOSED")
    
    # Build content
    content = {
        "ticket_id": ticket_id,
        "subject": subject,
        "status": status,
        "status_category": status_category,
        "priority": priority,
        "category": category,
        "subcategory": subcategory,
        "request_type": request_type,
        "technician": technician_name,
        "technician_email": technician_email,
        "requester": requester_name,
        "requester_email": requester_email,
        "description": description,
        "resolution": resolution,
        "created_time": created_time,
        "due_by_time": due_by_time,
        "resolved_time": resolved_time,
        "closed_time": closed_time,
        "conversation_count": conversation_count,
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/requests/{ticket_id}"
    }
    
    # Track statistics
    if status_category == 'active':
        stats['active_count'] += 1
    else:
        stats['closed_count'] += 1
    
    if request_type == 'Incident':
        stats['incidents'] += 1
    else:
        stats['service_requests'] += 1
    
    stats['total_exchanges'] += conversation_count
    
    return content, tags

def process_batch(batch_tickets: List[dict], batch_num: int, total_batches: int, is_full_sync: bool):
    """Process a batch of tickets."""
    
    logger.info(f"\n{'=' * 60}")
    logger.info(f"BATCH {batch_num}/{total_batches} ({len(batch_tickets)} tickets)")
    logger.info(f"{'=' * 60}")
    
    # Proactive token check
    ensure_token_valid()
    
    # Fetch conversations with rate limit handling
    logger.info("  Fetching conversation metadata...")
    conversations = {}
    for i, ticket in enumerate(batch_tickets):
        ticket_id = str(ticket.get('id', ''))
        conv_data = fetch_conversations(ticket_id)
        conversations[ticket_id] = conv_data
        
        # Add small delay every 10 tickets to avoid rate limits
        if (i + 1) % 10 == 0 and i + 1 < len(batch_tickets):
            time.sleep(CONVERSATION_FETCH_DELAY)
    
    logger.info(f"  ✓ Fetched conversations for {len(batch_tickets)} tickets")
    stats['conversations_fetched'] += len(batch_tickets)
    
    # Process tickets
    logger.info("  Processing tickets...")
    documents = []
    active_documents = []
    
    for ticket in batch_tickets:
        ticket_id = str(ticket.get('id', ''))
        conv_data = conversations.get(ticket_id, {"exchanges": [], "count": 0})
        
        content, tags = process_ticket(ticket, conv_data)
        doc_id = f"ticket-{ticket_id}"
        
        documents.append((doc_id, content, tags))
        
        # Add to active documents if status_category is active
        if content.get('status_category') == 'active':
            active_documents.append((doc_id, content, tags))
    
    logger.info(f"  ✓ Processed {len(documents)} documents")
    stats['tickets_processed'] += len(documents)
    
    # Upload to BOTH data sources
    logger.info("  Uploading to BOTH data sources...")
    
    # Upload to MAIN source (always)
    main_success = 0
    for doc_id, content, tags in documents:
        if upload_document_to_dust(DUST_MAIN_DATA_SOURCE, doc_id, content, tags):
            main_success += 1
    
    logger.info(f"  📦 Main source: {main_success}/{len(documents)} uploaded")
    stats['main_uploaded'] += main_success
    
    # Upload to ACTIVE source (only active tickets)
    if active_documents:
        active_success = 0
        for doc_id, content, tags in active_documents:
            if upload_document_to_dust(DUST_ACTIVE_DATA_SOURCE, doc_id, content, tags):
                active_success += 1
        
        logger.info(f"  ⚡ Active source: {active_success}/{len(active_documents)} uploaded")
        stats['active_uploaded'] += active_success
    
    # Pause before next batch
    if batch_num < total_batches:
        logger.info(f"  ⏸️  Pausing {BATCH_PAUSE_SECONDS}s before next batch...")
        time.sleep(BATCH_PAUSE_SECONDS)

# ============================================================
# MAIN SYNC LOGIC
# ============================================================
def run_sync():
    """Main sync orchestration."""
    
    sync_start_time = datetime.now(timezone.utc)
    
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 60)
    logger.info(f"Sync starting at {sync_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")
    
    # Initialize token
    refresh_token()
    
    # Load checkpoint
    last_sync = load_checkpoint()
    
    # Check for forced full sync
    force_full_sync = os.getenv('FORCE_FULL_SYNC', '').lower() in ['true', '1', 'yes']
    if force_full_sync:
        logger.info("  🔄 Force full sync requested - ignoring checkpoint")
        last_sync = None
    
    # Determine sync mode
    is_full_sync = (last_sync is None)
    sync_mode = "full" if is_full_sync else "incremental"
    
    if is_full_sync:
        logger.info("  🔄 No previous sync - performing full sync")
        logger.info("")
        logger.info("=" * 60)
        logger.info("FULL SYNC: CLEANING BOTH DATA SOURCES")
        logger.info("=" * 60)
        logger.info("")
        
        # Clean MAIN data source
        logger.info("📦 Cleaning MAIN data source (servicedesk)...")
        main_docs = fetch_all_documents_from_dust(DUST_MAIN_DATA_SOURCE)
        if main_docs:
            stats['main_deleted'] = delete_documents_from_dust(DUST_MAIN_DATA_SOURCE, main_docs)
        
        # Clean ACTIVE data source
        logger.info("")
        logger.info("⚡ Cleaning ACTIVE data source (servicedesk-active)...")
        active_docs = fetch_all_documents_from_dust(DUST_ACTIVE_DATA_SOURCE)
        if active_docs:
            stats['active_deleted'] = delete_documents_from_dust(DUST_ACTIVE_DATA_SOURCE, active_docs)
        
        logger.info("")
        logger.info("✓ Both data sources cleaned - ready for fresh upload")
        logger.info("=" * 60)
    else:
        logger.info(f"  ✓ Performing incremental sync since {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("")
        logger.info("=" * 60)
        logger.info("INCREMENTAL SYNC: CLEANING ACTIVE DATA SOURCE")
        logger.info("=" * 60)
        logger.info("  📦 Main data source (servicedesk) will receive upserts only")
        logger.info("  ⚡ Active data source (servicedesk-active) will be rebuilt fresh")
        logger.info("")
        
        # Clean ONLY active data source (main gets upserts)
        logger.info("⚡ Cleaning ACTIVE data source (servicedesk-active)...")
        active_docs = fetch_all_documents_from_dust(DUST_ACTIVE_DATA_SOURCE)
        if active_docs:
            stats['active_deleted'] = delete_documents_from_dust(DUST_ACTIVE_DATA_SOURCE, active_docs)
            logger.info(f"  ✓ Deleted {stats['active_deleted']} documents from servicedesk-active")
        
        logger.info("✓ Active data source cleaned - will rebuild with current active tickets only")
        logger.info("=" * 60)
    
    # Fetch tickets
    tickets = fetch_tickets(last_sync)
    
    if not tickets:
        logger.info("")
        logger.info("No tickets to process - sync complete")
        save_checkpoint(sync_start_time)
        return
    
    # Process in batches
    total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
    
    # Time estimate
    if is_full_sync:
        est_minutes = total_batches * 0.9  # ~54s per batch for dual upload
        logger.info("")
        logger.info(f"Processing {len(tickets)} tickets in {total_batches} batches of {BATCH_SIZE}")
        logger.info(f"Estimated time: {int(est_minutes)} minutes (full sync - dual upload)")
    else:
        est_minutes = total_batches * 0.4  # ~24s per batch for incremental
        logger.info("")
        logger.info(f"Processing {len(tickets)} tickets in {total_batches} batches of {BATCH_SIZE}")
        logger.info(f"Estimated time: {int(est_minutes)} minutes (incremental with active rebuild)")
    
    # Process batches
    for i in range(0, len(tickets), BATCH_SIZE):
        batch = tickets[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        process_batch(batch, batch_num, total_batches, is_full_sync)
    
    # Save checkpoint
    save_checkpoint(sync_start_time)
    
    # Calculate execution time
    sync_end_time = datetime.now(timezone.utc)
    execution_time = (sync_end_time - sync_start_time).total_seconds()
    execution_minutes = execution_time / 60
    
    logger.info("")
    logger.info(f"⏱️  Total execution time: {execution_minutes:.1f} minutes")
    
    # Print summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("SYNC COMPLETE - FINAL SUMMARY")
    logger.info("=" * 60)
    logger.info("")
    logger.info("📊 TICKET STATISTICS:")
    logger.info(f"  Sync Mode:         {sync_mode}")
    logger.info(f"  Total Fetched:     {stats['tickets_fetched']}")
    logger.info(f"  Total Processed:   {stats['tickets_processed']}")
    logger.info(f"  Uploaded to Main:  {stats['main_uploaded']}")
    logger.info(f"  Uploaded to Active: {stats['active_uploaded']}")
    logger.info(f"  Upload Failures:   {stats['upload_failures']}")
    
    if stats['tickets_processed'] > 0:
        success_rate = (stats['main_uploaded'] / stats['tickets_processed']) * 100
        logger.info(f"  Success Rate:      {success_rate:.1f}%")
    
    logger.info("")
    logger.info("📈 TICKET BREAKDOWN:")
    logger.info(f"  Active Tickets:    {stats['active_count']}")
    logger.info(f"  Closed Tickets:    {stats['closed_count']}")
    logger.info(f"  Service Requests:  {stats['service_requests']}")
    logger.info(f"  Incidents:         {stats['incidents']}")
    
    if stats['new_tickets'] > 0 or stats['updated_tickets'] > 0:
        logger.info("")
        logger.info("🆕 NEW vs UPDATED:")
        logger.info(f"  New Tickets:       {stats['new_tickets']}")
        logger.info(f"  Updated Tickets:   {stats['updated_tickets']}")
    
    logger.info("")
    logger.info("💬 CONVERSATION DATA:")
    logger.info(f"  Conversations Fetched:     {stats['conversations_fetched']}")
    logger.info(f"  Total Exchanges:           {stats['total_exchanges']}")
    if stats['conversations_fetched'] > 0:
        avg_exchanges = stats['total_exchanges'] / stats['conversations_fetched']
        logger.info(f"  Average per Ticket:        {avg_exchanges:.1f}")
    
    if stats['main_deleted'] > 0 or stats['active_deleted'] > 0:
        logger.info("")
        logger.info("🧹 CLEANUP STATISTICS:")
        if stats['main_deleted'] > 0:
            logger.info(f"  Main - Documents Deleted:  {stats['main_deleted']}")
        if stats['active_deleted'] > 0:
            logger.info(f"  Active - Documents Deleted: {stats['active_deleted']}")
    
    logger.info("")
    logger.info("🔧 API STATISTICS:")
    logger.info(f"  Total API Calls:   {stats['api_calls']}")
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✓ ServiceDesk Intelligence Suite {VERSION}")
    logger.info("  Dual Data Source + Token Retry + Rate Limit Handling")
    logger.info("=" * 60)

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    try:
        # Validate configuration
        logger.info("=" * 60)
        logger.info("VALIDATING ENVIRONMENT VARIABLES")
        logger.info("=" * 60)
        logger.info("")
        
        required_vars = [
            'DUST_API_KEY', 'DUST_WORKSPACE_ID',
            'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN'
        ]
        
        missing = [var for var in required_vars if not os.getenv(var)]
        
        if missing:
            logger.error(f"Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)
        
        logger.info("✓ Configuration loaded")
        logger.info(f"  Workspace: {DUST_WORKSPACE_ID}")
        logger.info(f"  Main Data Source: {DUST_MAIN_DATA_SOURCE} (all tickets)")
        logger.info(f"  Active Data Source: {DUST_ACTIVE_DATA_SOURCE} (active only)")
        logger.info(f"  SDP Domain: {SDP_DOMAIN}")
        logger.info(f"  Version: {VERSION}")
        logger.info("")
        
        # Run sync
        run_sync()
        
    except KeyboardInterrupt:
        logger.info("\n\nSync interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
