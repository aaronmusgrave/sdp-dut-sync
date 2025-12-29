#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.5.4-FIND-CONVERSATIONS
MISSION: Find where the portal conversation content actually lives in the API
"""

import os
import sys
import json
import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

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

SDP_DOMAIN = clean_secret(os.getenv('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_secret(os.getenv('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_secret(os.getenv('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_secret(os.getenv('SDP_REFRESH_TOKEN', ''))

VERSION = "v3.5.4-FIND-CONVERSATIONS"

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

TOKEN = None
TOKEN_EXPIRY = None

# ============================================================
# TOKEN MANAGEMENT
# ============================================================
def refresh_token():
    """Refresh OAuth token."""
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
        
        logger.info(f"✓ Token refreshed successfully")
        
    except Exception as e:
        logger.error(f"Failed to refresh token: {e}")
        raise

# ============================================================
# FIND TICKET
# ============================================================
def find_ticket_15706() -> Optional[dict]:
    """Find ticket #15706."""
    global TOKEN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.manageengine.sdp.v3+json"
    }
    
    logger.info("🔍 Searching for ticket #15706...")
    
    # Fetch recent tickets to find #15706
    for page in range(1, 11):
        list_info = {
            "list_info": {
                "row_count": 100,
                "start_index": (page - 1) * 100 + 1,
                "sort_field": "id",
                "sort_order": "desc"
            }
        }
        
        url = f"https://{SDP_DOMAIN}/api/v3/requests"
        params = {"input_data": json.dumps(list_info)}
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            requests_data = data.get('requests', [])
            
            if not requests_data:
                break
            
            for ticket in requests_data:
                display_id_obj = ticket.get('display_id', {})
                if isinstance(display_id_obj, dict):
                    display_id = str(display_id_obj.get('display_value', ''))
                else:
                    display_id = str(display_id_obj)
                
                if display_id == "15706":
                    logger.info(f"✓ Found ticket #15706!")
                    return ticket
            
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Failed to fetch page {page}: {e}")
            break
    
    return None

