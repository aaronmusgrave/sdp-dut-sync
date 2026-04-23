#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v4.1.4
TWO DATA SOURCES:
  - servicedesk:        All non-active tickets (closed + SDP archived)
  - servicedesk-active: Always current, rebuilt every sync

FIXES in v4.1.4:
  1. Archive tickets now use /api/v3/archive_requests endpoint (was returning
     live tickets only - now correctly fetches all 12,000+ archived tickets)
  2. SLA endpoint removed - /requests/{id}/sla returns the global policy
     catalog, not per-ticket data. All SLA fields now read directly from
     the ticket object (due_by_time, is_overdue, first_response_due_by_time)
  3. ~100 fewer API calls per batch (no more SLA endpoint calls)

SYNC STRATEGY:
  - Incremental: Upload changed live tickets to servicedesk, rebuild servicedesk-active
  - Full sync:   Rebuild both sources + fetch SDP archived tickets into servicedesk
  - Archive:     Syncs on full sync or FORCE_ARCHIVE_SYNC=true (auto after 7 days)
"""

import os
import sys
import json
import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import re
from html import unescape

# ============================================================
# CONFIGURATION
# ============================================================
def clean_secret(value: str) -> str:
    """Remove quotes, backslashes, and whitespace from secrets."""
    if not value:
        return ''
    cleaned = value.strip()
    cleaned = cleaned.strip('"').strip("'")
    cleaned = cleaned.replace('\\', '')
    cleaned = cleaned.strip()
    return cleaned

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
MAX_CONCURRENT_CONVERSATIONS = 10
MAX_CONCURRENT_INDIVIDUAL_NOTIFS = 5
MAX_CONCURRENT_UPLOADS = 2
UPLOAD_DELAY = 0.3

VERSION = "v4.1.4"

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

CONVERSATION_DIAGNOSTICS = {
    'notifications_list_success': 0,
    'notifications_list_empty': 0,
    'notifications_list_error': 0,
    'individual_fetch_success': 0,
    'individual_fetch_empty': 0,
    'individual_fetch_error': 0,
    'notes_success': 0,
    'notes_empty': 0,
    'notes_error': 0,
    'total_notifications': 0,
    'total_notes': 0,
    'total_conversations': 0,
    'content_extracted': 0,
    'content_empty': 0,
}

stats = {
    'tickets_fetched': 0,
    'tickets_processed': 0,
    'main_uploaded': 0,
    'active_uploaded': 0,
    'archive_uploaded': 0,
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
    'archived_count': 0,
    'service_requests': 0,
    'incidents': 0,
    'total_exchanges': 0,
    'all_tickets_fetched': 0,
    'archived_tickets_fetched': 0,
    'changed_ticket_ids': set(),
    'tickets_skipped': 0,
    'sla_violated': 0,
    'sla_delta_calculated': 0,
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

        logger.info(f"✓ Token refreshed (expires in {expires_in // 60} minutes)")

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
            logger.info(f"Token expiring in {int(time_remaining/60)} min - refreshing...")
            refresh_token()

# ============================================================
# CHECKPOINT MANAGEMENT
# ============================================================
def load_checkpoint() -> Tuple[Optional[datetime], Optional[datetime]]:
    """Load last sync timestamps. Returns (last_sync, last_archive_sync)."""
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                data = json.load(f)

            last_sync = None
            last_archive_sync = None

            ts = data.get('last_sync_timestamp')
            if ts:
                last_sync = datetime.fromisoformat(ts)
                logger.info(f"  Checkpoint: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")

            arch_ts = data.get('last_archive_sync_timestamp')
            if arch_ts:
                last_archive_sync = datetime.fromisoformat(arch_ts)
                logger.info(f"  Archive checkpoint: {last_archive_sync.strftime('%Y-%m-%d %H:%M:%S')}")

            return last_sync, last_archive_sync

        except Exception as e:
            logger.warning(f"Failed to load checkpoint: {e}")

    logger.info("  No checkpoint - will perform full sync")
    return None, None

def save_checkpoint(timestamp: datetime, archive_timestamp: Optional[datetime] = None):
    """Save sync timestamps to checkpoint file."""
    try:
        existing = {}
        if os.path.exists(CHECKPOINT_FILE):
            with open(CHECKPOINT_FILE, 'r') as f:
                existing = json.load(f)

        existing['last_sync_timestamp'] = timestamp.isoformat()
        existing['version'] = VERSION

        if archive_timestamp:
            existing['last_archive_sync_timestamp'] = archive_timestamp.isoformat()

        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(existing, f, indent=2)

        logger.info(f"  ✓ Checkpoint saved: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        if archive_timestamp:
            logger.info(f"  ✓ Archive checkpoint saved: {archive_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")

    except Exception as e:
        logger.warning(f"Failed to save checkpoint: {e}")

def check_force_full_sync() -> bool:
    """Check if full sync is forced via environment variable."""
    possible_vars = [
        'FORCE_FULL_SYNC', 'FULL_SYNC',
        'force_full_sync', 'full_sync',
        'INPUT_FORCE_FULL_SYNC', 'INPUT_FULL_SYNC'
    ]

    logger.info("  🔍 Checking for full sync env vars...")
    for var_name in possible_vars:
        var_value = os.getenv(var_name, '')
        if var_value:
            cleaned = clean_secret(var_value)
            logger.info(f"    Found {var_name}={var_value} (cleaned: {cleaned})")
            if cleaned.lower() in ['true', '1', 'yes', 'on']:
                logger.info(f"    ✓ Full sync triggered by {var_name}")
                return True

    logger.info("    No full sync env vars found")
    return False

def check_force_archive_sync() -> bool:
    """Check if archive sync is forced via environment variable."""
    possible_vars = [
        'FORCE_ARCHIVE_SYNC', 'ARCHIVE_SYNC',
        'force_archive_sync', 'archive_sync',
        'INPUT_FORCE_ARCHIVE_SYNC'
    ]

    for var_name in possible_vars:
        var_value = os.getenv(var_name, '')
        if var_value:
            cleaned = clean_secret(var_value)
            if cleaned.lower() in ['true', '1', 'yes', 'on']:
                logger.info(f"    ✓ Archive sync triggered by {var_name}")
                return True

    return False

# ============================================================
# HTML STRIPPING
# ============================================================
def strip_html_enhanced(html_content: str) -> str:
    """Enhanced HTML stripping."""
    if not html_content:
        return ""
    try:
        text = re.sub('<[^<]+?>', '', html_content)
        text = unescape(text)
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        if not text and html_content and len(html_content.strip()) > 0:
            return html_content.strip()
        return text
    except Exception as e:
        logger.debug(f"HTML stripping failed: {e}")
        return html_content

# ============================================================
# SERVICEDESK API - NOTIFICATIONS
# ============================================================
def fetch_individual_notification(ticket_id: str, notification_id: str) -> Optional[dict]:
    """Fetch individual notification for full email content."""
    global TOKEN, CONVERSATION_401_ERRORS

    if CONVERSATION_401_ERRORS >= MAX_401_BEFORE_REFRESH:
        ensure_token_valid()
        CONVERSATION_401_ERRORS = 0

    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }

    url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications/{notification_id}"

    try:
        response = requests.get(url, headers=headers, timeout=30)
        stats['api_calls'] += 1

        if response.status_code == 200:
            data = response.json()
            notification = data.get('notification', data)
            description = notification.get('description', '')

            if description and len(description.strip()) > 0:
                CONVERSATION_DIAGNOSTICS['individual_fetch_success'] += 1
                CONVERSATION_DIAGNOSTICS['content_extracted'] += 1
            else:
                CONVERSATION_DIAGNOSTICS['individual_fetch_empty'] += 1
                CONVERSATION_DIAGNOSTICS['content_empty'] += 1

            return notification

        elif response.status_code == 401:
            CONVERSATION_401_ERRORS += 1
            CONVERSATION_DIAGNOSTICS['individual_fetch_error'] += 1
            return None
        else:
            CONVERSATION_DIAGNOSTICS['individual_fetch_error'] += 1
            return None

    except Exception as e:
        CONVERSATION_DIAGNOSTICS['individual_fetch_error'] += 1
        logger.debug(f"Failed to fetch notification {notification_id}: {e}")
        return None

def fetch_notifications_with_content(ticket_id: str) -> Tuple[List[dict], List[dict]]:
    """Fetch notifications and notes with full content."""
    global TOKEN

    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }

    notifications_with_content = []
    notes = []

    # Notifications
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications"
        response = requests.get(url, headers=headers, timeout=30)
        stats['api_calls'] += 1

        if response.status_code == 200:
            data = response.json()
            notification_list = data.get('notifications', [])

            if notification_list:
                CONVERSATION_DIAGNOSTICS['notifications_list_success'] += 1
                CONVERSATION_DIAGNOSTICS['total_notifications'] += len(notification_list)

                with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_INDIVIDUAL_NOTIFS) as executor:
                    future_to_id = {
                        executor.submit(fetch_individual_notification, ticket_id, notif.get('id')): notif
                        for notif in notification_list
                        if notif.get('id')
                    }
                    for future in as_completed(future_to_id):
                        original_notif = future_to_id[future]
                        try:
                            full_notif = future.result()
                            if full_notif:
                                notifications_with_content.append(full_notif)
                        except Exception as e:
                            logger.debug(f"Notification fetch failed: {e}")
                            notifications_with_content.append(original_notif)
            else:
                CONVERSATION_DIAGNOSTICS['notifications_list_empty'] += 1
        else:
            CONVERSATION_DIAGNOSTICS['notifications_list_error'] += 1

    except Exception as e:
        CONVERSATION_DIAGNOSTICS['notifications_list_error'] += 1
        logger.debug(f"Failed notification list for {ticket_id}: {e}")

    # Notes
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notes"
        response = requests.get(url, headers=headers, timeout=30)
        stats['api_calls'] += 1

        if response.status_code == 200:
            data = response.json()
            notes = data.get('notes', [])
            if notes:
                CONVERSATION_DIAGNOSTICS['notes_success'] += 1
                CONVERSATION_DIAGNOSTICS['total_notes'] += len(notes)
            else:
                CONVERSATION_DIAGNOSTICS['notes_empty'] += 1
        else:
            CONVERSATION_DIAGNOSTICS['notes_error'] += 1

    except Exception as e:
        CONVERSATION_DIAGNOSTICS['notes_error'] += 1
        logger.debug(f"Failed notes for {ticket_id}: {e}")

    return notifications_with_content, notes

def fetch_conversations_parallel(tickets: List[dict]) -> Dict[str, dict]:
    """Fetch conversations in parallel."""
    conversations_map = {}

    logger.info(f"  Fetching conversations ({MAX_CONCURRENT_CONVERSATIONS} workers)...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CONVERSATIONS) as executor:
        future_to_ticket = {
            executor.submit(fetch_notifications_with_content, ticket['id']): ticket['id']
            for ticket in tickets
        }
        for future in as_completed(future_to_ticket):
            ticket_id = future_to_ticket[future]
            try:
                notifications, notes = future.result()
                total_count = len(notifications) + len(notes)
                conversations_map[ticket_id] = {
                    "count": total_count,
                    "notifications": notifications,
                    "notes": notes
                }
                CONVERSATION_DIAGNOSTICS['total_conversations'] += total_count
            except Exception as e:
                conversations_map[ticket_id] = {"count": 0, "notifications": [], "notes": []}
                logger.debug(f"Conversation fetch failed for {ticket_id}: {e}")

    elapsed = time.time() - start_time
    logger.info(f"  ✓ Conversations fetched ({elapsed:.1f}s, {len(tickets)/elapsed:.1f}/sec)")
    return conversations_map

# ============================================================
# CONVERSATION FORMATTING
# ============================================================
def format_conversation_summary(
    notifications: List[dict],
    notes: List[dict],
    max_chars: int = 10000
) -> Tuple[str, str, int]:
    """Format notifications and notes into searchable conversation summary."""
    all_exchanges = []

    for notif in notifications:
        time_obj = notif.get('time', {})
        timestamp = time_obj.get('display_value', 'Unknown time') if isinstance(time_obj, dict) else str(time_obj or 'Unknown time')

        sender_obj = notif.get('sender', {})
        sender_name = sender_obj.get('name', 'Unknown') if isinstance(sender_obj, dict) else 'Unknown'

        content = strip_html_enhanced(notif.get('description', ''))

        if content and len(content.strip()) > 0:
            if len(content) > 2000:
                content = content[:2000] + "... (truncated)"
            all_exchanges.append({
                'timestamp': timestamp,
                'sender': sender_name,
                'type': 'email',
                'content': content
            })

    for note in notes:
        created_time_obj = note.get('created_time', {})
        timestamp = created_time_obj.get('display_value', 'Unknown time') if isinstance(created_time_obj, dict) else str(created_time_obj or 'Unknown time')

        created_by_obj = note.get('created_by', {})
        creator_name = created_by_obj.get('name', 'Unknown') if isinstance(created_by_obj, dict) else 'Unknown'

        content = strip_html_enhanced(note.get('description', ''))

        if content and len(content.strip()) > 0:
            if len(content) > 2000:
                content = content[:2000] + "... (truncated)"
            all_exchanges.append({
                'timestamp': timestamp,
                'sender': creator_name,
                'type': 'internal_note',
                'content': content
            })

    conversation_lines = []
    note_lines = []

    for exchange in all_exchanges:
        if exchange['type'] == 'internal_note':
            note_lines.append(f"[{exchange['timestamp']}] {exchange['sender']} (Internal Note):\n  {exchange['content']}")
        else:
            conversation_lines.append(f"[{exchange['timestamp']}] {exchange['sender']}:\n  {exchange['content']}")

    conversation_summary = "\n\n".join(conversation_lines) if conversation_lines else "No email exchanges"
    notes_summary = "\n\n".join(note_lines) if note_lines else "No internal notes"

    if len(conversation_summary) > max_chars:
        conversation_summary = conversation_summary[:max_chars] + "\n\n... (truncated)"
    if len(notes_summary) > max_chars:
        notes_summary = notes_summary[:max_chars] + "\n\n... (truncated)"

    return conversation_summary, notes_summary, len(all_exchanges)

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

            all_doc_ids.extend([doc['document_id'] for doc in documents])

            if len(all_doc_ids) % 1000 == 0:
                logger.info(f"    {len(all_doc_ids)} documents fetched so far...")

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

    logger.info(f"  Deleting {len(document_ids)} documents from {data_source_id} (20 workers)...")

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
    logger.info(f"  ✓ Deleted {deleted_count}/{len(document_ids)} ({elapsed:.1f}s, {deleted_count/elapsed:.1f}/sec)")
    return deleted_count

def upload_document_to_dust(
    data_source_id: str,
    doc_id: str,
    content: dict,
    tags: list,
    max_retries: int = 3
) -> Tuple[bool, Optional[str]]:
    """Upload a single document to Dust with retry logic."""
    url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{data_source_id}/documents/{doc_id}"
    headers = {
        "Authorization": f"Bearer {DUST_API_KEY}",
        "Content-Type": "application/json"
    }

    text_content = f"""
