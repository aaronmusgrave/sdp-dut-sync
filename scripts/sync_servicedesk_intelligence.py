#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.20-DEBUG2
=============================================

DEBUG2: Dump complete API response to find conversation text
- Process only 3 tickets
- Show FULL JSON response from conversation list endpoint
- Determine if text is available and under what field name
"""

import os
import sys
import requests
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# VERSION & CONFIGURATION
# ============================================================

VERSION = "3.1.20-DEBUG2"

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
SDP_DOMAIN = os.environ.get("SDP_DOMAIN", "").strip()
SDP_CLIENT_ID = os.environ.get("SDP_CLIENT_ID", "").strip().lstrip('\\').lstrip('/')
SDP_CLIENT_SECRET = os.environ.get("SDP_CLIENT_SECRET", "").strip().lstrip('\\').lstrip('/')
SDP_REFRESH_TOKEN = os.environ.get("SDP_REFRESH_TOKEN", "").strip().lstrip('\\').lstrip('/')
SDP_BASE_URL = f"https://{SDP_DOMAIN}"

ZOHO_OAUTH_URL = os.environ.get("ZOHO_OAUTH_URL", "https://accounts.zoho.com/oauth/v2/token")

print(f"✓ Configuration loaded")
print(f"  SDP Domain: {SDP_DOMAIN}")
print("")

# Processing Configuration - DEBUG MODE
BATCH_SIZE = 3  # Only process 3 tickets
RETRY_ATTEMPTS = 3

# ============================================================
# LOGGING SETUP - DEBUG MODE
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

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
            
            logger.info(f"✓ Token refreshed successfully")
            
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
        pool_connections=20,
        pool_maxsize=20
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()

# ============================================================
# SERVICEDESK PLUS API
# ============================================================

def fetch_tickets_from_sdp(start_index: int = 1, row_count: int = 100) -> Dict:
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
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_BASE_URL}/api/v3/requests"
    
    try:
        response = session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        raise

def fetch_ticket_conversations(ticket_id: str, display_id: str) -> List[Dict]:
    """Fetch conversation list - DUMP FULL RESPONSE"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations"
    
    logger.info(f"\n{'='*70}")
    logger.info(f"📋 TICKET #{display_id} - Fetching Conversations")
    logger.info(f"{'='*70}")
    logger.info(f"URL: {url}")
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # DUMP THE ENTIRE RESPONSE
        logger.info(f"\n{'#'*70}")
        logger.info(f"FULL API RESPONSE:")
        logger.info(f"{'#'*70}")
        logger.info(json.dumps(data, indent=2))
        logger.info(f"{'#'*70}\n")
        
        conversations = data.get('conversations', [])
        
        logger.info(f"✓ Found {len(conversations)} conversations")
        
        if conversations and len(conversations) > 0:
            # Show first conversation in EXTREME detail
            first_conv = conversations[0]
            conv_id = first_conv.get('id', 'unknown')
            
            logger.info(f"\n{'='*70}")
            logger.info(f"FIRST CONVERSATION (ID: {conv_id}) - DETAILED VIEW:")
            logger.info(f"{'='*70}")
            logger.info(f"All available keys: {list(first_conv.keys())}")
            logger.info(f"\nField-by-field breakdown:")
            logger.info(f"{'-'*70}")
            
            for key, value in first_conv.items():
                value_type = type(value).__name__
                
                if value is None:
                    logger.info(f"  {key}: None (type: {value_type})")
                elif isinstance(value, str):
                    if len(value) > 200:
                        logger.info(f"  {key}: '{value[:200]}...' (truncated, full length: {len(value)} chars)")
                        logger.info(f"           [TYPE: {value_type}]")
                    else:
                        logger.info(f"  {key}: '{value}'")
                        logger.info(f"           [TYPE: {value_type}, LENGTH: {len(value)} chars]")
                elif isinstance(value, dict):
                    logger.info(f"  {key}: {json.dumps(value, indent=4)}")
                    logger.info(f"           [TYPE: {value_type}, KEYS: {list(value.keys())}]")
                elif isinstance(value, list):
                    logger.info(f"  {key}: {value}")
                    logger.info(f"           [TYPE: {value_type}, LENGTH: {len(value)} items]")
                else:
                    logger.info(f"  {key}: {value}")
                    logger.info(f"           [TYPE: {value_type}]")
            
            logger.info(f"{'='*70}\n")
            
            # Check for ANY field that might contain text
            logger.info(f"🔍 Searching for TEXT content in any field...")
            text_fields = ['text', 'description', 'message', 'content', 'body', 
                          'comment', 'reply', 'note', 'details', 'html']
            found_text = False
            
            for field in text_fields:
                if field in first_conv:
                    value = first_conv[field]
                    if value:
                        logger.info(f"   ✓ FOUND: '{field}' field exists and has content!")
                        logger.info(f"      Content length: {len(str(value))} chars")
                        logger.info(f"      Preview: {str(value)[:100]}...")
                        found_text = True
            
            if not found_text:
                logger.warning(f"   ⚠️  NO TEXT FIELDS FOUND in conversation data")
                logger.warning(f"   Available fields: {list(first_conv.keys())}")
        
        return conversations
        
    except Exception as e:
        logger.error(f"❌ Failed to fetch conversations for ticket #{display_id}: {e}")
        return []

