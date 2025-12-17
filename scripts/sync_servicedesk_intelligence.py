#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.19-DEBUG
============================================

DEBUG VERSION - Diagnose conversation fetching issue
- Process only 10 tickets
- Skip cleanup (don't delete existing data)
- Enhanced logging for conversation fetching
- Show exactly why conversations have no text
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

VERSION = "3.1.19-DEBUG"

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

# Processing Configuration - DEBUG MODE
BATCH_SIZE = 10  # Only process 10 tickets
SUB_BATCH_SIZE = 5
MAX_PAGES = 1  # Only fetch first page
MAX_CONCURRENT_CONVERSATIONS = 5  # Reduced for debugging
MAX_CONCURRENT_UPLOADS = 5
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
# LOGGING SETUP - DEBUG MODE
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for detailed output
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
        response = session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        raise

def fetch_conversation_detail(ticket_id: str, conversation_id: str) -> Dict:
    """Fetch individual conversation detail - WITH DEBUG LOGGING"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations/{conversation_id}"
    
    logger.debug(f"🔍 Fetching conversation detail: Ticket #{ticket_id}, Conv #{conversation_id}")
    logger.debug(f"   URL: {url}")
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        logger.debug(f"   Response status: {response.status_code}")
        logger.debug(f"   Response keys: {list(data.keys())}")
        
        time.sleep(CONVERSATION_DETAIL_DELAY)
        
        METRICS['conversation_details_fetched'] += 1
        
        conv_data = data.get('conversation', {})
        
        if conv_data:
            logger.debug(f"   Conversation data keys: {list(conv_data.keys())}")
            
            text = conv_data.get('text', '')
            if text:
                logger.info(f"   ✓ HAS TEXT: {len(text)} characters")
                METRICS['conversations_with_text'] += 1
            else:
                logger.warning(f"   ⚠️  NO TEXT in conversation data")
                logger.debug(f"   Full conversation data: {json.dumps(conv_data, indent=2)[:500]}...")
                METRICS['conversations_empty'] += 1
        else:
            logger.warning(f"   ⚠️  No 'conversation' key in response")
            logger.debug(f"   Full response: {json.dumps(data, indent=2)[:500]}...")
            METRICS['conversations_empty'] += 1
        
        return conv_data
        
    except requests.exceptions.HTTPError as e:
        logger.error(f"   ❌ HTTP Error {e.response.status_code}: {e}")
        METRICS['conversation_api_errors'] += 1
        return {}
    except Exception as e:
        logger.error(f"   ❌ Error fetching conversation: {e}")
        METRICS['conversation_api_errors'] += 1
        return {}

def fetch_ticket_conversations(ticket_id: str) -> List[Dict]:
    """Fetch conversation list, then fetch details for each"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations"
    
    logger.info(f"📋 Fetching conversations for Ticket #{ticket_id}")
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        conversations = data.get('conversations', [])
        
        logger.info(f"   Found {len(conversations)} conversations")
        
        if not conversations:
            logger.warning(f"   ⚠️  No conversations found for ticket #{ticket_id}")
            return []
        
        # Log conversation IDs
        conv_ids = [str(conv.get('id', 'unknown')) for conv in conversations]
        logger.debug(f"   Conversation IDs: {', '.join(conv_ids)}")
        
        detailed_conversations = []
        
        # Fetch details for each conversation
        for i, conv in enumerate(conversations, 1):
            conv_id = conv.get('id')
            logger.info(f"   Processing conversation {i}/{len(conversations)} (ID: {conv_id})")
            
            conv_detail = fetch_conversation_detail(ticket_id, conv_id)
            merged_conv = {**conv, **conv_detail}
            detailed_conversations.append(merged_conv)
        
        return detailed_conversations
        
    except Exception as e:
        logger.error(f"❌ Failed to fetch conversations for ticket #{ticket_id}: {e}")
        return []

def fetch_conversations_parallel(ticket_ids: List[str]) -> Dict[str, List[Dict]]:
    """Fetch conversations for multiple tickets in parallel"""
    conversations_map = {}
    
    logger.info(f"🔄 Fetching conversations for {len(ticket_ids)} tickets (parallel)")
    
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
    """Fetch tickets - DEBUG: Only first 10"""
    all_tickets = []
    page = 1
    row_count = 100
    
    logger.info(f"📊 DEBUG MODE: Fetching first 10 tickets only")
    
    start_index = 1
    
    try:
        data = fetch_tickets_from_sdp(start_index, row_count, last_sync_time)
        tickets = data.get('requests', [])
        
        if tickets:
            # Take only first 10
            tickets = tickets[:10]
            all_tickets.extend(tickets)
            logger.info(f"  Page {page}: {len(tickets)} tickets (DEBUG: Limited to 10)")
    
    except Exception as e:
        logger.error(f"Error fetching tickets: {e}")
    
    logger.info(f"✓ Fetched {len(all_tickets)} tickets\n")
    return all_tickets

# ============================================================
# MINIMAL TICKET PROCESSING (Focus on conversations)
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
    """Extract minimal fields for debugging"""
    fields = {
        'ticket_id': safe_get(ticket, 'display_id', default='Unknown'),
        'subject': safe_get(ticket, 'subject', default='No Subject'),
        'status': safe_get(ticket, 'status', 'name', default='Unknown'),
        'priority': safe_get(ticket, 'priority', 'name', default='Normal'),
        'technician': safe_get(ticket, 'technician', 'name', default='Unassigned'),
    }
    return fields

def process_single_ticket(ticket: Dict, conversations_map: Dict[str, List[Dict]]) -> Optional[Dict]:
    """Process single ticket - FOCUS ON CONVERSATIONS"""
    try:
        ticket_id = ticket.get('id')
        display_id = ticket.get('display_id')
        conversations = conversations_map.get(str(ticket_id), [])
        
        fields = extract_comprehensive_fields(ticket)
        
        logger.info(f"\n{'='*60}")
        logger.info(f"TICKET #{display_id} SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Subject: {fields['subject']}")
        logger.info(f"Conversations: {len(conversations)}")
        
        if conversations:
            logger.info(f"\nConversation Details:")
            for i, conv in enumerate(conversations, 1):
                text = conv.get('text', '')
                sender = conv.get('sender', {}).get('name', 'Unknown')
                conv_id = conv.get('id', 'unknown')
                
                logger.info(f"  Conversation {i} (ID: {conv_id}):")
                logger.info(f"    From: {sender}")
                logger.info(f"    Has text: {'YES' if text else 'NO'}")
                if text:
                    logger.info(f"    Text length: {len(text)} chars")
                    logger.info(f"    Text preview: {text[:100]}...")
                else:
                    logger.info(f"    ⚠️  NO TEXT CONTENT")
                    logger.debug(f"    Keys in conversation: {list(conv.keys())}")
        else:
            logger.warning(f"  ⚠️  NO CONVERSATIONS for this ticket")
        
        logger.info(f"{'='*60}\n")
        
        METRICS['total_processed'] += 1
        
        return {"ticket_id": display_id, "conversations": len(conversations)}
        
    except Exception as e:
        logger.error(f"Error processing ticket: {e}")
        METRICS['skipped'] += 1
        return None

def process_ticket_batch(tickets: List[Dict]) -> None:
    """Process batch of tickets"""
    logger.info(f"\n{'#'*60}")
    logger.info(f"PROCESSING BATCH OF {len(tickets)} TICKETS")
    logger.info(f"{'#'*60}\n")
    
    ticket_ids = [str(t.get('id')) for t in tickets]
    conversations_map = fetch_conversations_parallel(ticket_ids)
    
    for ticket in tickets:
        process_single_ticket(ticket, conversations_map)

# ============================================================
# MAIN
# ============================================================

def main():
    """Main execution - DEBUG MODE"""
    logger.info("=" * 60)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 60)
    logger.info("🐛 DEBUG MODE: Testing conversation fetching")
    logger.info("   - Processing only 10 tickets")
    logger.info("   - Skipping cleanup (no data deletion)")
    logger.info("   - Enhanced logging enabled")
    logger.info("   - Skipping Dust upload")
    logger.info("")
    
    try:
        token_manager.get_token()
        
        logger.info("📊 Fetching 10 test tickets...")
        tickets = fetch_all_tickets(max_pages=1)
        
        if not tickets:
            logger.warning("✓ No tickets to process")
            return 0
        
        logger.info(f"✓ Fetched {len(tickets)} tickets\n")
        
        process_ticket_batch(tickets)
        
        logger.info("\n" + "=" * 60)
        logger.info(f"✓ DEBUG TEST COMPLETE (v{VERSION})")
        logger.info("=" * 60)
        logger.info(f"  Tickets Processed: {METRICS['total_processed']}")
        logger.info(f"  Conversation Details Fetched: {METRICS['conversation_details_fetched']}")
        logger.info(f"  Conversations WITH Text: {METRICS['conversations_with_text']}")
        logger.info(f"  Conversations EMPTY: {METRICS['conversations_empty']}")
        logger.info(f"  API Errors: {METRICS['conversation_api_errors']}")
        logger.info("=" * 60)
        
        if METRICS['conversations_with_text'] == 0:
            logger.warning("\n⚠️  WARNING: NO conversations have text!")
            logger.warning("    Check the debug logs above to see why.")
        
        return 0
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Test interrupted")
        return 130
    
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