Ticket ID: {content.get('ticket_id', 'N/A')}
Subject: {content.get('subject', 'N/A')}
Status: {content.get('status', 'N/A')}
Priority: {content.get('priority', 'N/A')}
Technician (Assigned): {content.get('technician', 'Unassigned')}
Technician (Closed By): {content.get('closed_by', 'N/A')}
Requester: {content.get('requester', 'N/A')}
Category: {content.get('category', 'N/A')}
Archived: {content.get('is_archived', False)}
Created: {content.get('created_time', 'N/A')}
Resolved: {content.get('resolved_time', 'N/A')}
Closed: {content.get('closed_time', 'N/A')}

SLA INFORMATION:
  Policy:                {content.get('sla_policy', 'N/A')}
  Status:                {content.get('sla_status', 'N/A')}
  Compliance:            {content.get('sla_compliance_label', 'Unknown')}
  Days Delta:            {content.get('sla_days_delta', 'N/A')}
  Violated:              {content.get('sla_violated', False)}
  First Response Due:    {content.get('first_response_due', 'N/A')}
  First Response Late:   {content.get('first_response_overdue', False)}
  Resolution Due:        {content.get('sla_resolution_due', 'N/A')}

INITIAL REQUEST:
{content.get('description', 'N/A')}

CONVERSATION HISTORY ({content.get('conversation_count', 0)} exchanges):
{content.get('conversation_summary', 'No email exchanges')}

