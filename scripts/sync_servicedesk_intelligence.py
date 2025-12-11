#!/usr/bin/env python3
"""
ServiceDesk Intelligence Sync Script v3.0
Syncs ticket data from ServiceDesk Plus to Dust with parallel uploads

Changes in v3.0:
- Added parallel upload support (5 concurrent workers)
- Reduced upload delay from 0.5s to 0.1s
- Expected speedup: 10-15x faster (80 min → 5-8 min)
"""

import os
import sys
import json
import time
import logging
import requests
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone, timedelta
from pathlib import Path
import concurrent.futures
from threading import Semaphore
import threading

# Configuration
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
DUST_API_KEY = os.getenv('DUST_API_KEY')
DUST_WORKSPACE_ID = os.getenv('DUST_WORKSPACE_ID', 'MotoRad')
DATA_SOURCE_ID = 'servicedesk'

# API Endpoints
ZOHO_TOKEN_URL = 'https://accounts.zoho.com/oauth/v2/token'
SDP_API_BASE = 'https://sdpondemand.manageengine.com/api/v3'

# Parallel upload configuration
MAX_CONCURRENT_UPLOADS = 5  # Upload 5 tickets simultaneously
UPLOAD_TIMEOUT = 30  # Seconds per upload

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reusable session
SESSION = requests.Session()

# Excluded technicians (always filter out)
EXCLUDED_TECHNICIANS = ['Lori Hale', 'Zach McGee']

# Excluded statuses
EXCLUDED_STATUSES = ['Cancelled', 'Rejected', 'Declined']


