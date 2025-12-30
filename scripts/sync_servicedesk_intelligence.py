#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.5.5-FINAL-EMAIL-CONTENT-TEST
MISSION: Exhaustive test to find email body content in ManageEngine API

This will test:
1. Individual notification fetching by ID
2. Various API parameters (?fields=all, ?include_body=true, etc.)
3. Different field specifications
4. Alternative headers/accept types
5. Raw API responses for hidden fields
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

VERSION = "v3.5.5-FINAL-EMAIL-CONTENT-TEST"

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
    """Find ticket #15706 quickly."""
    global TOKEN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.manageengine.sdp.v3+json"
    }
    
    logger.info("🔍 Searching for ticket #15706...")
    
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
            logger.error(f"Failed: {e}")
            break
    
    return None

# ============================================================
# EXHAUSTIVE EMAIL CONTENT TESTING
# ============================================================
def test_email_content_retrieval(ticket: dict):
    """Exhaustive testing for email content in API."""
    global TOKEN
    
    ticket_id = str(ticket.get('id', ''))
    display_id_obj = ticket.get('display_id', {})
    if isinstance(display_id_obj, dict):
        display_id = str(display_id_obj.get('display_value', ticket_id))
    else:
        display_id = str(display_id_obj) if display_id_obj else ticket_id
    
    logger.info("")
    logger.info("=" * 80)
    logger.info(f"🔬 EXHAUSTIVE EMAIL CONTENT TEST FOR TICKET #{display_id}")
    logger.info("=" * 80)
    logger.info("")
    
    # First, get notification list to get IDs
    logger.info("Step 1: Fetching notification list to get IDs...")
    
    headers_base = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications"
    
    try:
        response = requests.get(url, headers=headers_base, timeout=30)
        if response.status_code == 200:
            data = response.json()
            notifications = data.get('notifications', [])
            
            if not notifications:
                logger.error("No notifications found!")
                return
            
            logger.info(f"✓ Found {len(notifications)} notifications")
            
            # Find a REQREPLY notification (actual user reply)
            reqreply_notif = None
            for notif in notifications:
                if notif.get('type') == 'REQREPLY':
                    reqreply_notif = notif
                    break
            
            if not reqreply_notif:
                logger.warning("No REQREPLY notifications found, using first notification")
                reqreply_notif = notifications[0]
            
            notif_id = reqreply_notif.get('id')
            notif_type = reqreply_notif.get('type')
            notif_sender = reqreply_notif.get('sender', {}).get('name', 'Unknown')
            
            logger.info(f"\n📧 Testing with notification:")
            logger.info(f"  ID: {notif_id}")
            logger.info(f"  Type: {notif_type}")
            logger.info(f"  Sender: {notif_sender}")
            logger.info(f"  Subject: {reqreply_notif.get('subject', 'N/A')}")
            logger.info(f"  MessageID: {reqreply_notif.get('messageid', 'N/A')}")
            logger.info("")
            
            # ============================================================
            # TEST 1: Individual Notification by ID
            # ============================================================
            logger.info("=" * 80)
            logger.info("TEST 1: FETCH INDIVIDUAL NOTIFICATION BY ID")
            logger.info("=" * 80)
            logger.info("")
            
            individual_url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications/{notif_id}"
            logger.info(f"URL: {individual_url}")
            
            try:
                response = requests.get(individual_url, headers=headers_base, timeout=30)
                logger.info(f"Status: {response.status_code}")
                
                if response.status_code == 200:
                    logger.info("✅ SUCCESS!")
                    individual_data = response.json()
                    logger.info("\nComplete response:")
                    logger.info(json.dumps(individual_data, indent=2, default=str))
                    
                    # Check for content
                    notif_obj = individual_data.get('notification', individual_data)
                    if 'description' in notif_obj and notif_obj['description']:
                        logger.info(f"\n🎉 FOUND CONTENT in description!")
                        logger.info(f"Content: {notif_obj['description']}")
                    else:
                        logger.info(f"\n❌ Still no content in description field")
                else:
                    logger.info(f"❌ Failed: {response.text[:500]}")
            except Exception as e:
                logger.error(f"❌ Exception: {e}")
            
            logger.info("")
            
            # ============================================================
            # TEST 2: Try Different Parameters
            # ============================================================
            logger.info("=" * 80)
            logger.info("TEST 2: TRY VARIOUS API PARAMETERS")
            logger.info("=" * 80)
            logger.info("")
            
            test_params = [
                {'fields': 'all'},
                {'include_body': 'true'},
                {'include_description': 'true'},
                {'include_content': 'true'},
                {'format': 'complete'},
                {'expand': 'all'},
                {'view': 'full'},
                {'details': 'complete'}
            ]
            
            base_url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notifications/{notif_id}"
            
            for params in test_params:
                param_str = '&'.join([f"{k}={v}" for k, v in params.items()])
                test_url = f"{base_url}?{param_str}"
                logger.info(f"Testing: {param_str}")
                
                try:
                    response = requests.get(test_url, headers=headers_base, timeout=30)
                    logger.info(f"  Status: {response.status_code}")
                    
                    if response.status_code == 200:
                        data = response.json()
                        notif_obj = data.get('notification', data)
                        
                        if 'description' in notif_obj and notif_obj['description']:
                            logger.info(f"  🎉 SUCCESS! Found content with parameter: {param_str}")
                            logger.info(f"  Content: {notif_obj['description'][:200]}")
                            logger.info("")
                            logger.info("FULL RESPONSE:")
                            logger.info(json.dumps(data, indent=2, default=str))
                        else:
                            logger.info(f"  ❌ Still empty")
                    else:
                        logger.info(f"  ❌ Failed: {response.status_code}")
                        
                except Exception as e:
                    logger.info(f"  ❌ Error: {e}")
                
                logger.info("")
                time.sleep(0.3)
            
            # ============================================================
            # TEST 3: Try Different Accept Headers
            # ============================================================
            logger.info("=" * 80)
            logger.info("TEST 3: TRY DIFFERENT ACCEPT HEADERS")
            logger.info("=" * 80)
            logger.info("")
            
            test_headers = [
                {"Accept": "application/json"},
                {"Accept": "application/vnd.manageengine.sdp.v3+json"},
                {"Accept": "application/vnd.manageengine.sdp.v2+json"},
                {"Accept": "application/vnd.manageengine.sdp.v1+json"},
                {"Accept": "application/xml"},
                {"Accept": "*/*"}
            ]
            
            for test_header in test_headers:
                headers_test = headers_base.copy()
                headers_test.update(test_header)
                
                logger.info(f"Testing Accept: {test_header['Accept']}")
                
                try:
                    response = requests.get(individual_url, headers=headers_test, timeout=30)
                    logger.info(f"  Status: {response.status_code}")
                    
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            notif_obj = data.get('notification', data)
                            
                            if 'description' in notif_obj and notif_obj['description']:
                                logger.info(f"  🎉 SUCCESS! Found content with Accept: {test_header['Accept']}")
                                logger.info(f"  Content: {notif_obj['description'][:200]}")
                            else:
                                logger.info(f"  ❌ Still empty")
                        except:
                            logger.info(f"  Response (non-JSON): {response.text[:200]}")
                    else:
                        logger.info(f"  ❌ Failed")
                        
                except Exception as e:
                    logger.info(f"  ❌ Error: {e}")
                
                logger.info("")
                time.sleep(0.3)
            
            # ============================================================
            # TEST 4: Check Conversation Endpoint with Parameters
            # ============================================================
            logger.info("=" * 80)
            logger.info("TEST 4: CONVERSATIONS ENDPOINT WITH PARAMETERS")
            logger.info("=" * 80)
            logger.info("")
            
            # Get conversation ID (should match notification ID for REQREPLY)
            conv_url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/conversations"
            response = requests.get(conv_url, headers=headers_base, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                conversations = data.get('conversations', [])
                
                # Find matching conversation
                conv_id = None
                for conv in conversations:
                    if conv.get('type') == 'REQREPLY':
                        conv_id = conv.get('id')
                        break
                
                if conv_id:
                    logger.info(f"Found conversation ID: {conv_id}")
                    
                    test_params = [
                        {},
                        {'fields': 'all'},
                        {'include_description': 'true'},
                        {'expand': 'all'}
                    ]
                    
                    for params in test_params:
                        param_str = '&'.join([f"{k}={v}" for k, v in params.items()]) if params else 'No params'
                        logger.info(f"\nTesting conversations with: {param_str}")
                        
                        test_url = conv_url if not params else f"{conv_url}?{'&'.join([f'{k}={v}' for k, v in params.items()])}"
                        
                        try:
                            response = requests.get(test_url, headers=headers_base, timeout=30)
                            if response.status_code == 200:
                                data = response.json()
                                conversations = data.get('conversations', [])
                                
                                if conversations:
                                    first_conv = conversations[0]
                                    has_desc = 'description' in first_conv and first_conv['description']
                                    logger.info(f"  Status: 200, Has description: {has_desc}")
                                    
                                    if has_desc:
                                        logger.info(f"  🎉 FOUND CONTENT!")
                                        logger.info(f"  Content: {first_conv['description'][:200]}")
                                else:
                                    logger.info(f"  Status: 200, No conversations")
                            else:
                                logger.info(f"  Status: {response.status_code}")
                        except Exception as e:
                            logger.info(f"  Error: {e}")
                        
                        time.sleep(0.3)
            
            # ============================================================
            # TEST 5: Raw Response Inspection
            # ============================================================
            logger.info("")
            logger.info("=" * 80)
            logger.info("TEST 5: RAW RESPONSE INSPECTION")
            logger.info("=" * 80)
            logger.info("")
            
            logger.info("Checking for hidden/undocumented fields in notification object...")
            logger.info("\nAll fields in REQREPLY notification:")
            for key in reqreply_notif.keys():
                value = reqreply_notif[key]
                value_type = type(value).__name__
                value_preview = str(value)[:100] if value else '(empty)'
                logger.info(f"  {key} ({value_type}): {value_preview}")
            
    except Exception as e:
        logger.error(f"Failed to fetch notifications: {e}")

# ============================================================
# MAIN
# ============================================================
def run_test():
    """Run comprehensive email content test."""
    
    logger.info("=" * 80)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 80)
    logger.info("FINAL TEST: Exhaustive search for email body content")
    logger.info("")
    logger.info("This will test:")
    logger.info("  1. Individual notification fetching by ID")
    logger.info("  2. Various API parameters (?fields=all, etc.)")
    logger.info("  3. Different Accept headers")
    logger.info("  4. Conversation endpoint variations")
    logger.info("  5. Raw response field inspection")
    logger.info("")
    
    refresh_token()
    
    ticket = find_ticket_15706()
    
    if not ticket:
        logger.error("Could not find ticket #15706")
        return
    
    test_email_content_retrieval(ticket)
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST COMPLETE")
    logger.info("=" * 80)
    logger.info("")
    logger.info("📚 DOCUMENTATION RESEARCH NEEDED:")
    logger.info("")
    logger.info("Check these ManageEngine API documentation sources:")
    logger.info("")
    logger.info("1. Official API Docs:")
    logger.info("   https://www.manageengine.com/products/service-desk/sdpod-v3-api/")
    logger.info("   https://www.manageengine.com/products/service-desk/sdpod-v3-api/SDPOD-V3-API.html")
    logger.info("")
    logger.info("2. Notifications API Section:")
    logger.info("   Look for: GET /api/v3/requests/{request_id}/notifications/{id}")
    logger.info("   Check for: fields parameter, expand parameter, include options")
    logger.info("")
    logger.info("3. Search for:")
    logger.info("   - 'email body'")
    logger.info("   - 'notification content'")
    logger.info("   - 'reply content'")
    logger.info("   - 'message body'")
    logger.info("")
    logger.info("4. Community Forums:")
    logger.info("   https://pitstop.manageengine.com/portal/en/community/servicedesk-plus-cloud")
    logger.info("")
    logger.info("5. Support Contact:")
    logger.info("   If email content should be available, open a support ticket")
    logger.info("   asking how to retrieve email body content via API")

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