INTERNAL NOTES:
{content.get('internal_notes_summary', 'No internal notes')}

RESOLUTION:
{content.get('resolution', 'Not resolved')}
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
                time.sleep(UPLOAD_DELAY)
                return True, None

            elif response.status_code == 429:
                wait_time = (2 ** attempt) * 2
                logger.warning(f"    Rate limited on {doc_id}, waiting {wait_time}s...")
                time.sleep(wait_time)
                stats['rate_limited'] += 1
                continue

            else:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                else:
                    return False, last_error

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

def upload_documents_parallel(
    documents: List[Tuple[str, dict, list]],
    data_source_id: str
) -> Tuple[int, int, List[str]]:
    """Upload documents in parallel."""
    if not documents:
        return 0, 0, []

    uploaded = 0
    failed = 0
    upload_lock = Lock()
    start_time = time.time()
    error_messages = []

    def upload_single(doc_tuple):
        nonlocal uploaded, failed
        doc_id, content, tags = doc_tuple
        success, error = upload_document_to_dust(data_source_id, doc_id, content, tags)

        with upload_lock:
            if success:
                uploaded += 1
                if uploaded % 25 == 0:
                    elapsed = time.time() - start_time
                    rate = uploaded / elapsed if elapsed > 0 else 0
                    logger.info(f"    {data_source_id}: {uploaded}/{len(documents)} ({rate:.1f}/sec)...")
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
            return 'active', f'matched: {keyword}'
    for keyword in closed_keywords:
        if keyword in status_lower:
            return 'closed', f'matched: {keyword}'
    for keyword in active_keywords:
        if keyword in internal_lower:
            return 'active', f'internal matched: {keyword}'
    for keyword in closed_keywords:
        if keyword in internal_lower:
            return 'closed', f'internal matched: {keyword}'

    return 'closed', 'defaulted to closed'

