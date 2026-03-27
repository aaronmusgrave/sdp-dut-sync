#!/usr/bin/env python3
"""
ServiceDesk Plus CSV Exporter v1.1
Export ServiceDesk Plus tickets to CSV with full conversation content in separate columns
Based on ServiceDesk Intelligence Suite v4.0.6
"""

import os
import sys
import json
import time
import requests
import logging
import csv
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Load and clean all secrets
SDP_DOMAIN = clean_secret(os.getenv('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_secret(os.getenv('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_secret(os.getenv('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_secret(os.getenv('SDP_REFRESH_TOKEN', ''))

# CSV Export Settings
EXPORT_DAYS_BACK = int(os.getenv('EXPORT_DAYS_BACK', '365'))
OUTPUT_FILE = os.getenv('OUTPUT_FILE', 'servicedesk_export.csv')
MAX_CONVERSATION_COLUMNS = int(os.getenv('MAX_CONVERSATION_COLUMNS', '50'))  # Max conversation columns

BATCH_SIZE = 100
MAX_CONCURRENT_CONVERSATIONS = 10
MAX_CONCURRENT_INDIVIDUAL_NOTIFS = 5

VERSION = "v1.1-CSV-EXPORT-DETAILED"

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

stats = {
    'tickets_fetched': 0,
    'tickets_processed': 0,
    'conversations_fetched': 0,
    'total_exchanges': 0,
    'api_calls': 0,
    'max_conversations_per_ticket': 0
}

# ============================================================
# TOKEN MANAGEMENT
# ============================================================
def refresh_token():
    """Refresh OAuth token and update expiry."""
    global TOKEN, TOKEN_EXPIRY
    
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
            logger.info(f"Token expiring in {int(time_remaining/60)} minutes - refreshing...")
            refresh_token()

# ============================================================
# HTML STRIPPING (ENHANCED)
# ============================================================
def strip_html_enhanced(html_content: str) -> str:
    """Enhanced HTML stripping with better tag removal and entity decoding."""
    if not html_content:
        return ""
    
    try:
        # First pass: Remove script and style tags with their content
        text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        
        # Second pass: Remove all HTML tags (including attributes)
        text = re.sub(r'<[^>]+>', '', text)
        
        # Third pass: Handle self-closing tags
        text = re.sub(r'<[^>]*/?>', '', text)
        
        # IMPORTANT: Decode HTML entities MULTIPLE times (some are double-encoded)
        for _ in range(3):  # Decode up to 3 times for nested encoding
            decoded = unescape(text)
            if decoded == text:  # No more changes
                break
            text = decoded
        
        # Additional common entity replacements
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')
        text = text.replace('&#39;', "'")
        text = text.replace('&apos;', "'")
        
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text)
        
        # Clean up line breaks
        text = text.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
        
        # Remove any remaining HTML-like artifacts
        text = re.sub(r'&[a-zA-Z0-9#]+;?', '', text)
        
        # Final trim
        text = text.strip()
        
        # If stripping resulted in empty string but original had content
        if not text and html_content and len(html_content.strip()) > 0:
            fallback = re.sub(r'[<>]', '', html_content)
            fallback = re.sub(r'&[a-zA-Z0-9#]+;?', '', fallback)
            return fallback.strip()
        
        return text
        
    except Exception as e:
        logger.debug(f"HTML stripping failed: {e}")
        try:
            fallback = re.sub(r'[<>]', '', html_content)
            fallback = re.sub(r'&[a-zA-Z0-9#]+;?', '', fallback)
            return fallback.strip()
        except:
            return html_content.strip()

# ============================================================
# SERVICEDESK API - CONVERSATIONS
# ============================================================
def fetch_individual_notification(ticket_id: str, notification_id: str) -> Optional[dict]:
    """Fetch individual notification to get FULL email content."""
    global TOKEN
    
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
            return data.get('notification', data)
        return None
            
    except Exception as e:
        logger.debug(f"Failed to fetch notification {notification_id}: {e}")
        return None

def fetch_notifications_with_content(ticket_id: str) -> Tuple[List[dict], List[dict]]:
    """Fetch notifications with FULL content."""
    global TOKEN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    notifications_with_content = []
    notes = []
    
    # Fetch notification list
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications"
        response = requests.get(url, headers=headers, timeout=30)
        stats['api_calls'] += 1
        
        if response.status_code == 200:
            data = response.json()
            notification_list = data.get('notifications', [])
            
            if notification_list:
                # Fetch each notification individually for full content
                with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_INDIVIDUAL_NOTIFS) as executor:
                    future_to_id = {
                        executor.submit(fetch_individual_notification, ticket_id, notif.get('id')): notif
                        for notif in notification_list
                        if notif.get('id')
                    }
                    
                    for future in as_completed(future_to_id):
                        try:
                            full_notif = future.result()
                            if full_notif:
                                notifications_with_content.append(full_notif)
                        except Exception as e:
                            logger.debug(f"Failed to fetch notification: {e}")
    except Exception as e:
        logger.debug(f"Failed to fetch notification list for {ticket_id}: {e}")
    
    # Fetch notes
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notes"
        response = requests.get(url, headers=headers, timeout=30)
        stats['api_calls'] += 1
        
        if response.status_code == 200:
            data = response.json()
            notes = data.get('notes', [])
    except Exception as e:
        logger.debug(f"Failed to fetch notes for {ticket_id}: {e}")
    
    return notifications_with_content, notes

def fetch_conversations_parallel(tickets: List[dict]) -> Dict[str, dict]:
    """Fetch conversations in parallel for multiple tickets."""
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
                
                conversations_map[ticket_id] = {
                    "count": len(notifications) + len(notes),
                    "notifications": notifications,
                    "notes": notes
                }
                
                stats['total_exchanges'] += len(notifications) + len(notes)
                
                # Track max conversations
                if len(notifications) + len(notes) > stats['max_conversations_per_ticket']:
                    stats['max_conversations_per_ticket'] = len(notifications) + len(notes)
                
            except Exception as e:
                conversations_map[ticket_id] = {
                    "count": 0,
                    "notifications": [],
                    "notes": []
                }
                logger.debug(f"Failed to fetch conversations for {ticket_id}: {e}")
    
    elapsed = time.time() - start_time
    logger.info(f"  ✓ Fetched conversations ({elapsed:.1f}s, {len(tickets)/elapsed:.1f}/sec)")
    
    return conversations_map

# ============================================================
# CONVERSATION PARSING - DETAILED
# ============================================================
def parse_conversations_detailed(notifications: List[dict], notes: List[dict]) -> List[dict]:
    """Parse conversations into detailed list with timestamp, sender, type, content."""
    all_exchanges = []
    
    # Process notifications (emails)
    for notif in notifications:
        time_obj = notif.get('time', {})
        if isinstance(time_obj, dict):
            timestamp = time_obj.get('display_value', 'Unknown')
            timestamp_ms = time_obj.get('value', 0)
        else:
            timestamp = str(time_obj)
            timestamp_ms = 0
        
        sender_obj = notif.get('sender', {})
        sender_name = sender_obj.get('name', 'Unknown') if isinstance(sender_obj, dict) else 'Unknown'
        sender_email = sender_obj.get('email_id', '') if isinstance(sender_obj, dict) else ''
        
        description = notif.get('description', '')
        content = strip_html_enhanced(description)
        
        if content and len(content.strip()) > 0:
            all_exchanges.append({
                'timestamp': timestamp,
                'timestamp_ms': timestamp_ms,
                'sender': sender_name,
                'sender_email': sender_email,
                'type': 'Email',
                'content': content
            })
    
    # Process notes (internal)
    for note in notes:
        created_time_obj = note.get('created_time', {})
        if isinstance(created_time_obj, dict):
            timestamp = created_time_obj.get('display_value', 'Unknown')
            timestamp_ms = created_time_obj.get('value', 0)
        else:
            timestamp = str(created_time_obj)
            timestamp_ms = 0
        
        created_by_obj = note.get('created_by', {})
        creator_name = created_by_obj.get('name', 'Unknown') if isinstance(created_by_obj, dict) else 'Unknown'
        creator_email = created_by_obj.get('email_id', '') if isinstance(created_by_obj, dict) else ''
        
        description = note.get('description', '')
        content = strip_html_enhanced(description)
        
        if content and len(content.strip()) > 0:
            all_exchanges.append({
                'timestamp': timestamp,
                'timestamp_ms': timestamp_ms,
                'sender': creator_name,
                'sender_email': creator_email,
                'type': 'Note',
                'content': content
            })
    
    # Sort by timestamp
    all_exchanges.sort(key=lambda x: x.get('timestamp_ms', 0))
    
    return all_exchanges

# ============================================================
# TICKET PROCESSING - WITH DETAILED CONVERSATIONS
# ============================================================
def process_ticket_for_csv(ticket: dict, conversation_data: dict) -> dict:
    """Process ticket and return CSV row data with separate conversation columns."""
    
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
    
    # Extract ticket data
    ticket_id = str(ticket.get('id', ''))
    
    display_id_obj = safe_get(ticket, 'display_id', {})
    display_id = str(display_id_obj.get('display_value', ticket_id)) if isinstance(display_id_obj, dict) else str(display_id_obj)
    
    subject = ticket.get('subject', 'No Subject') or 'No Subject'
    
    status_obj = safe_get(ticket, 'status', {})
    status = status_obj.get('name', 'Unknown') if isinstance(status_obj, dict) else 'Unknown'
    
    created_time = safe_get_nested(ticket, 'created_time', 'display_value', 'Unknown')
    
    technician_obj = safe_get(ticket, 'technician', {})
    technician_name = technician_obj.get('name', 'Unassigned') if isinstance(technician_obj, dict) else 'Unassigned'
    
    requester_obj = safe_get(ticket, 'requester', {})
    requester_name = requester_obj.get('name', 'Unknown') if isinstance(requester_obj, dict) else 'Unknown'
    
    priority_obj = safe_get(ticket, 'priority', {})
    priority = priority_obj.get('name', 'Normal') if isinstance(priority_obj, dict) else 'Normal'
    
    category_obj = safe_get(ticket, 'category', {})
    category = category_obj.get('name', 'Uncategorized') if isinstance(category_obj, dict) else 'Uncategorized'
    
    description = ticket.get('description', 'No description') or 'No description'
    description_clean = strip_html_enhanced(description)
    
    # Parse conversations into detailed structure
    notifications = conversation_data.get('notifications', [])
    notes = conversation_data.get('notes', [])
    conversations = parse_conversations_detailed(notifications, notes)
    
    # Build row with base fields
    row = {
        'ticket_number': display_id,
        'date': created_time,
        'subject': subject,
        'status': status,
        'priority': priority,
        'category': category,
        'technician': technician_name,
        'requester': requester_name,
        'initial_request': description_clean,
        'conversation_count': len(conversations)
    }
    
    # Add conversation columns (up to MAX_CONVERSATION_COLUMNS)
    for i, conv in enumerate(conversations[:MAX_CONVERSATION_COLUMNS], start=1):
        row[f'conv_{i}_timestamp'] = conv.get('timestamp', '')
        row[f'conv_{i}_sender'] = conv.get('sender', '')
        row[f'conv_{i}_type'] = conv.get('type', '')
        row[f'conv_{i}_content'] = conv.get('content', '')
    
    return row

# ============================================================
# SERVICEDESK API - TICKET FETCHING
# ============================================================
def fetch_tickets(days_back: int = 365) -> List[dict]:
    """Fetch tickets from ServiceDesk Plus API."""
    ensure_token_valid()
    
    all_tickets = []
    page = 1
    row_count = 100
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.manageengine.sdp.v3+json"
    }
    
    # Calculate date range
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=days_back)
    from_date_ms = int(from_date.timestamp() * 1000)
    
    logger.info(f"📊 Fetching tickets from {from_date.strftime('%Y-%m-%d')} to {to_date.strftime('%Y-%m-%d')}")
    
    list_info = {
        "list_info": {
            "row_count": row_count,
            "start_index": 1,
            "get_total_count": True,
            "sort_field": "created_time",
            "sort_order": "desc",
            "search_criteria": [
                {
                    "field": "created_time",
                    "condition": "greater than",
                    "value": str(from_date_ms)
                }
            ]
        }
    }
    
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
            
            if not has_more or len(requests_data) < row_count:
                break
            
            page += 1
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Failed to fetch tickets on page {page}: {e}")
            break
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets total")
    stats['tickets_fetched'] = len(all_tickets)
    
    return all_tickets

