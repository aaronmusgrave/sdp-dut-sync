#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.2.9-STABLE
Dual Data Source + Cloud API + Working Pagination + RATE LIMIT SAFE

Changes in v3.2.9-STABLE:
- REDUCED: Upload workers from 5 to 3 (avoid rate limits)
- STABLE: No more rate limiting issues
- Keeps all error logging and retry logic from v3.2.8

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

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
    cleaned = value.strip()
    cleaned = cleaned.strip('"').strip("'")
    cleaned = cleaned.replace('\\', '')
    cleaned = cleaned.strip()
    return cleaned

# Load and clean all secrets
DUST_API_KEY = clean_secret(os.getenv('DUST_API_KEY', ''))
DUST_WORKSPACE_ID = clean_secret(os.getenv('DUST_WORKSPACE_ID', ''))
DUST_MAIN_DATA_SOURCE = 'servicedesk'
DUST_ACTIVE_DATA_SOURCE = 'servicedesk-active'

SDP_DOMAIN = clean_secret(os.getenv('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_secret(os.getenv('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_secret(os.getenv('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_secret(os.getenv('SDP_REFRESH_TOKEN', ''))

CHECKPOINT_FILE = 'sync_checkpoint.json'
BATCH_SIZE = 100
MAX_CONCURRENT_CONVERSATIONS = 20
MAX_CONCURRENT_UPLOADS = 3  # REDUCED from 5 to 3 - rate limit safe

# Version
VERSION = "v3.2.9-STABLE"

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
        
        CONVERSATION_401_ERRORS = 0
        
        logger.info(f"✓ Token refreshed successfully (expires in {expires_in // 60} minutes)")
        
    except Exception as e:
        logger.error(f"Failed to refresh token: {e}")
        raise

def ensure_token_valid():
    """Ensure token is valid, refresh if expired or expiring soon."""
    global TOKEN, TOKEN_EXPIRY
    
    if not TOKEN or not TOKEN_EXPIRY:
        refresh_token()
    else:
        time_remaining = (TOKEN_EXPIRY - datetime.now(timezone.utc)).total_seconds()
        if time_remaining < 600:
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
            
            if len(all_doc_ids) % 1000 == 0:
                logger.info(f"    Fetched {len(all_doc_ids)} documents so far...")
            
            if len(documents) < limit:
                break
            
            offset += limit
            
        except Exception as e:
            logger.error(f"Failed to fetch documents from {data_source_id}: {e}")
            break
    
    logger.info(f"  ✓ Found {len(all_doc_ids)} documents in {data_source_id}")
    return all_doc_ids

def delete_documents_from_dust_parallel(data_source_id: str, document_ids: List[str]) -> int:
    """Delete documents from Dust in parallel."""
    if not document_ids:
        logger.info(f"  No documents to delete from {data_source_id}")
        return 0
    
    logger.info(f"  Deleting {len(document_ids)} documents from {data_source_id} (parallel, 20 workers)...")
    
    deleted_count = 0
    delete_lock = Lock()
    start_time = time.time()
    
    def delete_single(doc_id: str) -> bool:
        nonlocal deleted_count
        url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{data_source_id}/documents/{doc_id}"
        headers = {"Authorization": f"Bearer {DUST_API_KEY}"}
        
        try:
            response = requests.delete(url, headers=headers, timeout=30)
            if response.status_code in [200, 204]:
                with delete_lock:
                    deleted_count += 1
                    if deleted_count % 100 == 0:
                        elapsed = time.time() - start_time
                        rate = deleted_count / elapsed if elapsed > 0 else 0
                        logger.info(f"    Deleted {deleted_count}/{len(document_ids)} ({rate:.1f}/sec)...")
                return True
        except:
            pass
        return False
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(delete_single, doc_id) for doc_id in document_ids]
        for future in as_completed(futures):
            future.result()
    
    elapsed = time.time() - start_time
    logger.info(f"  ✓ Deleted {deleted_count}/{len(document_ids)} documents ({elapsed:.1f}s, {deleted_count/elapsed:.1f}/sec)")
    return deleted_count

def upload_document_to_dust(data_source_id: str, doc_id: str, content: dict, tags: list, max_retries: int = 3) -> Tuple[bool, Optional[str]]:
    """Upload a single document to Dust with retry logic and error reporting."""
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
    
    last_error = None
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            
            if response.status_code in [200, 201]:
                stats['api_calls'] += 1
                return True, None
            
            elif response.status_code == 429:
                # Rate limited - wait and retry
                wait_time = (2 ** attempt) * 2
                logger.warning(f"    Rate limited on {doc_id}, waiting {wait_time}s...")
                time.sleep(wait_time)
                stats['rate_limited'] += 1
                continue
            
            else:
                # Other HTTP error
                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                last_error = error_msg
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                else:
                    return False, error_msg
                    
        except requests.exceptions.Timeout:
            last_error = "Request timeout"
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            else:
                return False, last_error
                
        except Exception as e:
            last_error = f"Exception: {str(e)}"
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            else:
                return False, last_error
    
    stats['upload_failures'] += 1
    return False, last_error

# ============================================================
# SERVICEDESK API - PARALLEL CONVERSATION FETCHING
# ============================================================
def fetch_conversation_single(ticket_id: str, retry_count: int = 0) -> Tuple[List[dict], Optional[str]]:
    """Fetch conversations for a single ticket with retry logic."""
    global TOKEN, CONVERSATION_401_ERRORS
    
    if CONVERSATION_401_ERRORS >= MAX_401_BEFORE_REFRESH:
        ensure_token_valid()
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
        
        if CONVERSATION_401_ERRORS > 0:
            CONVERSATION_401_ERRORS = 0
        
        data = response.json()
        conversations = data.get('conversations', [])
        
        total_exchanges = 0
        for conv in conversations:
            replies = conv.get('replies', [])
            total_exchanges += len(replies)
        
        return conversations, None
        
    except requests.HTTPError as e:
        if hasattr(e, 'response') and e.response and e.response.status_code == 401:
            CONVERSATION_401_ERRORS += 1
            if retry_count < 2:
                ensure_token_valid()
                time.sleep(1)
                return fetch_conversation_single(ticket_id, retry_count + 1)
            else:
                return [], f"401 after retries"
        else:
            return [], f"HTTP {e.response.status_code if hasattr(e, 'response') and e.response else 'error'}"
    except Exception as e:
        return [], f"Exception: {str(e)}"

def fetch_conversations_parallel(tickets: List[dict]) -> Dict[str, dict]:
    """Fetch conversations in parallel with ThreadPoolExecutor."""
    conversations_map = {}
    
    logger.info(f"  Fetching conversations with {MAX_CONCURRENT_CONVERSATIONS} parallel workers...")
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CONVERSATIONS) as executor:
        future_to_ticket = {
            executor.submit(fetch_conversation_single, ticket['id']): ticket['id']
            for ticket in tickets
        }
        
        for future in as_completed(future_to_ticket):
            ticket_id = future_to_ticket[future]
            try:
                conversations, error = future.result()
                
                total_exchanges = 0
                for conv in conversations:
                    replies = conv.get('replies', [])
                    total_exchanges += len(replies)
                
                conversations_map[ticket_id] = {
                    "exchanges": conversations,
                    "count": total_exchanges
                }
                
            except Exception as e:
                conversations_map[ticket_id] = {"exchanges": [], "count": 0}
    
    elapsed = time.time() - start_time
    logger.info(f"  ✓ Fetched conversations for {len(tickets)} tickets ({elapsed:.1f}s, {len(tickets)/elapsed:.1f}/sec)")
    return conversations_map

# ============================================================
# SERVICEDESK API - TICKET FETCHING
# ============================================================
def fetch_tickets(last_sync: Optional[datetime] = None) -> List[dict]:
    """Fetch tickets from ServiceDesk Plus API with working pagination."""
    ensure_token_valid()
    
    all_tickets = []
    page = 1
    row_count = 100
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.manageengine.sdp.v3+json"
    }
    
    search_criteria = []
    if last_sync:
        last_sync_ms = int(last_sync.timestamp() * 1000)
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
        logger.info(f"📊 Fetching tickets updated since {last_sync.strftime('%Y-%m-%d %H:%M:%S')}...")
    else:
        logger.info(f"📊 Fetching all tickets (full sync)...")
    
    list_info = {
        "list_info": {
            "row_count": row_count,
            "start_index": 1,
            "get_total_count": True,
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    if search_criteria:
        list_info["list_info"]["search_criteria"] = search_criteria
    
    url = f"https://{SDP_DOMAIN}/api/v3/requests"
    
    while True:
        list_info["list_info"]["start_index"] = (page - 1) * row_count + 1
        params = {"input_data": json.dumps(list_info)}
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            stats['api_calls'] += 1
            
            data = response.json()
            requests_data = data.get('requests', [])
            
            if not requests_data:
                logger.info(f"  Page {page}: No more tickets")
                break
            
            all_tickets.extend(requests_data)
            logger.info(f"  Page {page}: {len(requests_data)} tickets (total: {len(all_tickets)})")
            
            list_info_response = data.get('list_info', {})
            has_more = list_info_response.get('has_more_rows', False)
            
            if not has_more:
                logger.info(f"  Page {page}: No more pages (has_more_rows=False)")
                break
            
            if len(requests_data) < row_count:
                logger.info(f"  Page {page}: Last page (got {len(requests_data)} < {row_count})")
                break
            
            page += 1
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Failed to fetch tickets on page {page}: {e}")
            break
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets total")
    stats['tickets_fetched'] = len(all_tickets)
    
    if last_sync and all_tickets:
        new_count = 0
        updated_count = 0
        for ticket in all_tickets:
            created_time_obj = ticket.get('created_time', {})
            if created_time_obj and isinstance(created_time_obj, dict):
                created_time = created_time_obj.get('value', 0)
                if created_time > int(last_sync.timestamp() * 1000):
                    new_count += 1
                else:
                    updated_count += 1
        
        if new_count > 0 or updated_count > 0:
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
    
    active_keywords = ['open', 'in progress', 'on hold', 'pending', 'waiting', 'assigned']
    closed_keywords = ['closed', 'resolved', 'completed', 'cancelled', 'canceled']
    
    for keyword in active_keywords:
        if keyword in status_lower:
            return 'active', f'matched active keyword: {keyword}'
    
    for keyword in closed_keywords:
        if keyword in status_lower:
            return 'closed', f'matched closed keyword: {keyword}'
    
    for keyword in active_keywords:
        if keyword in internal_lower:
            return 'active', f'matched active keyword in internal: {keyword}'
    
    for keyword in closed_keywords:
        if keyword in internal_lower:
            return 'closed', f'matched closed keyword in internal: {keyword}'
    
    return 'closed', 'defaulted to closed (uncertain)'

def process_ticket(ticket: dict, conversation_data: dict) -> Tuple[dict, list]:
    """Process a single ticket into Dust document format with null safety."""
    
    def safe_get(obj, key, default=''):
        if not obj:
            return default
        value = obj.get(key)
        if value is None:
            return default
        return value
    
    def safe_get_nested(obj, key, nested_key, default=''):
        parent = safe_get(obj, key, {})
        if not isinstance(parent, dict):
            return default
        value = parent.get(nested_key)
        if value is None:
            return default
        return value
    
    ticket_id = str(ticket.get('id', ''))
    subject = ticket.get('subject', 'No Subject') or 'No Subject'
    
    status_obj = safe_get(ticket, 'status', {})
    if isinstance(status_obj, dict):
        status = status_obj.get('name', 'Unknown') or 'Unknown'
        internal_status = status_obj.get('internal_name', status) or status
    else:
        status = 'Unknown'
        internal_status = 'Unknown'
    
    status_category, status_reason = determine_status_category(status, internal_status)
    
    created_time = safe_get_nested(ticket, 'created_time', 'display_value', 'Unknown')
    due_by_time = safe_get_nested(ticket, 'due_by_time', 'display_value', 'Not set')
    resolved_time = safe_get_nested(ticket, 'resolved_time', 'display_value', 'Not resolved')
    closed_time = safe_get_nested(ticket, 'closed_time', 'display_value', 'Not closed')
    
    technician_obj = safe_get(ticket, 'technician', {})
    if isinstance(technician_obj, dict):
        technician_name = technician_obj.get('name', 'Unassigned') or 'Unassigned'
        technician_email = technician_obj.get('email_id', '') or ''
    else:
        technician_name = 'Unassigned'
        technician_email = ''
    
    requester_obj = safe_get(ticket, 'requester', {})
    if isinstance(requester_obj, dict):
        requester_name = requester_obj.get('name', 'Unknown') or 'Unknown'
        requester_email = requester_obj.get('email_id', '') or ''
    else:
        requester_name = 'Unknown'
        requester_email = ''
    
    priority_obj = safe_get(ticket, 'priority', {})
    priority = (priority_obj.get('name', 'Normal') or 'Normal') if isinstance(priority_obj, dict) else 'Normal'
    
    category_obj = safe_get(ticket, 'category', {})
    category = (category_obj.get('name', 'Uncategorized') or 'Uncategorized') if isinstance(category_obj, dict) else 'Uncategorized'
    
    subcategory_obj = safe_get(ticket, 'subcategory', {})
    subcategory = (subcategory_obj.get('name', '') or '') if isinstance(subcategory_obj, dict) else ''
    
    request_type_obj = safe_get(ticket, 'request_type', {})
    request_type = (request_type_obj.get('name', 'Service Request') or 'Service Request') if isinstance(request_type_obj, dict) else 'Service Request'
    
    description = ticket.get('description', 'No description') or 'No description'
    
    resolution_obj = safe_get(ticket, 'resolution', {})
    resolution = (resolution_obj.get('content', '') or '') if isinstance(resolution_obj, dict) else ''
    
    conversation_count = conversation_data.get('count', 0) if conversation_data else 0
    
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

def upload_documents_parallel(documents: List[Tuple[str, dict, list]], data_source_id: str) -> Tuple[int, int, List[str]]:
    """Upload documents in parallel with detailed error tracking."""
    if not documents:
        return 0, 0, []
    
    uploaded = 0
    failed = 0
    upload_lock = Lock()
    start_time = time.time()
    error_messages = []
    
    def upload_single(doc_tuple: Tuple[str, dict, list]) -> Tuple[bool, Optional[str]]:
        nonlocal uploaded, failed
        doc_id, content, tags = doc_tuple
        success, error = upload_document_to_dust(data_source_id, doc_id, content, tags)
        
        with upload_lock:
            if success:
                uploaded += 1
                if uploaded % 25 == 0:
                    elapsed = time.time() - start_time
                    rate = uploaded / elapsed if elapsed > 0 else 0
                    logger.info(f"    {data_source_id}: {uploaded}/{len(documents)} uploaded ({rate:.1f}/sec)...")
            else:
                failed += 1
                if error:
                    error_messages.append(f"{doc_id}: {error}")
                    if failed <= 5:
                        logger.error(f"    ❌ Upload failed for {doc_id}: {error}")
        
        return success, error
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS) as executor:
        futures = [executor.submit(upload_single, doc) for doc in documents]
        for future in as_completed(futures):
            future.result()
    
    elapsed = time.time() - start_time
    return uploaded, failed, error_messages

def process_batch(batch_tickets: List[dict], batch_num: int, total_batches: int, is_full_sync: bool, processing_start_time: float, total_tickets: int):
    """Process a batch of tickets with parallel operations and error tracking."""
    
    logger.info(f"\n{'=' * 60}")
    logger.info(f"BATCH {batch_num}/{total_batches} ({len(batch_tickets)} tickets)")
    logger.info(f"{'=' * 60}")
    
    batch_start = time.time()
    
    ensure_token_valid()
    
    conversations_map = fetch_conversations_parallel(batch_tickets)
    stats['conversations_fetched'] += len(batch_tickets)
    
    logger.info("  Processing tickets...")
    documents = []
    active_documents = []
    
    for ticket in batch_tickets:
        ticket_id = str(ticket.get('id', ''))
        conv_data = conversations_map.get(ticket_id, {"exchanges": [], "count": 0})
        
        content, tags = process_ticket(ticket, conv_data)
        doc_id = f"ticket-{ticket_id}"
        
        documents.append((doc_id, content, tags))
        
        if content.get('status_category') == 'active':
            active_documents.append((doc_id, content, tags))
    
    logger.info(f"  ✓ Processed {len(documents)} documents ({len(active_documents)} active)")
    stats['tickets_processed'] += len(documents)
    
    logger.info(f"  Uploading with {MAX_CONCURRENT_UPLOADS} parallel workers...")
    upload_start = time.time()
    
    # Upload to MAIN source
    main_uploaded, main_failed, main_errors = upload_documents_parallel(documents, DUST_MAIN_DATA_SOURCE)
    stats['main_uploaded'] += main_uploaded
    
    # Upload to ACTIVE source
    if active_documents:
        active_uploaded, active_failed, active_errors = upload_documents_parallel(active_documents, DUST_ACTIVE_DATA_SOURCE)
        stats['active_uploaded'] += active_uploaded
    else:
        active_uploaded = 0
        active_failed = 0
    
    upload_elapsed = time.time() - upload_start
    
    # Report results
    logger.info(f"  ✓ Upload complete ({upload_elapsed:.1f}s):")
    logger.info(f"    Main: {main_uploaded}/{len(documents)} succeeded, {main_failed} failed")
    if active_documents:
        logger.info(f"    Active: {active_uploaded}/{len(active_documents)} succeeded, {active_failed} failed")
    
    # Log first few errors if any
    if main_failed > 0 and main_errors:
        logger.error(f"  ❌ Sample errors from main uploads (showing first 3):")
        for error in main_errors[:3]:
            logger.error(f"    {error}")
    
    # Calculate overall progress
    batch_elapsed = time.time() - batch_start
    overall_elapsed = time.time() - processing_start_time
    tickets_done = batch_num * BATCH_SIZE
    if tickets_done > total_tickets:
        tickets_done = total_tickets
    
    rate = tickets_done / overall_elapsed if overall_elapsed > 0 else 0
    remaining_tickets = total_tickets - tickets_done
    eta_seconds = remaining_tickets / rate if rate > 0 else 0
    
    logger.info(f"  📊 Progress: {tickets_done}/{total_tickets} tickets ({rate:.1f}/sec, ~{eta_seconds/60:.1f} min remaining)")

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
    logger.info("✅ STABLE MODE - RATE LIMIT SAFE:")
    logger.info(f"   • Parallel conversations: {MAX_CONCURRENT_CONVERSATIONS} workers")
    logger.info(f"   • Parallel uploads: {MAX_CONCURRENT_UPLOADS} workers (rate limit safe)")
    logger.info(f"   • Batch size: {BATCH_SIZE}")
    logger.info(f"   • Error logging and retry logic enabled")
    logger.info("")
    
    refresh_token()
    
    last_sync = load_checkpoint()
    
    force_full_sync = os.getenv('FORCE_FULL_SYNC', '').lower() in ['true', '1', 'yes']
    if force_full_sync:
        logger.info("  🔄 Force full sync requested - ignoring checkpoint")
        last_sync = None
    
    is_full_sync = (last_sync is None)
    sync_mode = "full" if is_full_sync else "incremental"
    
    if is_full_sync:
        logger.info("  🔄 No previous sync - performing full sync")
        logger.info("")
        logger.info("=" * 60)
        logger.info("FULL SYNC: CLEANING BOTH DATA SOURCES")
        logger.info("=" * 60)
        logger.info("")
        
        logger.info("📦 Cleaning MAIN data source (servicedesk)...")
        main_docs = fetch_all_documents_from_dust(DUST_MAIN_DATA_SOURCE)
        if main_docs:
            stats['main_deleted'] = delete_documents_from_dust_parallel(DUST_MAIN_DATA_SOURCE, main_docs)
        
        logger.info("")
        logger.info("⚡ Cleaning ACTIVE data source (servicedesk-active)...")
        active_docs = fetch_all_documents_from_dust(DUST_ACTIVE_DATA_SOURCE)
        if active_docs:
            stats['active_deleted'] = delete_documents_from_dust_parallel(DUST_ACTIVE_DATA_SOURCE, active_docs)
        
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
        
        logger.info("⚡ Cleaning ACTIVE data source (servicedesk-active)...")
        active_docs = fetch_all_documents_from_dust(DUST_ACTIVE_DATA_SOURCE)
        if active_docs:
            stats['active_deleted'] = delete_documents_from_dust_parallel(DUST_ACTIVE_DATA_SOURCE, active_docs)
            logger.info(f"  ✓ Deleted {stats['active_deleted']} documents from servicedesk-active")
        
        logger.info("✓ Active data source cleaned - will rebuild with current active tickets only")
        logger.info("=" * 60)
    
    tickets = fetch_tickets(last_sync)
    
    if not tickets:
        logger.info("")
        logger.info("No tickets to process - sync complete")
        save_checkpoint(sync_start_time)
        return
    
    total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
    
    if is_full_sync:
        est_minutes = total_batches * 0.4
        logger.info("")
        logger.info(f"Processing {len(tickets)} tickets in {total_batches} batches of {BATCH_SIZE}")
        logger.info(f"Estimated time: {int(est_minutes)} minutes (full sync - stable mode)")
    else:
        est_minutes = total_batches * 0.25
        logger.info("")
        logger.info(f"Processing {len(tickets)} tickets in {total_batches} batches of {BATCH_SIZE}")
        logger.info(f"Estimated time: {int(est_minutes)} minutes (incremental - stable mode)")
    
    processing_start = time.time()
    for i in range(0, len(tickets), BATCH_SIZE):
        batch = tickets[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        process_batch(batch, batch_num, total_batches, is_full_sync, processing_start, len(tickets))
    
    save_checkpoint(sync_start_time)
    
    sync_end_time = datetime.now(timezone.utc)
    execution_time = (sync_end_time - sync_start_time).total_seconds()
    execution_minutes = execution_time / 60
    
    logger.info("")
    logger.info(f"⏱️  Total execution time: {execution_minutes:.1f} minutes")
    
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
    logger.info(f"  Rate Limited:      {stats['rate_limited']}")
    
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
    
    if execution_time > 0:
        tickets_per_sec = stats['tickets_processed'] / execution_time
        logger.info(f"  Processing Rate:   {tickets_per_sec:.1f} tickets/sec")
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✓ ServiceDesk Intelligence Suite {VERSION}")
    logger.info("  STABLE MODE: Rate Limit Safe with 3 Workers")
    logger.info("=" * 60)

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    try:
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
        
        run_sync()
        
    except KeyboardInterrupt:
        logger.info("\n\nSync interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