def is_ticket_active(ticket: dict) -> bool:
    """Fast active check for pre-filtering."""
    status_obj = ticket.get('status', {})
    if isinstance(status_obj, dict):
        status = status_obj.get('name', '').lower()
        internal_status = status_obj.get('internal_name', '').lower()
    else:
        return False

    closed_keywords = ['closed', 'resolved', 'completed', 'cancelled', 'canceled']
    for keyword in closed_keywords:
        if keyword in status or keyword in internal_status:
            return False

    return True

def get_ms_value(obj: dict, key: str) -> Optional[int]:
    """Extract raw millisecond timestamp value from an SDP time field."""
    if not isinstance(obj, dict):
        return None
    field = obj.get(key, {})
    if isinstance(field, dict):
        try:
            val = field.get('value', 0)
            return int(val) if val else None
        except (ValueError, TypeError):
            return None
    return None

def process_ticket(
    ticket: dict,
    conversation_data: dict,
    is_archived: bool = False
) -> Tuple[dict, list]:
    """
    Process a single ticket.
    SLA data is read directly from the ticket object - no separate API call needed.
    """

    def safe_get(obj, key, default=''):
        if not obj:
            return default
        value = obj.get(key)
        return value if value is not None else default

    def safe_get_nested(obj, key, nested_key, default=''):
        parent = safe_get(obj, key, {})
        if not isinstance(parent, dict):
            return default
        value = parent.get(nested_key)
        return value if value is not None else default

    # --------------------------------------------------------
    # CORE FIELDS
    # --------------------------------------------------------
    internal_id = str(ticket.get('id', ''))

    display_id_obj = safe_get(ticket, 'display_id', {})
    display_id = str(display_id_obj.get('display_value', internal_id)) if isinstance(display_id_obj, dict) else str(display_id_obj or internal_id)

    subject = ticket.get('subject', 'No Subject') or 'No Subject'

    status_obj = safe_get(ticket, 'status', {})
    if isinstance(status_obj, dict):
        status = status_obj.get('name', 'Unknown') or 'Unknown'
        internal_status = status_obj.get('internal_name', status) or status
    else:
        status = 'Unknown'
        internal_status = 'Unknown'

    status_category, _ = determine_status_category(status, internal_status)

    created_time = safe_get_nested(ticket, 'created_time', 'display_value', 'Unknown')
    due_by_time = safe_get_nested(ticket, 'due_by_time', 'display_value', 'Not set')
    resolved_time = safe_get_nested(ticket, 'resolved_time', 'display_value', 'Not resolved')
    closed_time = safe_get_nested(ticket, 'closed_time', 'display_value', 'Not closed')

    # Assigned technician
    technician_obj = safe_get(ticket, 'technician', {})
    technician_name = (technician_obj.get('name', 'Unassigned') or 'Unassigned') if isinstance(technician_obj, dict) else 'Unassigned'
    technician_email = (technician_obj.get('email_id', '') or '') if isinstance(technician_obj, dict) else ''

    # Closed by - may differ from assigned technician if reassigned before closure
    closed_by_obj = safe_get(ticket, 'closed_by', {})
    if isinstance(closed_by_obj, dict) and closed_by_obj.get('name'):
        closed_by_name = closed_by_obj.get('name', 'Unknown')
    else:
        closed_by_name = technician_name

    requester_obj = safe_get(ticket, 'requester', {})
    requester_name = (requester_obj.get('name', 'Unknown') or 'Unknown') if isinstance(requester_obj, dict) else 'Unknown'
    requester_email = (requester_obj.get('email_id', '') or '') if isinstance(requester_obj, dict) else ''

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

    notifications = conversation_data.get('notifications', []) if conversation_data else []
    notes = conversation_data.get('notes', []) if conversation_data else []
    conversation_summary, notes_summary, total_exchanges = format_conversation_summary(notifications, notes)

    # --------------------------------------------------------
    # SLA EXTRACTION
    # All SLA data comes directly from the ticket object.
    # The /requests/{id}/sla endpoint returns the global policy
    # catalog (not per-ticket data) so we do not call it.
    # --------------------------------------------------------

    # SLA policy name derived from ticket priority
    # (priority maps 1:1 to SLA tier in SDP)
    priority_to_sla = {
        'high': 'HIGH',
        'low': 'LOW',
        'normal': 'NORMAL',
        'medium': 'NORMAL',
        'urgent': 'URGENT',
        'critical': 'URGENT',
    }
    sla_policy_name = priority_to_sla.get(priority.lower(), priority.upper())

    # SLA flags - read directly from ticket object
    def get_ticket_bool(key: str) -> bool:
        val = ticket.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, dict):
            return bool(val.get('value', False))
        if isinstance(val, str):
            return val.lower() in ['true', '1', 'yes']
        return False

    sla_violated = get_ticket_bool('is_overdue')
    first_response_overdue = get_ticket_bool('is_first_response_overdue')

    # Due times from ticket fields
    first_response_due = safe_get_nested(ticket, 'first_response_due_by_time', 'display_value', 'N/A')
    sla_resolution_due = due_by_time  # due_by_time IS the SLA resolution deadline

    if sla_violated:
        sla_status = 'Violated'
        stats['sla_violated'] += 1
    elif status_category == 'active':
        sla_status = 'Active'
    else:
        sla_status = 'Met'

    # --------------------------------------------------------
    # SLA DELTA CALCULATION
    # Positive = resolved early (good), Negative = resolved late (bad)
    # --------------------------------------------------------
    sla_days_delta = None
    sla_compliance_label = 'Unknown'

    resolved_ms = get_ms_value(ticket, 'resolved_time')
    sla_due_ms = get_ms_value(ticket, 'due_by_time')

    if resolved_ms and sla_due_ms and resolved_ms > 0 and sla_due_ms > 0:
        delta_ms = sla_due_ms - resolved_ms
        sla_days_delta = round(delta_ms / (1000 * 60 * 60 * 24), 2)
        stats['sla_delta_calculated'] += 1

        if sla_days_delta >= 0:
            sla_compliance_label = f"Met (resolved {sla_days_delta} days early)"
        else:
            sla_compliance_label = f"Violated (resolved {abs(sla_days_delta)} days late)"

    elif status_category == 'active':
        sla_compliance_label = "In Progress"
    elif sla_violated:
        sla_compliance_label = "Violated (exact delta unavailable)"
    else:
        sla_compliance_label = "Met (no timestamp available)"
    # --------------------------------------------------------

    # Build tags
    tags = [
        f"ticket-id:{display_id}",
        f"status:{status}",
        f"status-category:{status_category}",
        f"priority:{priority}",
        f"category:{category}",
        f"request-type:{request_type}",
        f"technician:{technician_name.lower().replace(' ', '-')}",
        f"closed-by:{closed_by_name.lower().replace(' ', '-')}",
        f"requester:{requester_name.lower().replace(' ', '-')}",
        f"conversations:{total_exchanges}",
        f"sla-status:{sla_status.lower()}",
        f"sla-policy:{sla_policy_name.lower()}",
    ]

    if notes:
        tags.append("has-notes")
    else:
        tags.append("no-notes")

    if sla_violated:
        tags.append("SLA-VIOLATED")
    if first_response_overdue:
        tags.append("FIRST-RESPONSE-OVERDUE")
    if sla_days_delta is not None and sla_days_delta >= 0:
        tags.append("SLA-MET")

    if is_archived:
        tags.append("ARCHIVED")
        tags.append("CLOSED-TICKET")
        stats['archived_count'] += 1
    elif status_category == 'active':
        tags.append("ACTIVE-TICKET")
        tags.append("IS-OPEN")
        tags.append("NOT-CLOSED")
        stats['active_count'] += 1
    else:
        tags.append("CLOSED-TICKET")
        stats['closed_count'] += 1

    if request_type == 'Incident':
        stats['incidents'] += 1
    else:
        stats['service_requests'] += 1

    stats['total_exchanges'] += total_exchanges

    content = {
        # Core
        "ticket_id": display_id,
        "internal_id": internal_id,
        "subject": subject,
        "status": status,
        "status_category": status_category,
        "priority": priority,
        "category": category,
        "subcategory": subcategory,
        "request_type": request_type,
        # Technician tracking
        "technician": technician_name,
        "technician_email": technician_email,
        "closed_by": closed_by_name,
        # Requester
        "requester": requester_name,
        "requester_email": requester_email,
        # Content
        "description": strip_html_enhanced(description),
        "resolution": strip_html_enhanced(resolution),
        # Timestamps
        "created_time": created_time,
        "due_by_time": due_by_time,
        "resolved_time": resolved_time,
        "closed_time": closed_time,
        # Conversations
        "conversation_count": total_exchanges,
        "conversation_summary": conversation_summary,
        "internal_notes_summary": notes_summary,
        # URL
        "source_url": f"https://{SDP_DOMAIN}/app/itdesk/requests/{internal_id}",
        # SLA (all from ticket object)
        "sla_policy": sla_policy_name,
        "sla_status": sla_status,
        "sla_violated": sla_violated,
        "first_response_due": first_response_due,
        "first_response_overdue": first_response_overdue,
        "sla_resolution_due": sla_resolution_due,
        "sla_days_delta": sla_days_delta,
        "sla_compliance_label": sla_compliance_label,
        # Archive
        "is_archived": is_archived,
    }

    return content, tags