def fetch_all_tickets() -> List[Dict]:
    """Fetch first 3 tickets"""
    logger.info(f"📊 DEBUG2: Fetching first 3 tickets")
    
    try:
        data = fetch_tickets_from_sdp(start_index=1, row_count=100)
        tickets = data.get('requests', [])
        
        if tickets:
            tickets = tickets[:3]
            logger.info(f"✓ Fetched {len(tickets)} tickets\n")
            return tickets
        
        return []
    
    except Exception as e:
        logger.error(f"Error fetching tickets: {e}")
        return []

# ============================================================
# MAIN
# ============================================================

def main():
    """Main execution - DEBUG2 MODE"""
    logger.info("=" * 70)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 70)
    logger.info("🐛 DEBUG2 MODE: Dumping full conversation API response")
    logger.info("   - Processing only 3 tickets")
    logger.info("   - Showing COMPLETE JSON response")
    logger.info("   - Analyzing all fields for text content")
    logger.info("")
    
    try:
        token_manager.get_token()
        
        tickets = fetch_all_tickets()
        
        if not tickets:
            logger.warning("✓ No tickets to process")
            return 0
        
        logger.info(f"Processing {len(tickets)} tickets...\n")
        
        for i, ticket in enumerate(tickets, 1):
            ticket_id = ticket.get('id')
            display_id = ticket.get('display_id')
            subject = ticket.get('subject', 'No Subject')
            
            logger.info(f"\n{'*'*70}")
            logger.info(f"TICKET {i}/3: #{display_id}")
            logger.info(f"Subject: {subject}")
            logger.info(f"{'*'*70}")
            
            conversations = fetch_ticket_conversations(ticket_id, display_id)
            
            if not conversations:
                logger.warning(f"No conversations found for ticket #{display_id}")
            
            # Pause between tickets for readability
            if i < len(tickets):
                logger.info(f"\n{'~'*70}")
                logger.info("Pausing 2 seconds before next ticket...")
                logger.info(f"{'~'*70}\n")
                time.sleep(2)
        
        logger.info("\n" + "=" * 70)
        logger.info(f"✓ DEBUG2 TEST COMPLETE (v{VERSION})")
        logger.info("=" * 70)
        logger.info("\nREVIEW THE LOGS ABOVE TO DETERMINE:")
        logger.info("  1. Is 'text' field present in conversation data?")
        logger.info("  2. If not, what fields ARE available?")
        logger.info("  3. Is conversation text available via this API at all?")
        logger.info("=" * 70)
        
        return 0
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Test interrupted")
        return 130
    
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