# ============================================================
# COMPREHENSIVE CONTENT SEARCH
# ============================================================
def search_for_conversation_content(ticket: dict):
    """Exhaustive search for conversation content."""
    global TOKEN
    
    ticket_id = str(ticket.get('id', ''))
    display_id_obj = ticket.get('display_id', {})
    if isinstance(display_id_obj, dict):
        display_id = str(display_id_obj.get('display_value', ticket_id))
    else:
        display_id = str(display_id_obj) if display_id_obj else ticket_id
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    logger.info("")
    logger.info("=" * 80)
    logger.info(f"🔬 COMPREHENSIVE CONTENT SEARCH FOR TICKET #{display_id}")
    logger.info("=" * 80)
    logger.info("")
    
    # ============================================================
    # TEST 1: FULL TICKET OBJECT - Check ALL fields
    # ============================================================
    logger.info("=" * 80)
    logger.info("TEST 1: FULL TICKET OBJECT INSPECTION")
    logger.info("=" * 80)
    logger.info("")
    
    logger.info("📊 TOP-LEVEL TICKET FIELDS:")
    logger.info(f"  Available fields: {list(ticket.keys())}")
    logger.info("")
    
    # Check description field
    description = ticket.get('description', '')
    logger.info(f"📝 DESCRIPTION FIELD:")
    logger.info(f"  Type: {type(description)}")
    logger.info(f"  Length: {len(str(description)) if description else 0} chars")
    if description:
        logger.info(f"  Content preview (first 500 chars):")
        logger.info(f"  {str(description)[:500]}")
        logger.info("")
        logger.info(f"  Full content:")
        logger.info(f"  {description}")
    else:
        logger.info(f"  ❌ Empty or missing")
    logger.info("")
    
    # ============================================================
    # TEST 2: NOTIFICATIONS ENDPOINT - Full inspection
    # ============================================================
    logger.info("=" * 80)
    logger.info("TEST 2: NOTIFICATIONS ENDPOINT (FULL INSPECTION)")
    logger.info("=" * 80)
    logger.info("")
    
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications"
        logger.info(f"Endpoint: {url}")
        
        response = requests.get(url, headers=headers, timeout=30)
        logger.info(f"Status: {response.status_code}")
        logger.info("")
        
        if response.status_code == 200:
            data = response.json()
            notifications = data.get('notifications', [])
            
            logger.info(f"✓ Retrieved {len(notifications)} notifications")
            logger.info("")
            
            if notifications:
                logger.info("📊 FIRST 5 NOTIFICATIONS WITH COMPLETE STRUCTURE:")
                for idx, notif in enumerate(notifications[:5]):
                    logger.info(f"\n  Notification #{idx + 1}:")
                    logger.info(f"    All fields: {list(notif.keys())}")
                    
                    # Check for content fields
                    content_found = False
                    for field in ['description', 'content', 'body', 'text', 'message', 'html', 'plain']:
                        if field in notif and notif[field]:
                            logger.info(f"    ✅ HAS '{field}' field!")
                            logger.info(f"       Content: {str(notif[field])[:200]}")
                            content_found = True
                    
                    if not content_found:
                        logger.info(f"    ❌ No content fields found")
                    
                    # Show complete JSON for first 2
                    if idx < 2:
                        logger.info(f"\n    COMPLETE JSON:")
                        logger.info(json.dumps(notif, indent=6, default=str))
            else:
                logger.info("❌ No notifications found")
        else:
            logger.error(f"❌ Failed: {response.text[:500]}")
            
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    # ============================================================
    # TEST 3: TRY ALTERNATIVE ENDPOINTS
    # ============================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST 3: TRYING ALTERNATIVE ENDPOINTS")
    logger.info("=" * 80)
    logger.info("")
    
    alternative_endpoints = [
        f"/api/v3/requests/{ticket_id}/replies",
        f"/api/v3/requests/{ticket_id}/threads",
        f"/api/v3/requests/{ticket_id}/messages",
        f"/api/v3/requests/{ticket_id}/conversation_threads",
        f"/api/v3/requests/{ticket_id}/email_conversations",
        f"/api/v3/requests/{ticket_id}/responses"
    ]
    
    for endpoint_path in alternative_endpoints:
        url = f"https://{SDP_DOMAIN}{endpoint_path}"
        logger.info(f"Testing: {endpoint_path}")
        
        try:
            response = requests.get(url, headers=headers, timeout=30)
            logger.info(f"  Status: {response.status_code}")
            
            if response.status_code == 200:
                logger.info(f"  ✅ SUCCESS! Endpoint exists!")
                data = response.json()
                logger.info(f"  Response keys: {list(data.keys())}")
                logger.info(f"  Full response:")
                logger.info(json.dumps(data, indent=4, default=str)[:2000])
                logger.info("")
            elif response.status_code == 404:
                logger.info(f"  ❌ Not found")
            else:
                logger.info(f"  Response: {response.text[:200]}")
            
        except Exception as e:
            logger.info(f"  ❌ Error: {e}")
        
        logger.info("")
        time.sleep(0.3)
    
    # ============================================================
    # TEST 4: FETCH FULL TICKET DETAILS WITH ALL FIELDS
    # ============================================================
    logger.info("=" * 80)
    logger.info("TEST 4: FETCH COMPLETE TICKET DETAILS")
    logger.info("=" * 80)
    logger.info("")
    
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}"
        logger.info(f"Endpoint: {url}")
        
        response = requests.get(url, headers=headers, timeout=30)
        logger.info(f"Status: {response.status_code}")
        logger.info("")
        
        if response.status_code == 200:
            data = response.json()
            request_data = data.get('request', data)
            
            logger.info("📊 COMPLETE TICKET OBJECT:")
            logger.info(f"  All fields: {list(request_data.keys())}")
            logger.info("")
            
            # Check ALL fields for content
            logger.info("🔍 CHECKING ALL FIELDS FOR CONTENT:")
            for field_name, field_value in request_data.items():
                if isinstance(field_value, str) and len(field_value) > 50:
                    logger.info(f"\n  ✅ Field '{field_name}' has text content ({len(field_value)} chars):")
                    logger.info(f"     {field_value[:300]}")
            
            logger.info("")
            logger.info("COMPLETE TICKET JSON:")
            logger.info(json.dumps(request_data, indent=2, default=str))
            
        else:
            logger.error(f"❌ Failed: {response.text[:500]}")
            
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    # ============================================================
    # TEST 5: NOTES (we know this works)
    # ============================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST 5: NOTES ENDPOINT (KNOWN TO WORK)")
    logger.info("=" * 80)
    logger.info("")
    
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notes"
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            notes = data.get('notes', [])
            logger.info(f"✓ Retrieved {len(notes)} notes")
            
            if notes:
                for idx, note in enumerate(notes):
                    logger.info(f"\n  Note #{idx + 1}:")
                    logger.info(f"    Description: {note.get('description', 'N/A')}")
            else:
                logger.info("No notes found")
                
    except Exception as e:
        logger.error(f"❌ Exception: {e}")

# ============================================================
# MAIN
# ============================================================
def run_test():
    """Run comprehensive content search."""
    
    logger.info("=" * 80)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 80)
    logger.info("MISSION: Find where portal conversation content lives in the API")
    logger.info("")
    
    refresh_token()
    
    ticket = find_ticket_15706()
    
    if not ticket:
        logger.error("Could not find ticket #15706")
        return
    
    search_for_conversation_content(ticket)
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("SEARCH COMPLETE")
    logger.info("=" * 80)
    logger.info("")
    logger.info("Review the output above to find where conversation content exists!")

if __name__ == "__main__":
    try:
        logger.info("=" * 60)
        logger.info("VALIDATING ENVIRONMENT VARIABLES")
        logger.info("=" * 60)
        logger.info("")
        
        required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN']
        missing = [var for var in required_vars if not os.getenv(var)]
        
        if missing:
            logger.error(f"Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)
        
        logger.info("✓ Configuration loaded")
        logger.info("")
        
        run_test()
        
    except KeyboardInterrupt:
        logger.info("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