# ============================================================
# SERVICEDESK API - TICKET FETCHING
# ============================================================
def fetch_tickets(last_sync: Optional[datetime] = None, include_archived: bool = False) -> List[dict]:
    """
    Fetch tickets from ServiceDesk Plus API.

    Live tickets:     GET /api/v3/requests
    Archived tickets: GET /api/v3/archive_requests  (separate endpoint)

    Args:
        last_sync:        Only fetch tickets updated since this time (live only)
        include_archived: Fetch from archive_requests endpoint
    """
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

    if include_archived:
        # Archived tickets live at a dedicated endpoint
        url = f"https://{SDP_DOMAIN}/api/v3/archive_requests"
        logger.info("📦 Fetching ARCHIVED tickets (archive_requests endpoint)...")

    elif last_sync:
        url = f"https://{SDP_DOMAIN}/api/v3/requests"
        last_sync_ms = int(last_sync.timestamp() * 1000)
        search_criteria = [{
            "field": "last_updated_time",
            "condition": "greater than",
            "value": str(last_sync_ms)
        }]
        logger.info(f"📊 Fetching tickets updated since {last_sync.strftime('%Y-%m-%d %H:%M:%S')}...")

    else:
        url = f"https://{SDP_DOMAIN}/api/v3/requests"
        logger.info("📊 Fetching all live tickets...")

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

    while True:
        list_info["list_info"]["start_index"] = (page - 1) * row_count + 1
        params = {"input_data": json.dumps(list_info)}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            stats['api_calls'] += 1

            data = response.json()

            # archive_requests returns 'archive_requests' key; requests returns 'requests'
            if include_archived:
                requests_data = data.get('archive_requests', data.get('requests', []))
            else:
                requests_data = data.get('requests', [])

            if not requests_data:
                break

            all_tickets.extend(requests_data)
            logger.info(f"  Page {page}: {len(requests_data)} tickets (total: {len(all_tickets)})")

            has_more = data.get('list_info', {}).get('has_more_rows', False)
            if not has_more or len(requests_data) < row_count:
                break

            page += 1
            time.sleep(0.1)

        except Exception as e:
            logger.error(f"Failed on page {page}: {e}")
            break

    label = " (archived)" if include_archived else ""
    logger.info(f"✓ Fetched {len(all_tickets)} tickets{label}")

    if include_archived:
        stats['archived_tickets_fetched'] = len(all_tickets)
    elif last_sync:
        stats['tickets_fetched'] = len(all_tickets)
        last_sync_ms = int(last_sync.timestamp() * 1000)
        new_count = sum(
            1 for t in all_tickets
            if isinstance(t.get('created_time'), dict)
            and int(t['created_time'].get('value', 0) or 0) > last_sync_ms
        )
        stats['new_tickets'] = new_count
        stats['updated_tickets'] = len(all_tickets) - new_count
        logger.info(f"  New: {new_count}, Updated: {stats['updated_tickets']}")
    else:
        stats['all_tickets_fetched'] = len(all_tickets)

    return all_tickets

