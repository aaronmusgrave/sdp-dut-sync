#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v4.0.1-DEBUG-INCREMENTAL
DEBUG: Find why incremental sync fetches all tickets instead of filtering

Added debugging:
- Log exact API request payload
- Log API response metadata
- Test multiple search criteria formats
- Validate field names and values
- Show what tickets are actually returned vs expected
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

VERSION = "v4.0.1-DEBUG-INCREMENTAL"

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
    'content_empty': 0
}

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
# DEBUG: INCREMENTAL SYNC TESTING
# ============================================================
def test_incremental_filters(last_sync: datetime) -> List[dict]:
    """
    DEBUG: Test multiple search criteria formats to find what works.
    """
    ensure_token_valid()
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.manageengine.sdp.v3+json"
    }
    
    url = f"https://{SDP_DOMAIN}/api/v3/requests"
    last_sync_ms = int(last_sync.timestamp() * 1000)
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("DEBUG: TESTING INCREMENTAL FILTER FORMATS")
    logger.info("=" * 80)
    logger.info(f"Last sync: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Last sync (ms): {last_sync_ms}")
    logger.info("")
    
    # Test Format 1: Original OR logic
    logger.info("TEST 1: OR logic with created_time and last_updated_time")
    search_criteria_1 = [
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
    
    list_info_1 = {
        "list_info": {
            "row_count": 10,
            "start_index": 1,
            "search_criteria": search_criteria_1
        }
    }
    
    logger.info(f"Request payload:")
    logger.info(json.dumps(list_info_1, indent=2))
    
    try:
        params = {"input_data": json.dumps(list_info_1)}
        response = requests.get(url, headers=headers, params=params, timeout=60)
        logger.info(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            requests_data = data.get('requests', [])
            list_info_resp = data.get('list_info', {})
            
            logger.info(f"✓ Returned {len(requests_data)} tickets")
            logger.info(f"Response list_info: {json.dumps(list_info_resp, indent=2)}")
            
            if requests_data:
                logger.info(f"Sample ticket dates:")
                for i, ticket in enumerate(requests_data[:3]):
                    created_time = ticket.get('created_time', {})
                    created_val = created_time.get('value', 0) if isinstance(created_time, dict) else 0
                    created_dt = datetime.fromtimestamp(int(created_val)/1000, tz=timezone.utc) if created_val else None
                    
                    logger.info(f"  Ticket {i+1}:")
                    logger.info(f"    created_time: {created_dt}")
                    logger.info(f"    After checkpoint? {created_dt > last_sync if created_dt else 'N/A'}")
        else:
            logger.error(f"❌ Failed: {response.text[:500]}")
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    logger.info("")
    
    # Test Format 2: Only last_updated_time
    logger.info("TEST 2: Only last_updated_time filter")
    search_criteria_2 = [
        {
            "field": "last_updated_time",
            "condition": "greater than",
            "value": str(last_sync_ms)
        }
    ]
    
    list_info_2 = {
        "list_info": {
            "row_count": 10,
            "start_index": 1,
            "search_criteria": search_criteria_2
        }
    }
    
    logger.info(f"Request payload:")
    logger.info(json.dumps(list_info_2, indent=2))
    
    try:
        params = {"input_data": json.dumps(list_info_2)}
        response = requests.get(url, headers=headers, params=params, timeout=60)
        logger.info(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            requests_data = data.get('requests', [])
            list_info_resp = data.get('list_info', {})
            
            logger.info(f"✓ Returned {len(requests_data)} tickets")
            logger.info(f"Response list_info: {json.dumps(list_info_resp, indent=2)}")
        else:
            logger.error(f"❌ Failed: {response.text[:500]}")
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    logger.info("")
    
    # Test Format 3: Try different field name
    logger.info("TEST 3: Try 'modified_time' field")
    search_criteria_3 = [
        {
            "field": "modified_time",
            "condition": "greater than",
            "value": str(last_sync_ms)
        }
    ]
    
    list_info_3 = {
        "list_info": {
            "row_count": 10,
            "start_index": 1,
            "search_criteria": search_criteria_3
        }
    }
    
    logger.info(f"Request payload:")
    logger.info(json.dumps(list_info_3, indent=2))
    
    try:
        params = {"input_data": json.dumps(list_info_3)}
        response = requests.get(url, headers=headers, params=params, timeout=60)
        logger.info(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            requests_data = data.get('requests', [])
            logger.info(f"✓ Returned {len(requests_data)} tickets")
        else:
            logger.error(f"❌ Failed: {response.text[:500]}")
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    logger.info("")
    
    # Test Format 4: No search criteria (baseline)
    logger.info("TEST 4: No filters (baseline - should return all)")
    list_info_4 = {
        "list_info": {
            "row_count": 10,
            "start_index": 1
        }
    }
    
    try:
        params = {"input_data": json.dumps(list_info_4)}
        response = requests.get(url, headers=headers, params=params, timeout=60)
        logger.info(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            requests_data = data.get('requests', [])
            logger.info(f"✓ Returned {len(requests_data)} tickets (baseline)")
        else:
            logger.error(f"❌ Failed: {response.text[:500]}")
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("DEBUG TESTS COMPLETE")
    logger.info("=" * 80)
    logger.info("")
    logger.info("STOPPING HERE - Review debug output above")
    logger.info("The script will NOT proceed with full sync to avoid wasting time.")
    logger.info("")
    
    # Return empty list to stop processing
    return []

# ============================================================
# TICKET FETCHING (ORIGINAL - FOR REFERENCE)
# ============================================================
def fetch_tickets(last_sync: Optional[datetime] = None) -> List[dict]:
    """
    DEBUG VERSION: Only runs filter tests, doesn't fetch all tickets.
    """
    
    if last_sync:
        logger.info("=" * 80)
        logger.info("DEBUG MODE: Testing incremental filter formats")
        logger.info("=" * 80)
        return test_incremental_filters(last_sync)
    else:
        logger.info("=" * 80)
        logger.info("FULL SYNC: Skipping for debug mode")
        logger.info("=" * 80)
        logger.info("To run full sync, remove checkpoint file and disable debug mode")
        return []

# Stub remaining functions for clean execution
def strip_html_enhanced(html_content: str) -> str:
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
        return html_content

def run_sync():
    """Debug version - only tests filters."""
    
    sync_start_time = datetime.now(timezone.utc)
    
    logger.info("=" * 80)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 80)
    logger.info("DEBUG MODE: Testing incremental sync filters")
    logger.info("")
    
    refresh_token()
    
    last_sync = load_checkpoint()
    
    force_full_sync = os.getenv('FORCE_FULL_SYNC', '').lower() in ['true', '1', 'yes']
    if force_full_sync:
        logger.info("  🔄 Force full sync requested - but skipping in debug mode")
        last_sync = None
    
    if not last_sync:
        logger.info("")
        logger.info("No checkpoint found - need a checkpoint to test incremental sync")
        logger.info("Run a full sync first to create a checkpoint, then run debug mode")
        return
    
    logger.info(f"  Testing incremental filters since: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")
    
    tickets = fetch_tickets(last_sync)
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("DEBUG SESSION COMPLETE")
    logger.info("=" * 80)
    logger.info("")
    logger.info("Review the test results above to determine:")
    logger.info("  1. Which filter format works (if any)")
    logger.info("  2. Whether the API supports incremental filtering")
    logger.info("  3. What the correct field names and format should be")

if __name__ == "__main__":
    try:
        logger.info("=" * 80)
        logger.info("VALIDATING ENVIRONMENT VARIABLES")
        logger.info("=" * 80)
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
        logger.info(f"  Version: {VERSION}")
        logger.info("")
        
        run_sync()
        
    except KeyboardInterrupt:
        logger.info("\n\nDebug interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
