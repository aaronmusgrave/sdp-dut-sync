#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.5.3-TEST-INDIVIDUAL-CONVERSATIONS
TEST: Fetch individual conversations by ID to check for content

This is a TEST version that will:
1. Fetch conversation list (as before)
2. Take first 3 conversation IDs
3. Fetch EACH conversation individually
4. Log complete response to see if content exists
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
BATCH_SIZE = 10  # Reduced for testing
MAX_CONCURRENT_CONVERSATIONS = 5  # Reduced for testing
MAX_CONCURRENT_UPLOADS = 2
UPLOAD_DELAY = 0.3

# Debug mode
DEBUG_MODE = True
DEBUG_INDIVIDUAL_CONVS = True  # NEW: Test individual conversation fetching
MAX_INDIVIDUAL_TESTS = 3  # Test first 3 conversations

# Version
VERSION = "v3.5.3-TEST-INDIVIDUAL-CONVERSATIONS"

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

# Conversation diagnostics
CONVERSATION_DIAGNOSTICS = {
    'conversations_list_success': 0,
    'conversations_individual_success': 0,
    'conversations_individual_with_content': 0,
    'conversations_individual_empty': 0,
    'conversations_individual_error': 0,
    'notes_success': 0,
    'total_messages': 0,
    'content_extracted_success': 0,
    'content_extracted_empty': 0
}