def get_zoho_access_token() -> Optional[str]:
    """
    Get Zoho access token using refresh token
    """
    if not all([ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN]):
        logger.error("Missing Zoho credentials in environment variables")
        return None
    
    try:
        response = SESSION.post(
            ZOHO_TOKEN_URL,
            data={
                'refresh_token': ZOHO_REFRESH_TOKEN,
                'client_id': ZOHO_CLIENT_ID,
                'client_secret': ZOHO_CLIENT_SECRET,
                'grant_type': 'refresh_token'
            },
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            access_token = data.get('access_token')
            logger.info("✓ Zoho access token obtained")
            return access_token
        else:
            logger.error(f"Failed to get access token: {response.status_code} - {response.text}")
            return None
    
    except Exception as e:
        logger.error(f"Error getting access token: {e}")
        return None


def fetch_all_tickets(access_token: str) -> List[Dict]:
    """
    Fetch all tickets from ServiceDesk Plus using pagination
    Filters out excluded technicians and statuses
    """
    all_tickets = []
    start_index = 1
    row_count = 100  # Fetch 100 tickets per request
    
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    logger.info("Fetching tickets from ServiceDesk Plus...")
    
    while True:
        try:
            list_info = {
                "list_info": {
                    "row_count": row_count,
                    "start_index": start_index,
                    "get_total_count": True,
                    "search_fields": {
                        "status.name": {
                            "NOT IN": EXCLUDED_STATUSES
                        }
                    }
                }
            }
            
            url = f"{SDP_API_BASE}/requests"
            params = {'input_data': json.dumps(list_info)}
            
            response = SESSION.get(url, headers=headers, params=params, timeout=60)
            
            if response.status_code != 200:
                logger.error(f"API error: {response.status_code} - {response.text}")
                break
            
            data = response.json()
            requests_data = data.get('requests', [])
            
            # Filter out excluded technicians
            filtered_requests = [
                req for req in requests_data
                if req.get('technician', {}).get('name') not in EXCLUDED_TECHNICIANS
            ]
            
            all_tickets.extend(filtered_requests)
            
            total_count = data.get('list_info', {}).get('row_count', 0)
            logger.info(f"Fetched {len(all_tickets)} tickets so far... (filtered)")
            
            # Check if we've fetched all tickets
            if len(requests_data) < row_count:
                break
            
            start_index += row_count
            
            # Small delay to avoid rate limiting
            time.sleep(0.1)  # Reduced from 0.5s to 0.1s
        
        except Exception as e:
            logger.error(f"Error fetching tickets: {e}")
            break
    
    logger.info(f"✓ Total tickets fetched: {len(all_tickets)}")
    return all_tickets


def calculate_complexity(ticket: Dict) -> int:
    """
    Calculate ticket complexity score (0-10)
    Based on description length, priority, category, and conversation volume
    """
    score = 0
    
    # Priority weight
    priority = ticket.get('priority', {}).get('name', 'Normal')
    priority_scores = {
        'Urgent': 3,
        'High': 2,
        'Normal': 1,
        'Low': 0
    }
    score += priority_scores.get(priority, 1)
    
    # Description length
    description = ticket.get('description', '')
    if isinstance(description, str):
        if len(description) > 1000:
            score += 3
        elif len(description) > 500:
            score += 2
        elif len(description) > 100:
            score += 1
    
    # Category complexity
    category = ticket.get('category', {}).get('name', '')
    complex_categories = ['Network', 'Server', 'Database', 'Security', 'Hardware']
    if any(cat in category for cat in complex_categories):
        score += 2
    
    # Conversation volume
    conversations = ticket.get('conversations', [])
    if len(conversations) > 10:
        score += 2
    elif len(conversations) > 5:
        score += 1
    
    return min(score, 10)  # Cap at 10


def analyze_sentiment(ticket: Dict) -> str:
    """
    Analyze customer sentiment based on description and conversations
    Returns: positive, neutral, at-risk
    """
    text = ticket.get('description', '').lower()
    
    # Look for negative indicators
    negative_words = ['urgent', 'critical', 'broken', 'not working', 'failed', 'error', 'issue', 'problem']
    positive_words = ['thank', 'appreciate', 'working', 'resolved', 'fixed']
    
    negative_count = sum(1 for word in negative_words if word in text)
    positive_count = sum(1 for word in positive_words if word in text)
    
    if negative_count > positive_count + 2:
        return 'at-risk'
    elif positive_count > negative_count:
        return 'positive'
    else:
        return 'neutral'


def predict_sla_breach(ticket: Dict) -> Tuple[float, str]:
    """
    Predict likelihood of SLA breach (0-100%)
    Returns: (probability, risk_level)
    """
    due_date = ticket.get('due_by_time')
    if not due_date:
        return 0.0, 'low'
    
    try:
        due = datetime.fromisoformat(due_date.get('display_value', '').replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        hours_remaining = (due - now).total_seconds() / 3600
        
        # Calculate breach probability
        if hours_remaining < 0:
            probability = 100.0
            risk_level = 'critical'
        elif hours_remaining < 4:
            probability = 80.0
            risk_level = 'high'
        elif hours_remaining < 24:
            probability = 50.0
            risk_level = 'moderate'
        else:
            probability = 10.0
            risk_level = 'low'
        
        return probability, risk_level
    
    except Exception:
        return 0.0, 'low'


def extract_conversations(ticket: Dict) -> str:
    """
    Extract and format conversation history
    """
    conversations = ticket.get('conversations', [])
    if not conversations:
        return "No conversations"
    
    formatted = []
    for conv in conversations:
        sender = conv.get('sender', {}).get('name', 'Unknown')
        message = conv.get('description', '')
        timestamp = conv.get('created_time', {}).get('display_value', '')
        
        formatted.append(f"[{timestamp}] {sender}: {message}")
    
    return "\n\n".join(formatted)


def create_dust_document(ticket: Dict) -> Dict:
    """
    Create a Dust document from a ServiceDesk ticket with comprehensive metadata
    """
    ticket_id = ticket.get('id')
    
    # Basic info
    subject = ticket.get('subject', 'No Subject')
    description = ticket.get('description', '')
    status = ticket.get('status', {}).get('name', 'Unknown')
    priority = ticket.get('priority', {}).get('name', 'Normal')
    
    # People
    requester = ticket.get('requester', {}).get('name', 'Unknown')
    requester_email = ticket.get('requester', {}).get('email_id', '')
    technician = ticket.get('technician', {}).get('name', 'Unassigned')
    technician_email = ticket.get('technician', {}).get('email_id', '')
    
    # Timestamps
    created_time = ticket.get('created_time', {}).get('display_value', '')
    due_by_time = ticket.get('due_by_time', {}).get('display_value', '')
    resolved_time = ticket.get('resolved_time', {}).get('display_value', '')
    
    # Categories
    category = ticket.get('category', {}).get('name', '')
    subcategory = ticket.get('subcategory', {}).get('name', '')
    
    # AI-enhanced fields
    complexity = calculate_complexity(ticket)
    sentiment = analyze_sentiment(ticket)
    sla_breach_probability, sla_risk = predict_sla_breach(ticket)
    
    # Conversations
    conversations = extract_conversations(ticket)
    
    # Advanced fields
    asset = ticket.get('assets', [{}])[0].get('name', '') if ticket.get('assets') else ''
    department = ticket.get('department', {}).get('name', '')
    impact = ticket.get('impact', {}).get('name', '')
    urgency = ticket.get('urgency', {}).get('name', '')
    mode = ticket.get('mode', {}).get('name', '')
    level = ticket.get('level', {}).get('name', '')
    
    # Build comprehensive document text
    document_text = f"""# Ticket #{ticket_id}: {subject}

## Status & Priority
- **Status**: {status}
- **Priority**: {priority}
- **Complexity**: {complexity}/10
- **Sentiment**: {sentiment}
- **SLA Breach Risk**: {sla_risk} ({sla_breach_probability}%)

## People
- **Requester**: {requester} ({requester_email})
- **Technician**: {technician} ({technician_email})

## Timeline
- **Created**: {created_time}
- **Due By**: {due_by_time}
- **Resolved**: {resolved_time or 'Not resolved'}

## Classification
- **Category**: {category}
- **Subcategory**: {subcategory}
- **Department**: {department}
- **Impact**: {impact}
- **Urgency**: {urgency}

## Details
{description}

## Conversations
{conversations}

## Technical Details
- **Asset**: {asset}
- **Mode**: {mode}
- **Level**: {level}
- **Ticket ID**: {ticket_id}
"""
    
    # Tags for filtering
    tags = [
        f"status:{status.lower().replace(' ', '-')}",
        f"priority:{priority.lower()}",
        f"complexity:{complexity}",
        f"sentiment:{sentiment}",
        f"sla-risk:{sla_risk}",
        f"category:{category.lower().replace(' ', '-')}",
        f"technician:{technician.lower().replace(' ', '-')}",
        f"requester:{requester.lower().replace(' ', '-')}"
    ]
    
    if 'Overdue' in status:
        tags.append('overdue')
    
    if not technician_email:
        tags.append('unassigned')
    
    return {
        "document_id": f"request-{ticket_id}",
        "timestamp": int(time.time()),
        "tags": tags,
        "source_url": f"https://sdpondemand.manageengine.com/app/itdesk/ui/requests/{ticket_id}",
        "text": document_text
    }


def upload_to_dust_parallel(documents: List[Dict]) -> Tuple[int, int]:
    """
    Upload documents to Dust in parallel with rate limiting
    v3.0: Parallel uploads for 3-5x speed improvement
    
    Args:
        documents: List of document dictionaries to upload
    
    Returns:
        Tuple of (uploaded_count, error_count)
    """
    if not DUST_API_KEY or not DUST_WORKSPACE_ID:
        logger.warning("Dust credentials not configured. Skipping upload.")
        return 0, 0
    
    uploaded = 0
    errors = 0
    upload_lock = threading.Lock()
    rate_limiter = Semaphore(MAX_CONCURRENT_UPLOADS)
    
    def upload_single_document(doc: Dict) -> bool:
        """Upload a single document with retry logic"""
        nonlocal uploaded, errors
        
        with rate_limiter:  # Limit concurrent uploads
            url = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DATA_SOURCE_ID}/documents/{doc['document_id']}"
            headers = {
                'Authorization': f'Bearer {DUST_API_KEY}',
                'Content-Type': 'application/json'
            }
            
            # Retry logic (up to 3 attempts)
            for attempt in range(3):
                try:
                    response = SESSION.post(
                        url,
                        headers=headers,
                        json=doc,
                        timeout=UPLOAD_TIMEOUT
                    )
                    
                    if response.status_code in [200, 201]:
                        with upload_lock:
                            uploaded += 1
                        if uploaded % 100 == 0:  # Log every 100 uploads
                            logger.info(f"Progress: {uploaded} documents uploaded...")
                        return True
                    
                    elif response.status_code == 429:  # Rate limited
                        wait_time = (attempt + 1) * 2
                        logger.debug(f"Rate limited, waiting {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    
                    elif response.status_code in [502, 503, 504]:  # Server errors
                        wait_time = (attempt + 1) * 1
                        logger.debug(f"Server error {response.status_code}, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    
                    else:  # Other errors
                        with upload_lock:
                            errors += 1
                        logger.warning(f"Upload failed for {doc['document_id']}: HTTP {response.status_code}")
                        return False
                
                except requests.exceptions.Timeout:
                    if attempt == 2:  # Last attempt
                        with upload_lock:
                            errors += 1
                        logger.warning(f"Timeout uploading {doc['document_id']}")
                        return False
                    time.sleep(1)
                
                except Exception as e:
                    if attempt == 2:  # Last attempt
                        with upload_lock:
                            errors += 1
                        logger.warning(f"Error uploading {doc['document_id']}: {str(e)}")
                        return False
                    time.sleep(1)
            
            # Small delay to be nice to the API
            time.sleep(0.1)  # Reduced from 0.5s
            return False
    
    # Upload documents in parallel using ThreadPoolExecutor
    logger.info(f"Uploading {len(documents)} documents with {MAX_CONCURRENT_UPLOADS} concurrent workers...")
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS) as executor:
        # Submit all upload tasks
        futures = [executor.submit(upload_single_document, doc) for doc in documents]
        
        # Wait for all to complete
        concurrent.futures.wait(futures)
    
    elapsed = time.time() - start_time
    logger.info(f"Upload complete in {elapsed:.1f}s: {uploaded} succeeded, {errors} failed")
    logger.info(f"Average: {elapsed/len(documents):.2f}s per document")
    
    return uploaded, errors


def main():
    """
    Main sync function
    """
    start_time = time.time()
    
    logger.info("="*60)
    logger.info("ServiceDesk Intelligence Sync v3.0 - Starting")
    logger.info("Optimizations: Parallel uploads + Reduced delays")
    logger.info("="*60)
    
    # Get access token
    access_token = get_zoho_access_token()
    if not access_token:
        logger.error("Failed to get access token. Exiting.")
        return
    
    # Fetch all tickets
    tickets = fetch_all_tickets(access_token)
    if not tickets:
        logger.warning("No tickets fetched. Exiting.")
        return
    
    logger.info(f"Processing {len(tickets)} tickets...")
    
    # Create Dust documents
    documents = []
    for ticket in tickets:
        try:
            doc = create_dust_document(ticket)
            documents.append(doc)
        except Exception as e:
            logger.warning(f"Error processing ticket {ticket.get('id')}: {e}")
    
    logger.info(f"Created {len(documents)} documents")
    
    # Upload to Dust (parallel)
    uploaded, errors = upload_to_dust_parallel(documents)
    
    # Summary
    elapsed = time.time() - start_time
    logger.info("="*60)
    logger.info("Sync Complete!")
    logger.info(f"Total time: {elapsed/60:.1f} minutes")
    logger.info(f"Tickets processed: {len(tickets)}")
    logger.info(f"Documents uploaded: {uploaded}")
    logger.info(f"Errors: {errors}")
    logger.info(f"Success rate: {(uploaded/len(documents)*100):.1f}%")
    logger.info("="*60)


if __name__ == '__main__':
    main()