# ============================================================
# BATCH PROCESSING
# ============================================================
def process_batch(
    batch_tickets: List[dict],
    batch_num: int,
    total_batches: int,
    is_incremental: bool,
    changed_ticket_ids: Set[str],
    processing_start_time: float,
    total_tickets_to_process: int,
    is_archive_batch: bool = False
):
    """
    Process and upload a batch of tickets.

    Routing:
      - is_archive_batch=True  → servicedesk only, tagged ARCHIVED
      - is_archive_batch=False → changed tickets to servicedesk, active to servicedesk-active
    """
    label = f"ARCHIVE BATCH {batch_num}/{total_batches}" if is_archive_batch else f"BATCH {batch_num}/{total_batches}"
    logger.info(f"\n{'=' * 60}")
    logger.info(f"{label} ({len(batch_tickets)} tickets)")
    logger.info(f"{'=' * 60}")

    batch_start = time.time()
    ensure_token_valid()

    conversations_map = fetch_conversations_parallel(batch_tickets)
    stats['conversations_fetched'] += len(batch_tickets)

    logger.info("  Processing tickets...")

    main_documents = []
    active_documents = []

    for ticket in batch_tickets:
        ticket_id = str(ticket.get('id', ''))
        display_id_obj = ticket.get('display_id', {})
        display_id = str(display_id_obj.get('display_value', ticket_id)) if isinstance(display_id_obj, dict) else str(display_id_obj or ticket_id)

        conv_data = conversations_map.get(ticket_id, {"count": 0, "notifications": [], "notes": []})

        content, tags = process_ticket(
            ticket,
            conv_data,
            is_archived=is_archive_batch
        )
        doc_id = f"ticket-{display_id}"
        doc_tuple = (doc_id, content, tags)

        if is_archive_batch:
            main_documents.append(doc_tuple)
        else:
            if ticket_id in changed_ticket_ids or not is_incremental:
                main_documents.append(doc_tuple)
            if content.get('status_category') == 'active':
                active_documents.append(doc_tuple)

    # Reporting
    if is_archive_batch:
        logger.info(f"  ✓ {len(main_documents)} archived → servicedesk")
        stats['tickets_processed'] += len(main_documents)
    elif is_incremental:
        logger.info(f"  ✓ {len(main_documents)} changed → servicedesk, {len(active_documents)} active → servicedesk-active")
        stats['tickets_processed'] += len(main_documents)
        stats['tickets_skipped'] += (len(batch_tickets) - len(main_documents))
    else:
        logger.info(f"  ✓ {len(main_documents)} → servicedesk, {len(active_documents)} active → servicedesk-active")
        stats['tickets_processed'] += len(main_documents)

    upload_start = time.time()

    if main_documents:
        main_uploaded, _, _ = upload_documents_parallel(main_documents, DUST_MAIN_DATA_SOURCE)
        if is_archive_batch:
            stats['archive_uploaded'] += main_uploaded
        else:
            stats['main_uploaded'] += main_uploaded
    else:
        logger.info(f"    servicedesk: nothing to upload this batch")

    if not is_archive_batch and active_documents:
        active_uploaded, _, _ = upload_documents_parallel(active_documents, DUST_ACTIVE_DATA_SOURCE)
        stats['active_uploaded'] += active_uploaded

    upload_elapsed = time.time() - upload_start
    logger.info(f"  ✓ Upload complete ({upload_elapsed:.1f}s)")

    overall_elapsed = time.time() - processing_start_time
    tickets_done = min(batch_num * BATCH_SIZE, total_tickets_to_process)
    rate = tickets_done / overall_elapsed if overall_elapsed > 0 else 0
    eta = (total_tickets_to_process - tickets_done) / rate if rate > 0 else 0
    logger.info(f"  📊 {tickets_done}/{total_tickets_to_process} ({rate:.1f}/sec, ~{eta/60:.1f} min remaining)")

