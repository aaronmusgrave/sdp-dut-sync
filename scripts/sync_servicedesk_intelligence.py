#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.21-DEBUG3
=============================================

DEBUG3: Check if conversation text comes with ticket DETAIL endpoint
- Maybe conversations are included in the main ticket response
- Try different 'include' parameters
- Dump complete ticket detail response
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

VERSION = "3.1.21-DEBUG3"

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

def fetch_ticket_detail_with_includes(ticket_id: str, display_id: str) -> Dict:
    """Fetch COMPLETE ticket detail - try multiple include combinations"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}"
    
    logger.info(f"\n{'='*70}")
    logger.info(f"📋 TICKET #{display_id} - Fetching COMPLETE Detail")
    logger.info(f"{'='*70}")
    
    # Try multiple combinations of include parameters
    include_combinations = [
        ["conversations"],
        ["conversations", "notes"],
        ["conversations", "notes", "resolution"],
        ["conversations", "notes", "resolution", "worklogs"],
        ["conversations", "notes", "resolution", "worklogs", "tasks"],
        None  # No include parameter
    ]
    
    for i, include_params in enumerate(include_combinations, 1):
        logger.info(f"\n--- Attempt {i}/{len(include_combinations)} ---")
        
        if include_params:
            input_data = {"include": include_params}
            params = {'input_data': json.dumps(input_data)}
            logger.info(f"Include parameters: {include_params}")
        else:
            params = {}
            logger.info("No include parameters (default)")
        
        logger.info(f"URL: {url}")
        
        try:
            response = session.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            # Check if we got the ticket
            request_data = data.get('request', {})
            
            if not request_data:
                logger.warning("No 'request' key in response")
                continue
            
            logger.info(f"\n{'#'*70}")
            logger.info(f"RESPONSE STRUCTURE:")
            logger.info(f"{'#'*70}")
            logger.info(f"Top-level keys: {list(data.keys())}")
            logger.info(f"Request keys: {list(request_data.keys())}")
            
            # Check for conversation-related keys
            conversation_keys = [k for k in request_data.keys() if 'conv' in k.lower() or 'note' in k.lower() or 'comment' in k.lower()]
            
            if conversation_keys:
                logger.info(f"\n✓ FOUND conversation-related keys: {conversation_keys}")
                
                for key in conversation_keys:
                    value = request_data[key]
                    logger.info(f"\n{'='*70}")
                    logger.info(f"KEY: '{key}'")
                    logger.info(f"{'='*70}")
                    logger.info(f"Type: {type(value).__name__}")
                    
                    if isinstance(value, list):
                        logger.info(f"List length: {len(value)}")
                        if len(value) > 0:
                            logger.info(f"First item keys: {list(value[0].keys()) if isinstance(value[0], dict) else 'Not a dict'}")
                            logger.info(f"First item sample:")
                            logger.info(json.dumps(value[0], indent=2)[:1000])
                    elif isinstance(value, dict):
                        logger.info(f"Dict keys: {list(value.keys())}")
                        logger.info(f"Content sample:")
                        logger.info(json.dumps(value, indent=2)[:1000])
                    else:
                        logger.info(f"Value: {value}")
            
            # Dump first 10000 chars of full response
            logger.info(f"\n{'#'*70}")
            logger.info(f"FULL RESPONSE (first 10000 chars):")
            logger.info(f"{'#'*70}")
            full_json = json.dumps(data, indent=2)
            logger.info(full_json[:10000])
            if len(full_json) > 10000:
                logger.info(f"\n... (truncated, total length: {len(full_json)} chars)")
            logger.info(f"{'#'*70}")
            
            # Search for ANY field containing "text", "message", "description", "comment", "note"
            logger.info(f"\n🔍 Searching entire response for text content...")
            text_found = False
            
            def search_for_text(obj, path=""):
                nonlocal text_found
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        new_path = f"{path}.{key}" if path else key
                        
                        # Check if key name suggests text content
                        if any(keyword in key.lower() for keyword in ['text', 'message', 'description', 'comment', 'note', 'body', 'content', 'reply']):
                            if isinstance(value, str) and len(value) > 20:
                                logger.info(f"   ✓ FOUND TEXT at '{new_path}': {len(value)} chars")
                                logger.info(f"      Preview: {value[:200]}...")
                                text_found = True
                        
                        search_for_text(value, new_path)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        search_for_text(item, f"{path}[{i}]")
            
            search_for_text(data)
            
            if not text_found:
                logger.warning(f"   ⚠️  No text content found in response")
            
            # If this attempt found conversations, we're done
            if conversation_keys and text_found:
                logger.info(f"\n✓ SUCCESS with include={include_params}")
                return data
            
            # Small delay between attempts
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"   ❌ Error with include={include_params}: {e}")
            continue
    
    logger.warning(f"\n⚠️  No conversations found in any attempt")
    return {}

def fetch_all_tickets() -> List[Dict]:
    """Fetch first 3 tickets"""
    logger.info(f"📊 DEBUG3: Fetching first 3 tickets")
    
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
    """Main execution - DEBUG3 MODE"""
    logger.info("=" * 70)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 70)
    logger.info("🐛 DEBUG3 MODE: Checking if conversations in ticket DETAIL endpoint")
    logger.info("   - Processing only 3 tickets")
    logger.info("   - Trying multiple 'include' parameter combinations")
    logger.info("   - Searching entire response for text content")
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
            
            detail = fetch_ticket_detail_with_includes(ticket_id, display_id)
            
            # Pause between tickets for readability
            if i < len(tickets):
                logger.info(f"\n{'~'*70}")
                logger.info("Pausing 3 seconds before next ticket...")
                logger.info(f"{'~'*70}\n")
                time.sleep(3)
        
        logger.info("\n" + "=" * 70)
        logger.info(f"✓ DEBUG3 TEST COMPLETE (v{VERSION})")
        logger.info("=" * 70)
        logger.info("\nREVIEW THE LOGS ABOVE TO DETERMINE:")
        logger.info("  1. Does ticket detail endpoint include conversation text?")
        logger.info("  2. Which 'include' parameters work?")
        logger.info("  3. What field names contain the conversation text?")
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
