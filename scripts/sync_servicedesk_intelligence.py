#!/usr/bin/env python3
"""
ServiceDesk Conversation Diagnostic Tool
=========================================

Fetches raw conversation data to diagnose why text content is empty.
"""

import os
import sys
import requests
import json

# ============================================================
# VALIDATE ENVIRONMENT FIRST
# ============================================================

def validate_env():
    """Validate environment variables"""
    required = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN']
    missing = []
    
    for var in required:
        value = os.environ.get(var)
        if not value or not value.strip():
            missing.append(var)
    
    if missing:
        print("=" * 80)
        print("❌ MISSING REQUIRED ENVIRONMENT VARIABLES")
        print("=" * 80)
        for var in missing:
            print(f"  • {var}")
        print("=" * 80)
        sys.exit(1)
    
    # Clean SDP_DOMAIN
    sdp_domain = os.environ.get('SDP_DOMAIN', '').strip()
    sdp_domain = sdp_domain.replace('https://', '').replace('http://', '')
    sdp_domain = sdp_domain.lstrip('\\').lstrip('/').rstrip('/').strip()
    
    if not sdp_domain or len(sdp_domain) < 5:
        print("❌ INVALID SDP_DOMAIN")
        sys.exit(1)
    
    os.environ['SDP_DOMAIN'] = sdp_domain
    return True

validate_env()

# ============================================================
# CONFIGURATION
# ============================================================

SDP_DOMAIN = os.environ.get("SDP_DOMAIN", "").strip()
SDP_CLIENT_ID = os.environ.get("SDP_CLIENT_ID", "").strip().lstrip('\\').lstrip('/')
SDP_CLIENT_SECRET = os.environ.get("SDP_CLIENT_SECRET", "").strip().lstrip('\\').lstrip('/')
SDP_REFRESH_TOKEN = os.environ.get("SDP_REFRESH_TOKEN", "").strip().lstrip('\\').lstrip('/')
SDP_BASE_URL = f"https://{SDP_DOMAIN}"
ZOHO_OAUTH_URL = "https://accounts.zoho.com/oauth/v2/token"

# Test with these specific ticket IDs
TEST_TICKET_IDS = [
    "15831",  # Order Originator Report - 10 conversations
    "15767",  # GRV - 10 conversations
    "15900",  # 10 conversations (Hebrew)
]

print("=" * 80)
print("SERVICEDESK CONVERSATION DIAGNOSTIC TOOL")
print("=" * 80)
print(f"Target Domain: {SDP_DOMAIN}")
print(f"Test Tickets: {', '.join(TEST_TICKET_IDS)}")
print("=" * 80)
print()

# ============================================================
# TOKEN MANAGEMENT
# ============================================================

def get_access_token() -> str:
    """Get OAuth access token"""
    print("🔑 Fetching access token...")
    
    payload = {
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'refresh_token': SDP_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    
    print(f"  OAuth URL: {ZOHO_OAUTH_URL}")
    print(f"  Client ID: {SDP_CLIENT_ID[:10]}...{SDP_CLIENT_ID[-4:]}")
    print(f"  Client Secret: {SDP_CLIENT_SECRET[:4]}...{SDP_CLIENT_SECRET[-4:]}")
    print(f"  Refresh Token: {SDP_REFRESH_TOKEN[:10]}...{SDP_REFRESH_TOKEN[-4:]}")
    
    try:
        response = requests.post(ZOHO_OAUTH_URL, data=payload, timeout=30)
        
        print(f"  Response Status: {response.status_code}")
        
        if response.status_code == 400:
            try:
                error_data = response.json()
                error_msg = error_data.get('error', 'unknown')
                print(f"  ❌ OAuth Error: {error_msg}")
                if error_msg == 'invalid_client':
                    print("  💡 Client ID or Client Secret is incorrect")
                elif error_msg == 'invalid_grant':
                    print("  💡 Refresh Token is invalid or expired")
                print(f"  Response: {response.text[:500]}")
            except:
                print(f"  Response: {response.text[:500]}")
        
        response.raise_for_status()
        
        token_data = response.json()
        access_token = token_data['access_token']
        expires_in = token_data.get('expires_in', 3600)
        
        print(f"✓ Token obtained (expires in {expires_in/60:.0f} minutes)\n")
        return access_token
        
    except Exception as e:
        print(f"❌ Failed to get token: {e}")
        sys.exit(1)

# ============================================================
# TICKET & CONVERSATION FETCHING
# ============================================================

def fetch_ticket_by_display_id(display_id: str, access_token: str):
    """Fetch ticket details by display ID"""
    print(f"📋 Fetching ticket #{display_id}...")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    list_info = {
        "list_info": {
            "row_count": 1,
            "search_criteria": {
                "field": "display_id",
                "condition": "is",
                "value": display_id
            }
        }
    }
    
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_BASE_URL}/api/v3/requests"
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        requests_list = data.get('requests', [])
        
        if not requests_list:
            print(f"  ⚠️  Ticket #{display_id} not found")
            return None
        
        ticket = requests_list[0]
        ticket_id = ticket.get('id')
        subject = ticket.get('subject', 'No Subject')
        
        print(f"  ✓ Found: ID={ticket_id}, Subject='{subject[:50]}'")
        return ticket
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return None