# ============================================================
# CSV EXPORT - WITH DYNAMIC COLUMNS
# ============================================================
def export_to_csv(tickets: List[dict], output_file: str):
    """Export tickets to CSV file with detailed conversation columns."""
    
    logger.info(f"\n{'=' * 60}")
    logger.info(f"EXPORTING TO CSV: {output_file}")
    logger.info(f"{'=' * 60}")
    
    csv_rows = []
    
    # Process in batches for conversation fetching
    total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, len(tickets), BATCH_SIZE):
        batch = tickets[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        
        logger.info(f"\nBatch {batch_num}/{total_batches} ({len(batch)} tickets)")
        
        # Fetch conversations for batch
        conversations_map = fetch_conversations_parallel(batch)
        stats['conversations_fetched'] += len(batch)
        
        # Process each ticket
        for ticket in batch:
            ticket_id = str(ticket.get('id', ''))
            conv_data = conversations_map.get(ticket_id, {"count": 0, "notifications": [], "notes": []})
            
            row_data = process_ticket_for_csv(ticket, conv_data)
            csv_rows.append(row_data)
            stats['tickets_processed'] += 1
    
    # Build dynamic fieldnames
    base_fields = [
        'ticket_number',
        'date',
        'subject',
        'status',
        'priority',
        'category',
        'technician',
        'requester',
        'initial_request',
        'conversation_count'
    ]
    
    # Add conversation columns
    conversation_fields = []
    for i in range(1, MAX_CONVERSATION_COLUMNS + 1):
        conversation_fields.extend([
            f'conv_{i}_timestamp',
            f'conv_{i}_sender',
            f'conv_{i}_type',
            f'conv_{i}_content'
        ])
    
    fieldnames = base_fields + conversation_fields
    
    # Write to CSV
    logger.info(f"\n✍️  Writing {len(csv_rows)} rows to {output_file}...")
    logger.info(f"  Max conversations per ticket: {stats['max_conversations_per_ticket']}")
    logger.info(f"  Using {MAX_CONVERSATION_COLUMNS} conversation column sets")
    
    with open(output_file, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(csv_rows)
    
    logger.info(f"✓ CSV export complete: {output_file}")
    logger.info(f"  Total rows: {len(csv_rows)}")
    logger.info(f"  Total columns: {len(fieldnames)}")
    
    # Calculate file size
    file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
    logger.info(f"  File size: {file_size_mb:.2f} MB")

# ============================================================
# MAIN
# ============================================================
def main():
    """Main execution."""
    
    start_time = datetime.now(timezone.utc)
    
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK PLUS CSV EXPORTER {VERSION}")
    logger.info("=" * 60)
    logger.info(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")
    
    # Validate environment
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN']
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    logger.info("✓ Configuration:")
    logger.info(f"  SDP Domain: {SDP_DOMAIN}")
    logger.info(f"  Export Days Back: {EXPORT_DAYS_BACK}")
    logger.info(f"  Output File: {OUTPUT_FILE}")
    logger.info(f"  Max Conversation Columns: {MAX_CONVERSATION_COLUMNS}")
    logger.info("")
    
    # Fetch tickets
    tickets = fetch_tickets(days_back=EXPORT_DAYS_BACK)
    
    if not tickets:
        logger.info("No tickets found to export")
        return
    
    # Export to CSV
    export_to_csv(tickets, OUTPUT_FILE)
    
    # Final summary
    end_time = datetime.now(timezone.utc)
    elapsed = (end_time - start_time).total_seconds() / 60
    
    logger.info("")
    logger.info("=" * 60)
    logger.info("EXPORT COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Tickets Fetched: {stats['tickets_fetched']}")
    logger.info(f"  Tickets Processed: {stats['tickets_processed']}")
    logger.info(f"  Total Exchanges: {stats['total_exchanges']}")
    logger.info(f"  Max Conversations: {stats['max_conversations_per_ticket']}")
    logger.info(f"  API Calls: {stats['api_calls']}")
    logger.info(f"  Execution Time: {elapsed:.1f} minutes")
    logger.info(f"  Output: {OUTPUT_FILE}")
    logger.info("=" * 60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\nExport interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