# Statistics
stats = {
    'tickets_fetched': 0,
    'tickets_processed': 0,
    'api_calls': 0
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
    
    logger.info("  No checkpoint found - will perform test sync")
    return None

# ============================================================
# TEST: FETCH INDIVIDUAL CONVERSATION BY ID
# ============================================================
def fetch_individual_conversation(ticket_id: str, conversation_id: str, conv_number: int) -> Optional[dict]:
    """
    TEST: Fetch a single conversation by ID to check for content.
    This is the key test to see if content exists at the individual level.
    """
    global TOKEN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Try the individual conversation endpoint
    url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/conversations/{conversation_id}"
    
    try:
        logger.info("=" * 80)
        logger.info(f"🔍 TEST: Fetching INDIVIDUAL Conversation #{conv_number}")
        logger.info("=" * 80)
        logger.info(f"Ticket ID: {ticket_id}")
        logger.info(f"Conversation ID: {conversation_id}")
        logger.info(f"Endpoint: {url}")
        logger.info("")
        
        response = requests.get(url, headers=headers, timeout=30)
        stats['api_calls'] += 1
        
        logger.info(f"Response Status: {response.status_code}")
        logger.info("")
        
        if response.status_code == 200:
            data = response.json()
            
            logger.info("✅ SUCCESS! Retrieved individual conversation")
            logger.info("")
            logger.info("COMPLETE JSON RESPONSE:")
            logger.info(json.dumps(data, indent=2, default=str))
            logger.info("")
            
            # Check for content fields
            conversation = data.get('conversation', data)  # Might be nested
            
            logger.info("📊 CONTENT FIELD ANALYSIS:")
            logger.info(f"  Response keys: {list(data.keys())}")
            
            if 'conversation' in data:
                logger.info(f"  Conversation object keys: {list(data['conversation'].keys())}")
                conversation = data['conversation']
            else:
                conversation = data
            
            # Check all possible content fields
            content_fields = ['description', 'content', 'body', 'text', 'message', 'html']
            found_content = False
            
            for field in content_fields:
                if field in conversation:
                    content_value = conversation[field]
                    if content_value:
                        found_content = True
                        logger.info(f"  ✅ HAS '{field}' field!")
                        if isinstance(content_value, str):
                            logger.info(f"     Content length: {len(content_value)} chars")
                            logger.info(f"     Preview: {content_value[:200]}...")
                        else:
                            logger.info(f"     Type: {type(content_value)}")
                            logger.info(f"     Value: {content_value}")
            
            if found_content:
                CONVERSATION_DIAGNOSTICS['conversations_individual_with_content'] += 1
                logger.info("")
                logger.info("🎉 CONTENT FOUND IN INDIVIDUAL CONVERSATION!")
            else:
                CONVERSATION_DIAGNOSTICS['conversations_individual_empty'] += 1
                logger.info("")
                logger.info("❌ No content fields found in individual conversation")
            
            logger.info("=" * 80)
            logger.info("")
            
            CONVERSATION_DIAGNOSTICS['conversations_individual_success'] += 1
            return conversation
            
        else:
            logger.error(f"❌ Failed to fetch individual conversation: HTTP {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")
            logger.info("=" * 80)
            logger.info("")
            CONVERSATION_DIAGNOSTICS['conversations_individual_error'] += 1
            return None
            
    except Exception as e:
        logger.error(f"❌ Exception fetching individual conversation: {e}")
        logger.info("=" * 80)
        logger.info("")
        CONVERSATION_DIAGNOSTICS['conversations_individual_error'] += 1
        return None

# ============================================================
# SERVICEDESK API - TEST CONVERSATION FETCHING
# ============================================================
def test_fetch_conversations(ticket_id: str) -> Tuple[int, List[dict]]:
    """
    TEST: Fetch conversation list, then fetch first 3 individually.
    """
    global TOKEN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    total_count = 0
    conversation_messages = []
    
    # Step 1: Fetch conversation list
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/conversations"
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            stats['api_calls'] += 1
            data = response.json()
            conversations = data.get('conversations', [])
            
            logger.info(f"📋 Conversation List Endpoint returned {len(conversations)} conversations")
            logger.info("")
            
            if conversations:
                CONVERSATION_DIAGNOSTICS['conversations_list_success'] += 1
                total_count = len(conversations)
                
                # Step 2: Fetch first 3 individually
                logger.info(f"🔬 TEST: Fetching first {min(MAX_INDIVIDUAL_TESTS, len(conversations))} conversations INDIVIDUALLY")
                logger.info("")
                
                for idx, conv in enumerate(conversations[:MAX_INDIVIDUAL_TESTS]):
                    conv_id = conv.get('id')
                    if conv_id:
                        individual_conv = fetch_individual_conversation(ticket_id, conv_id, idx + 1)
                        
                        if individual_conv:
                            conversation_messages.append({
                                'id': conv_id,
                                'data': individual_conv,
                                'from_list': conv
                            })
                        
                        # Small delay between requests
                        time.sleep(0.5)
                
                logger.info(f"✓ Tested {len(conversation_messages)} individual conversations")
                logger.info("")
                    
    except Exception as e:
        logger.error(f"Error fetching conversations for {ticket_id}: {e}")
    
    return total_count, conversation_messages

# ============================================================
# SERVICEDESK API - TICKET FETCHING
# ============================================================
def fetch_tickets(last_sync: Optional[datetime] = None) -> List[dict]:
    """Fetch tickets from ServiceDesk Plus API - LIMITED FOR TESTING."""
    ensure_token_valid()
    
    all_tickets = []
    page = 1
    row_count = BATCH_SIZE  # Only fetch 10 for testing
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.manageengine.sdp.v3+json"
    }
    
    logger.info(f"📊 Fetching {BATCH_SIZE} tickets for testing...")
    
    list_info = {
        "list_info": {
            "row_count": row_count,
            "start_index": 1,
            "get_total_count": True,
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    url = f"https://{SDP_DOMAIN}/api/v3/requests"
    
    params = {"input_data": json.dumps(list_info)}
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        stats['api_calls'] += 1
        
        data = response.json()
        requests_data = data.get('requests', [])
        
        all_tickets.extend(requests_data)
        logger.info(f"  ✓ Fetched {len(requests_data)} tickets for testing")
        
    except Exception as e:
        logger.error(f"Failed to fetch tickets: {e}")
    
    stats['tickets_fetched'] = len(all_tickets)
    return all_tickets

# ============================================================
# TEST MAIN LOGIC
# ============================================================
def run_test():
    """Test individual conversation fetching."""
    
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 60)
    logger.info("TEST MODE: Individual Conversation Fetching")
    logger.info("")
    logger.info("This test will:")
    logger.info("  1. Fetch conversation list (as before)")
    logger.info("  2. Take first 3 conversation IDs from list")
    logger.info("  3. Fetch EACH conversation individually")
    logger.info("  4. Show if content exists at individual level")
    logger.info("")
    
    refresh_token()
    
    logger.info("Fetching test tickets...")
    tickets = fetch_tickets(None)
    
    if not tickets:
        logger.info("No tickets to test")
        return
    
    logger.info(f"Testing with {len(tickets)} tickets")
    logger.info("")
    
    # Test first ticket only
    test_ticket = tickets[0]
    ticket_id = str(test_ticket.get('id', ''))
    
    # FIXED: Safe display_id extraction
    display_id_obj = test_ticket.get('display_id', {})
    if isinstance(display_id_obj, dict):
        display_id = str(display_id_obj.get('display_value', ticket_id))
    else:
        display_id = str(display_id_obj) if display_id_obj else ticket_id
    
    subject = test_ticket.get('subject', 'Unknown')
    
    logger.info("=" * 80)
    logger.info(f"🎯 TESTING TICKET: #{display_id}")
    logger.info(f"Subject: {subject}")
    logger.info(f"Internal ID: {ticket_id}")
    logger.info("=" * 80)
    logger.info("")
    
    # Test conversation fetching
    total_count, conversations = test_fetch_conversations(ticket_id)
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST RESULTS SUMMARY")
    logger.info("=" * 80)
    logger.info("")
    logger.info("📊 DIAGNOSTICS:")
    logger.info(f"  Conversation list success: {CONVERSATION_DIAGNOSTICS['conversations_list_success']}")
    logger.info(f"  Individual fetches attempted: {CONVERSATION_DIAGNOSTICS['conversations_individual_success'] + CONVERSATION_DIAGNOSTICS['conversations_individual_error']}")
    logger.info(f"  Individual fetches successful: {CONVERSATION_DIAGNOSTICS['conversations_individual_success']}")
    logger.info(f"  Individual fetches with content: {CONVERSATION_DIAGNOSTICS['conversations_individual_with_content']}")
    logger.info(f"  Individual fetches empty: {CONVERSATION_DIAGNOSTICS['conversations_individual_empty']}")
    logger.info(f"  Individual fetch errors: {CONVERSATION_DIAGNOSTICS['conversations_individual_error']}")
    logger.info("")
    logger.info(f"  Total API calls: {stats['api_calls']}")
    logger.info("")
    
    if CONVERSATION_DIAGNOSTICS['conversations_individual_with_content'] > 0:
        logger.info("✅ SUCCESS: Content found in individual conversations!")
        logger.info("   We can fetch conversation content by requesting each one individually.")
        logger.info("")
        logger.info("📝 NEXT STEPS:")
        logger.info("   1. Modify script to fetch each conversation individually")
        logger.info("   2. Extract content from individual responses")
        logger.info("   3. This will add API calls but give us the content")
    else:
        logger.info("❌ NO CONTENT: Individual conversations also don't have content")
        logger.info("   The conversation endpoint may not store message content.")
        logger.info("")
        logger.info("📝 ALTERNATIVE APPROACH:")
        logger.info("   1. Use ticket description field (initial user request)")
        logger.info("   2. Use notes endpoint (technician comments)")
        logger.info("   3. These fields DO have content as we saw in previous tests")
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST COMPLETE")
    logger.info("=" * 80)

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
        logger.info(f"  Version: {VERSION}")
        logger.info(f"  Test Mode: Individual Conversation Fetching")
        logger.info("")
        
        run_test()
        
    except KeyboardInterrupt:
        logger.info("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
