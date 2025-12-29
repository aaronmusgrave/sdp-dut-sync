#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.5.3-TEST-SPECIFIC-TICKET
TEST: Fetch conversations from ticket #15706 (known to have replies)
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

# Load and clean all secrets
SDP_DOMAIN = clean_secret(os.getenv('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_secret(os.getenv('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_secret(os.getenv('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_secret(os.getenv('SDP_REFRESH_TOKEN', ''))

VERSION = "v3.5.3-TEST-SPECIFIC-TICKET"

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
        
        logger.info(f"✓ Token refreshed successfully")
        
    except Exception as e:
        logger.error(f"Failed to refresh token: {e}")
        raise

# ============================================================
# FIND TICKET BY DISPLAY ID
# ============================================================
def find_ticket_by_display_id(display_id: str) -> Optional[dict]:
    """Find a ticket by its display ID (e.g., 15706)."""
    global TOKEN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Search for ticket by display ID
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "display_id",
                    "condition": "is",
                    "value": display_id
                }
            ]
        }
    }
    
    url = f"https://{SDP_DOMAIN}/api/v3/requests"
    params = {"input_data": json.dumps(list_info)}
    
    try:
        logger.info(f"🔍 Searching for ticket #{display_id}...")
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        
        data = response.json()
        requests_data = data.get('requests', [])
        
        if requests_data:
            ticket = requests_data[0]
            logger.info(f"✓ Found ticket #{display_id}")
            logger.info(f"  Subject: {ticket.get('subject', 'Unknown')}")
            logger.info(f"  Internal ID: {ticket.get('id')}")
            return ticket
        else:
            logger.error(f"❌ Ticket #{display_id} not found")
            return None
            
    except Exception as e:
        logger.error(f"Failed to find ticket: {e}")
        return None

# ============================================================
# TEST CONVERSATION FETCHING
# ============================================================
def test_conversations_for_ticket(ticket: dict):
    """Test conversation fetching for a specific ticket."""
    global TOKEN
    
    ticket_id = str(ticket.get('id', ''))
    display_id = ticket.get('display_id', {})
    if isinstance(display_id, dict):
        display_id = display_id.get('display_value', ticket_id)
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {TOKEN}",
        "Content-Type": "application/json"
    }
    
    logger.info("")
    logger.info("=" * 80)
    logger.info(f"🔬 TESTING CONVERSATIONS FOR TICKET #{display_id}")
    logger.info("=" * 80)
    logger.info("")
    
    # Test 1: Fetch conversation list
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/conversations"
        logger.info(f"📋 Endpoint: {url}")
        logger.info("")
        
        response = requests.get(url, headers=headers, timeout=30)
        logger.info(f"Response Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            conversations = data.get('conversations', [])
            
            logger.info(f"✓ Retrieved {len(conversations)} conversations")
            logger.info("")
            
            if conversations:
                logger.info("📊 CONVERSATION LIST:")
                for idx, conv in enumerate(conversations[:10]):  # Show first 10
                    logger.info(f"\n  Conversation #{idx + 1}:")
                    logger.info(f"    ID: {conv.get('id')}")
                    logger.info(f"    Type: {conv.get('type')}")
                    logger.info(f"    Created by: {conv.get('created_by', {}).get('name', 'Unknown')}")
                    logger.info(f"    Show to requester: {conv.get('show_to_requester', False)}")
                    logger.info(f"    Created: {conv.get('created_time', {}).get('display_value', 'Unknown')}")
                    logger.info(f"    Has description: {'description' in conv}")
                    logger.info(f"    Has content: {'content' in conv}")
                    logger.info(f"    Has from: {'from' in conv}")
                    logger.info(f"    All fields: {list(conv.keys())}")
                    
                    # Show complete JSON for first 3
                    if idx < 3:
                        logger.info(f"\n    COMPLETE JSON:")
                        logger.info(json.dumps(conv, indent=6, default=str))
                
                logger.info("")
                logger.info("=" * 80)
                logger.info("🔍 ATTEMPTING INDIVIDUAL CONVERSATION FETCH")
                logger.info("=" * 80)
                logger.info("")
                
                # Try first conversation individually
                first_conv_id = conversations[0].get('id')
                if first_conv_id:
                    individual_url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/conversations/{first_conv_id}"
                    logger.info(f"Endpoint: {individual_url}")
                    
                    response = requests.get(individual_url, headers=headers, timeout=30)
                    logger.info(f"Response Status: {response.status_code}")
                    logger.info("")
                    
                    if response.status_code == 200:
                        individual_data = response.json()
                        logger.info("✅ SUCCESS! Individual conversation retrieved:")
                        logger.info(json.dumps(individual_data, indent=2, default=str))
                    else:
                        logger.info(f"❌ Failed: {response.text}")
            else:
                logger.info("❌ No conversations found in response")
        else:
            logger.error(f"❌ Failed to fetch conversations: {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Exception: {e}")
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("🔍 TESTING NOTES ENDPOINT")
    logger.info("=" * 80)
    logger.info("")
    
    # Test 2: Fetch notes
    try:
        url = f"https://{SDP_DOMAIN}/api/v3/requests/{ticket_id}/notes"
        logger.info(f"📋 Endpoint: {url}")
        logger.info("")
        
        response = requests.get(url, headers=headers, timeout=30)
        logger.info(f"Response Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            notes = data.get('notes', [])
            
            logger.info(f"✓ Retrieved {len(notes)} notes")
            logger.info("")
            
            if notes:
                logger.info("📊 NOTES:")
                for idx, note in enumerate(notes[:5]):  # Show first 5
                    logger.info(f"\n  Note #{idx + 1}:")
                    logger.info(f"    ID: {note.get('id')}")
                    logger.info(f"    Created by: {note.get('created_by', {}).get('name', 'Unknown')}")
                    logger.info(f"    Description: {note.get('description', 'N/A')[:200]}")
                    logger.info(f"    Show to requester: {note.get('show_to_requester', False)}")
                    
                    if idx == 0:
                        logger.info(f"\n    COMPLETE JSON:")
                        logger.info(json.dumps(note, indent=6, default=str))
            else:
                logger.info("No notes found")
        else:
            logger.error(f"❌ Failed to fetch notes: {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Exception: {e}")

# ============================================================
# MAIN TEST
# ============================================================
def run_test():
    """Run the test."""
    
    logger.info("=" * 80)
    logger.info(f"SERVICEDESK INTELLIGENCE SUITE {VERSION}")
    logger.info("=" * 80)
    logger.info("TEST: Specific Ticket #15706 (Known to have conversations)")
    logger.info("")
    
    refresh_token()
    
    # Find ticket #15706
    ticket = find_ticket_by_display_id("15706")
    
    if not ticket:
        logger.error("Could not find ticket #15706")
        return
    
    # Test conversations
    test_conversations_for_ticket(ticket)
    
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