# ============================================================
# MAIN SYNC LOGIC
# ============================================================
def run_sync():
    """Main sync orchestration - v4.1.4."""

    sync_start_time = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 60)
    logger.info(f"Start: {sync_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")
    logger.info("DATA SOURCES:")
    logger.info(f"  📦 {DUST_MAIN_DATA_SOURCE}:        Closed tickets + SDP archived tickets")
    logger.info(f"  ⚡ {DUST_ACTIVE_DATA_SOURCE}:  Active tickets, rebuilt every sync")
    logger.info("")
    logger.info("SUPPORTED QUERIES:")
    logger.info("  ✅ Tickets closed per technician")
    logger.info("  ✅ SLA compliance per technician (avg days early/late)")
    logger.info("  ✅ SLA violations by category, priority, technician")
    logger.info("  ✅ Full conversation history per ticket")
    logger.info("")

    refresh_token()

    last_sync, last_archive_sync = load_checkpoint()

    force_full_sync = check_force_full_sync()
    force_archive_sync = check_force_archive_sync()

    if force_full_sync:
        logger.info("  🔄 Force full sync requested")
        last_sync = None

    is_full_sync = (last_sync is None)
    sync_mode = "full" if is_full_sync else "incremental"

    run_archive = is_full_sync or force_archive_sync
    if not run_archive and last_archive_sync:
        days_since = (sync_start_time - last_archive_sync).days
        if days_since >= 7:
            logger.info(f"  📦 Archive sync due ({days_since} days since last run)")
            run_archive = True

    changed_ticket_ids: Set[str] = set()

    # --------------------------------------------------------
    # FULL SYNC
    # --------------------------------------------------------
    if is_full_sync:
        logger.info("")
        logger.info("=" * 60)
        logger.info("FULL SYNC: CLEANING BOTH DATA SOURCES")
        logger.info("=" * 60)

        logger.info("📦 Cleaning servicedesk...")
        main_docs = fetch_all_documents_from_dust(DUST_MAIN_DATA_SOURCE)
        if main_docs:
            stats['main_deleted'] = delete_documents_from_dust_parallel(DUST_MAIN_DATA_SOURCE, main_docs)

        logger.info("⚡ Cleaning servicedesk-active...")
        active_docs = fetch_all_documents_from_dust(DUST_ACTIVE_DATA_SOURCE)
        if active_docs:
            stats['active_deleted'] = delete_documents_from_dust_parallel(DUST_ACTIVE_DATA_SOURCE, active_docs)

        logger.info("✓ Both sources cleaned")
        logger.info("")

        all_tickets = fetch_tickets(last_sync=None)
        tickets_to_process = all_tickets
        for ticket in all_tickets:
            changed_ticket_ids.add(str(ticket.get('id', '')))

    # --------------------------------------------------------
    # INCREMENTAL SYNC
    # --------------------------------------------------------
    else:
        logger.info(f"  ✓ Incremental sync since {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("")
        logger.info("=" * 60)
        logger.info("INCREMENTAL SYNC: OPTIMIZED STRATEGY")
        logger.info("=" * 60)

        logger.info("⚡ Cleaning servicedesk-active...")
        active_docs = fetch_all_documents_from_dust(DUST_ACTIVE_DATA_SOURCE)
        if active_docs:
            stats['active_deleted'] = delete_documents_from_dust_parallel(DUST_ACTIVE_DATA_SOURCE, active_docs)

        logger.info("")

        changed_tickets = fetch_tickets(last_sync=last_sync)
        for ticket in changed_tickets:
            changed_ticket_ids.add(str(ticket.get('id', '')))

        logger.info("")

        logger.info("📊 Fetching all tickets to identify active...")
        all_tickets = fetch_tickets(last_sync=None)

        active_tickets = [t for t in all_tickets if is_ticket_active(t)]

        tickets_to_process_dict = {}
        for ticket in changed_tickets:
            tickets_to_process_dict[str(ticket.get('id', ''))] = ticket
        for ticket in active_tickets:
            tid = str(ticket.get('id', ''))
            if tid not in tickets_to_process_dict:
                tickets_to_process_dict[tid] = ticket

        tickets_to_process = list(tickets_to_process_dict.values())
        tickets_skipped = len(all_tickets) - len(tickets_to_process)

        logger.info("")
        logger.info(f"  Total in SDP:          {len(all_tickets)}")
        logger.info(f"  Changed:               {len(changed_tickets)}")
        logger.info(f"  Active:                {len(active_tickets)}")
        logger.info(f"  To process:            {len(tickets_to_process)}")
        logger.info(f"  ⚡ Skipped:             {tickets_skipped} unchanged closed tickets")
        logger.info("=" * 60)

    # --------------------------------------------------------
    # PROCESS LIVE TICKETS
    # --------------------------------------------------------
    if tickets_to_process:
        total_batches = (len(tickets_to_process) + BATCH_SIZE - 1) // BATCH_SIZE
        est = total_batches * (1.5 if is_full_sync else 0.2)

        logger.info("")
        logger.info(f"Processing {len(tickets_to_process)} tickets in {total_batches} batches (~{int(est)} min)")

        processing_start = time.time()
        for i in range(0, len(tickets_to_process), BATCH_SIZE):
            batch = tickets_to_process[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            process_batch(
                batch,
                batch_num,
                total_batches,
                not is_full_sync,
                changed_ticket_ids,
                processing_start,
                len(tickets_to_process),
                is_archive_batch=False
            )
    else:
        logger.info("No live tickets to process")

    # --------------------------------------------------------
    # ARCHIVE SYNC → uploads to servicedesk via archive_requests
    # --------------------------------------------------------
    archive_sync_timestamp = None

    if run_archive:
        logger.info("")
        logger.info("=" * 60)
        logger.info("ARCHIVE SYNC → uploading to servicedesk")
        logger.info("  Endpoint: /api/v3/archive_requests")
        logger.info("=" * 60)

        archive_sync_timestamp = datetime.now(timezone.utc)
        archived_tickets = fetch_tickets(last_sync=None, include_archived=True)

        if archived_tickets:
            total_archive_batches = (len(archived_tickets) + BATCH_SIZE - 1) // BATCH_SIZE
            logger.info(f"  Processing {len(archived_tickets)} archived tickets in {total_archive_batches} batches...")

            archive_start = time.time()
            for i in range(0, len(archived_tickets), BATCH_SIZE):
                batch = archived_tickets[i:i + BATCH_SIZE]
                batch_num = (i // BATCH_SIZE) + 1
                process_batch(
                    batch,
                    batch_num,
                    total_archive_batches,
                    is_incremental=False,
                    changed_ticket_ids=set(),
                    processing_start_time=archive_start,
                    total_tickets_to_process=len(archived_tickets),
                    is_archive_batch=True
                )

            logger.info(f"  ✓ Archive sync complete - {stats['archive_uploaded']} tickets → servicedesk")
        else:
            logger.info("  No archived tickets found")

    else:
        days_info = f"last ran {(sync_start_time - last_archive_sync).days} days ago" if last_archive_sync else "never run"
        logger.info("")
        logger.info(f"⏭️  Archive sync skipped ({days_info}) - use FORCE_ARCHIVE_SYNC=true to run")

    # --------------------------------------------------------
    # SAVE CHECKPOINT
    # --------------------------------------------------------
    save_checkpoint(sync_start_time, archive_timestamp=archive_sync_timestamp)

    # --------------------------------------------------------
    # FINAL SUMMARY
    # --------------------------------------------------------
    sync_end_time = datetime.now(timezone.utc)
    execution_minutes = (sync_end_time - sync_start_time).total_seconds() / 60

    logger.info("")
    logger.info(f"⏱️  Total execution time: {execution_minutes:.1f} minutes")
    logger.info("")
    logger.info("=" * 60)
    logger.info("SYNC COMPLETE - FINAL SUMMARY")
    logger.info("=" * 60)
    logger.info("")
    logger.info("📊 TICKET STATISTICS:")
    logger.info(f"  Sync Mode:              {sync_mode}")
    logger.info(f"  Live Fetched:           {stats.get('all_tickets_fetched', stats.get('tickets_fetched', 0))}")
    logger.info(f"  Archived Fetched:       {stats['archived_tickets_fetched']}")
    logger.info(f"  Total Processed:        {stats['tickets_processed']}")
    if stats['tickets_skipped'] > 0:
        logger.info(f"  Skipped (unchanged):    {stats['tickets_skipped']}")

    logger.info("")
    logger.info("📤 UPLOADS:")
    logger.info(f"  servicedesk (live):     {stats['main_uploaded']}")
    logger.info(f"  servicedesk (archived): {stats['archive_uploaded']}")
    logger.info(f"  servicedesk-active:     {stats['active_uploaded']}")
    logger.info(f"  Failures:               {stats['upload_failures']}")

    logger.info("")
    logger.info("🔒 SLA:")
    logger.info(f"  SLA Deltas Calculated:  {stats['sla_delta_calculated']}")
    logger.info(f"  SLA Violations:         {stats['sla_violated']}")

    logger.info("")
    logger.info("📈 BREAKDOWN:")
    logger.info(f"  Active:                 {stats['active_count']}")
    logger.info(f"  Closed:                 {stats['closed_count']}")
    logger.info(f"  Archived:               {stats['archived_count']}")
    logger.info(f"  Incidents:              {stats['incidents']}")
    logger.info(f"  Service Requests:       {stats['service_requests']}")

    logger.info("")
    logger.info("💬 CONVERSATIONS:")
    logger.info(f"  Fetched:                {stats['conversations_fetched']}")
    logger.info(f"  Total Exchanges:        {stats['total_exchanges']}")
    if stats['conversations_fetched'] > 0:
        logger.info(f"  Avg per Ticket:         {stats['total_exchanges'] / stats['conversations_fetched']:.1f}")

    logger.info("")
    logger.info("💬 CONVERSATION DIAGNOSTICS:")
    logger.info(f"  Notif successes:        {CONVERSATION_DIAGNOSTICS['individual_fetch_success']}")
    logger.info(f"  Notif empty:            {CONVERSATION_DIAGNOSTICS['individual_fetch_empty']}")
    logger.info(f"  Notif errors:           {CONVERSATION_DIAGNOSTICS['individual_fetch_error']}")
    logger.info(f"  Total notifications:    {CONVERSATION_DIAGNOSTICS['total_notifications']}")
    logger.info(f"  Total notes:            {CONVERSATION_DIAGNOSTICS['total_notes']}")

    if stats['main_deleted'] > 0 or stats['active_deleted'] > 0:
        logger.info("")
        logger.info("🧹 CLEANUP:")
        logger.info(f"  servicedesk deleted:    {stats['main_deleted']}")
        logger.info(f"  servicedesk-active del: {stats['active_deleted']}")

    logger.info("")
    logger.info("🔧 API:")
    logger.info(f"  Total Calls:            {stats['api_calls']}")
    if stats['tickets_processed'] > 0:
        elapsed_sec = (sync_end_time - sync_start_time).total_seconds()
        logger.info(f"  Processing Rate:        {stats['tickets_processed'] / elapsed_sec:.1f} tickets/sec")

    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✓ ServiceDesk Intelligence Suite {VERSION}")
    logger.info("  📦 servicedesk  = closed + archived (SLA from ticket object)")
    logger.info("  ⚡ servicedesk-active = open tickets")
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
            logger.error(f"Missing required env vars: {', '.join(missing)}")
            sys.exit(1)

        logger.info("✓ Configuration loaded")
        logger.info(f"  Workspace:              {DUST_WORKSPACE_ID}")
        logger.info(f"  Main Data Source:       {DUST_MAIN_DATA_SOURCE}  (closed + archived)")
        logger.info(f"  Active Data Source:     {DUST_ACTIVE_DATA_SOURCE}  (active only)")
        logger.info(f"  SDP Domain:             {SDP_DOMAIN}")
        logger.info(f"  Version:                {VERSION}")
        logger.info(f"  Batch Size:             {BATCH_SIZE}")
        logger.info(f"  Conversation Workers:   {MAX_CONCURRENT_CONVERSATIONS}")
        logger.info(f"  Notif Workers:          {MAX_CONCURRENT_INDIVIDUAL_NOTIFS}")
        logger.info(f"  Upload Workers:         {MAX_CONCURRENT_UPLOADS}")
        logger.info("")

        run_sync()

    except KeyboardInterrupt:
        logger.info("\nSync interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
