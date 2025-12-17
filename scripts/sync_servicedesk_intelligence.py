#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.1.18-ENDPOINT-TEST
====================================================

Uses proven v3.1.18 auth to test conversation endpoints
"""

import os
import sys
import requests
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "3.1.18-ENDPOINT-TEST"

# Validate environment variables
def validate_required_env_vars():
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

# Configuration - EXACT same as v3.1.18
SDP_DOMAIN = os.environ.get("SDP_DOMAIN", "").strip()
SDP_CLIENT_ID = os.environ.get("SDP_CLIENT_ID", "").strip().lstrip('\\').lstrip('/')
SDP_CLIENT_SECRET = os.environ.get("SDP_CLIENT_SECRET", "").strip().lstrip('\\').lstrip('/')
SDP_REFRESH_TOKEN = os.environ.get("SDP_REFRESH_TOKEN", "").strip().lstrip('\\').lstrip('/')
SDP_BASE_URL = f"https://{SDP_DOMAIN}"
ZOHO_OAUTH_URL = "https://accounts.zoho.com/oauth/v2/token"

print(f"✓ Configuration loaded")
print(f"  SDP Domain: {SDP_DOMAIN}")
print("")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Token Manager - EXACT same as v3.1.18
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

def fetch_tickets():
    """Fetch first 3 tickets"""
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
    return data.get('requests', [])[:3]

def test_endpoint(ticket_id: str, endpoint_name: str, display_id: str) -> Dict:
    """Test a specific endpoint"""
    access_token = token_manager.get_token()
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/{endpoint_name}"
    
    logger.info(f"\n{'='*70}")
    logger.info(f"Testing: /{endpoint_name}")
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
        
        logger.info(f"✅ SUCCESS!")
        logger.info(f"Top-level keys: {list(data.keys())}")
        
        # Dump response
        full_json = json.dumps(data, indent=2)
        logger.info(f"\n{'#'*70}")
        logger.info(f"FULL RESPONSE (first 3000 chars):")
        logger.info(f"{'#'*70}")
        logger.info(full_json[:3000])
        if len(full_json) > 3000:
            logger.info(f"... (truncated, total: {len(full_json)} chars)")
        logger.info(f"{'#'*70}\n")
        
        # Search for text
        text_found = False
        def search_text(obj, path=""):
            nonlocal text_found
            if isinstance(obj, dict):
                for key, value in obj.items():
                    new_path = f"{path}.{key}" if path else key
                    if any(kw in key.lower() for kw in ['text', 'message', 'description', 'comment', 'note', 'body', 'content', 'reply', 'resolution']):
                        if isinstance(value, str) and len(value) > 20:
                            logger.info(f"   ✓ FOUND TEXT at '{new_path}':")
                            logger.info(f"      Length: {len(value)} chars")
                            logger.info(f"      Preview: {value[:200]}...")
                            text_found = True
                    search_text(value, new_path)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    search_text(item, f"{path}[{i}]")
        
        search_text(data)
        
        if not text_found:
            logger.warning(f"   ⚠️  No text content found")
        
        return {"exists": True, "status": 200, "has_text": text_found, "data": data}
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return {"exists": False, "error": str(e)}

def main():
    logger.info("=" * 70)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE v{VERSION}")
    logger.info("=" * 70)
    logger.info("🔍 Testing conversation endpoints on 3 tickets")
    logger.info("")
    
    try:
        token_manager.get_token()
        
        logger.info("📊 Fetching 3 test tickets...")
        tickets = fetch_tickets()
        
        if not tickets:
            logger.error("No tickets found")
            return 1
        
        logger.info(f"✓ Got {len(tickets)} tickets\n")
        
        # Test first ticket with all endpoints
        ticket = tickets[0]
        ticket_id = ticket.get('id')
        display_id = ticket.get('display_id')
        subject = ticket.get('subject', 'No Subject')
        
        logger.info(f"{'*'*70}")
        logger.info(f"TEST TICKET: #{display_id}")
        logger.info(f"Subject: {subject}")
        logger.info(f"{'*'*70}\n")
        
        # Test endpoints
        endpoints = [
            'notes',
            'replies', 
            'resolutions',
            'worklogs',
            'threads',
            'comments',
            'conversations'
        ]
        
        results = {}
        for endpoint in endpoints:
            result = test_endpoint(ticket_id, endpoint, display_id)
            results[endpoint] = result
            time.sleep(0.3)
        
        # Summary
        logger.info("\n" + "=" * 70)
        logger.info("ENDPOINT TEST RESULTS")
        logger.info("=" * 70)
        
        working = []
        with_text = []
        
        for endpoint, result in results.items():
            if result.get('exists'):
                status = "✅ EXISTS"
                if result.get('has_text'):
                    status += " + HAS TEXT ⭐"
                    with_text.append(endpoint)
                working.append(endpoint)
            else:
                status = f"❌ {result.get('status', 'ERROR')}"
            logger.info(f"  /{endpoint:20s} : {status}")
        
        logger.info("=" * 70)
        
        if with_text:
            logger.info(f"\n🎉 SUCCESS! Found text in: {', '.join(with_text)}")
            logger.info("\nWe can use these endpoints for conversation data!")
        else:
            logger.warning(f"\n⚠️  No endpoints found with conversation text")
            if working:
                logger.info(f"Working endpoints (metadata only): {', '.join(working)}")
        
        logger.info("=" * 70)
        
        return 0
        
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