def fetch_conversations_raw(ticket_id: str, access_token: str):
    """Fetch RAW conversation data"""
    print(f"💬 Fetching conversations for ticket ID {ticket_id}...")
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    url = f"{SDP_BASE_URL}/api/v3/requests/{ticket_id}/conversations"
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        conversations = data.get('conversations', [])
        
        print(f"  ✓ Retrieved {len(conversations)} conversations")
        print(f"  📊 Response size: {len(json.dumps(data))} bytes")
        
        return data
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return None

# ============================================================
# DIAGNOSTIC ANALYSIS
# ============================================================

def analyze_conversation_structure(conv_data: dict, ticket_display_id: str):
    """Analyze conversation data structure"""
    print()
    print("=" * 80)
    print(f"DIAGNOSTIC REPORT: Ticket #{ticket_display_id}")
    print("=" * 80)
    
    conversations = conv_data.get('conversations', [])
    
    if not conversations:
        print("⚠️  No conversations found")
        return
    
    print(f"\n📈 Total Conversations: {len(conversations)}")
    print()
    
    # Analyze first conversation in detail
    print("-" * 80)
    print("DETAILED ANALYSIS: First Conversation")
    print("-" * 80)
    
    first_conv = conversations[0]
    
    print("\n🔍 Available Fields:")
    for key in sorted(first_conv.keys()):
        value = first_conv[key]
        value_type = type(value).__name__
        
        if isinstance(value, dict):
            print(f"  • {key}: {value_type} with {len(value)} keys")
            for subkey in list(value.keys())[:5]:  # Show first 5 subkeys
                subvalue = value[subkey]
                print(f"      - {subkey}: {type(subvalue).__name__}")
        elif isinstance(value, list):
            print(f"  • {key}: {value_type} with {len(value)} items")
        elif isinstance(value, str):
            preview = value[:100] if len(value) > 100 else value
            print(f"  • {key}: {value_type} = '{preview}'")
        else:
            print(f"  • {key}: {value_type} = {value}")
    
    print("\n" + "-" * 80)
    print("RAW JSON: First Conversation")
    print("-" * 80)
    print(json.dumps(first_conv, indent=2, ensure_ascii=False)[:2000])  # First 2000 chars
    
    # Check all common text fields
    print("\n" + "-" * 80)
    print("TEXT FIELD ANALYSIS: All Conversations")
    print("-" * 80)
    
    text_fields = ['text', 'description', 'reply', 'message', 'content', 'body', 
                   'comment', 'notes', 'html_text', 'plain_text', 'reply_text']
    
    for i, conv in enumerate(conversations[:5], 1):  # First 5 conversations
        print(f"\nConversation {i}:")
        print(f"  Sender: {conv.get('sender', {}).get('name', 'Unknown')}")
        print(f"  Time: {conv.get('created_time', {}).get('display_value', 'Unknown')}")
        
        found_text = False
        for field in text_fields:
            if field in conv:
                value = conv[field]
                if value:
                    found_text = True
                    if isinstance(value, str):
                        preview = value[:200].replace('\n', ' ') if len(value) > 200 else value.replace('\n', ' ')
                        print(f"  ✓ {field}: '{preview}'")
                        print(f"    (Length: {len(value)} chars)")
                    else:
                        print(f"  ✓ {field}: {type(value).__name__}")
        
        if not found_text:
            print(f"  ❌ No text content found in any common field")
            # Show ALL fields for this conversation
            print(f"  📋 All fields present: {', '.join(conv.keys())}")
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    has_text_count = 0
    for conv in conversations:
        for field in text_fields:
            if field in conv and conv[field]:
                has_text_count += 1
                break
    
    print(f"Conversations with text: {has_text_count}/{len(conversations)}")
    
    if has_text_count == 0:
        print("\n⚠️  WARNING: No conversations contain text in standard fields!")
        print("   Possible causes:")
        print("   1. API token lacks 'Read Conversation' permission")
        print("   2. Text is stored in a non-standard field")
        print("   3. Conversations are actually empty in the system")
        print("   4. Text requires additional API call per conversation")
    
    print()

# ============================================================
# MAIN
# ============================================================

def main():
    # Get token
    access_token = get_access_token()
    
    # Test each ticket
    for display_id in TEST_TICKET_IDS:
        print()
        print("=" * 80)
        
        # Fetch ticket
        ticket = fetch_ticket_by_display_id(display_id, access_token)
        if not ticket:
            continue
        
        ticket_id = ticket.get('id')
        
        # Fetch conversations
        conv_data = fetch_conversations_raw(ticket_id, access_token)
        if not conv_data:
            continue
        
        # Analyze structure
        analyze_conversation_structure(conv_data, display_id)
        
        # Save to file for further inspection
        filename = f"conversation_diagnostic_{display_id}.json"
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(conv_data, f, indent=2, ensure_ascii=False)
            print(f"💾 Full response saved to: {filename}")
        except:
            print(f"⚠️  Could not save to file (may not have write permissions)")
        
        print()
        
        # Only do first ticket by default
        break
    
    print()
    print("=" * 80)
    print("✓ DIAGNOSTIC COMPLETE")
    print("=" * 80)
    print()

if __name__ == "__main__":
    main()
