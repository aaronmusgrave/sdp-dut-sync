#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.22-DEBUG4
=============================================

DEBUG4: Test ALL possible endpoints for conversation text
- /notes
- /replies  
- /comments
- /resolutions
- /worklogs
- /threads
"""

import os
import sys
import requests
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "3.1.22-DEBUG4"

# [Environment validation code - same as before]
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
SDP_CLIENT_ID = os.environ.get("SDP_CLIENT_ID", "").strip()
SDP_CLIENT_SECRET = os.environ.get("SDP_CLIENT_SECRET", "").strip()
SDP_REFRESH_TOKEN = os.environ.get("SDP_REFRESH_TOKEN", "").strip()
SDP_BASE_URL = f"https://{SDP_DOMAIN}"
ZOHO_OAUTH_URL = "https://accounts.zoho.com/oauth/v2/token"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class TokenManager:
    def __init__(self):
        self.access_token = None
        self.token_expiry = None
        self.refresh_buffer = timedelta(minutes=5)
        
    def get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if (self.access_token is None or 
            self.token_expiry is None or 
            now >= (self.token_expiry - self.refresh_buffer)):
            self._refresh_token()
        return self.access_token
    
    def _refresh_token(self):
        if not self.access_token:
            logger.info("🔑 Fetching new token...")
        
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

def create_session() -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()

def test_endpoint(ticket_id: str, endpoint_name: str, display_id: str) -> Dict:
    """Test a specific endpoint for conversation/note data"""
    access_token = token_manager.get_token()
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/{endpoint_name}"
    
    logger.info(f"\n{'='*70}")
    logger.info(f"Testing: /{endpoint_name}")
    logger.info(f"{'='*70}")
    logger.info(f"URL: {url}")
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        
        logger.info(f"Status Code: {response.status_code}")
        
        if response.status_code == 404:
            logger.warning(f"❌ 404 - Endpoint doesn't exist")
            return {"exists": False, "status": 404}
        
        if response.status_code == 403:
            logger.warning(f"❌ 403 - No permission")
            return {"exists": False, "status": 403}
        
        response.raise_for_status()
        data = response.json()
        
        logger.info(f"✅ SUCCESS - Got response!")
        logger.info(f"\nTop-level keys: {list(data.keys())}")
        
        # Dump full response
        logger.info(f"\n{'#'*70}")
        logger.info(f"FULL RESPONSE:")
        logger.info(f"{'#'*70}")
        full_json = json.dumps(data, indent=2)
        logger.info(full_json[:5000])
        if len(full_json) > 5000:
            logger.info(f"\n... (truncated, total: {len(full_json)} chars)")
        logger.info(f"{'#'*70}")
        
        # Search for text content
        text_found = False
        
        def search_for_text(obj, path=""):
            nonlocal text_found
            if isinstance(obj, dict):
                for key, value in obj.items():
                    new_path = f"{path}.{key}" if path else key
                    if any(keyword in key.lower() for keyword in 
                          ['text', 'message', 'description', 'comment', 'note', 
                           'body', 'content', 'reply', 'resolution']):
                        if isinstance(value, str) and len(value) > 20:
                            logger.info(f"\n   ✓ FOUND TEXT at '{new_path}':")
                            logger.info(f"      Length: {len(value)} chars")
                            logger.info(f"      Preview: {value[:300]}...")
                            text_found = True
                    search_for_text(value, new_path)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    search_for_text(item, f"{path}[{i}]")
        
        search_for_text(data)
        
        if not text_found:
            logger.warning(f"\n   ⚠️  No substantial text content found")
        
        return {"exists": True, "status": 200, "data": data, "has_text": text_found}
        
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ HTTP Error: {e}")
        return {"exists": False, "status": e.response.status_code if e.response else None}
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return {"exists": False, "error": str(e)}

def fetch_tickets():
    """Fetch first ticket with conversations"""
    access_token = token_manager.get_token()
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "sort_field": "created_time",
            "sort_order": "desc"
        }
    }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_BASE_URL}/api/v3/requests"
    
    response = session.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get('requests', [])[:1]

def main():
    logger.info("=" * 70)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 70)
    logger.info("🔍 DEBUG4: Testing ALL endpoints for conversation text")
    logger.info("")
    
    try:
        token_manager.get_token()
        
        logger.info("Fetching test ticket...")
        tickets = fetch_tickets()
        
        if not tickets:
            logger.error("No tickets found")
            return 1
        
        ticket = tickets[0]
        ticket_id = ticket.get('id')
        display_id = ticket.get('display_id')
        subject = ticket.get('subject', 'No Subject')
        
        logger.info(f"\n{'*'*70}")
        logger.info(f"TEST TICKET: #{display_id}")
        logger.info(f"Subject: {subject}")
        logger.info(f"{'*'*70}\n")
        
        # Test all possible endpoints
        endpoints_to_test = [
            'notes',
            'replies',
            'comments',
            'resolutions',
            'worklogs',
            'threads',
            'conversations',
            'activities',
            'history',
            'responses',
            'messages',
            'discussions'
        ]
        
        results = {}
        
        for endpoint in endpoints_to_test:
            result = test_endpoint(ticket_id, endpoint, display_id)
            results[endpoint] = result
            
            # Small delay between requests
            import time
            time.sleep(0.3)
        
        # Summary
        logger.info("\n" + "=" * 70)
        logger.info("SUMMARY - Endpoint Test Results")
        logger.info("=" * 70)
        
        working_endpoints = []
        with_text = []
        
        for endpoint, result in results.items():
            if result.get('exists'):
                status = "✅ EXISTS"
                if result.get('has_text'):
                    status += " + HAS TEXT ⭐"
                    with_text.append(endpoint)
                working_endpoints.append(endpoint)
            else:
                status = f"❌ {result.get('status', 'ERROR')}"
            
            logger.info(f"  /{endpoint:20s} : {status}")
        
        logger.info("=" * 70)
        
        if with_text:
            logger.info(f"\n🎉 FOUND TEXT IN: {', '.join(with_text)}")
            logger.info("\nThese endpoints contain conversation/note text!")
        else:
            logger.warning(f"\n⚠️  No endpoints found with conversation text")
            if working_endpoints:
                logger.info(f"\nWorking endpoints (without text): {', '.join(working_endpoints)}")
        
        logger.info("=" * 70)
        
        return 0
        
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
