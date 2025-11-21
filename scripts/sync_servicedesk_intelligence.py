#!/usr/bin/env python3
"""
ServiceDesk Plus Intelligence Suite - Incremental Sync Version
VERSION: 2.0 - Incremental updates for near real-time intelligence
"""

import os
import requests
import json
from datetime import datetime, timedelta
import time
import re
from html import unescape
from collections import defaultdict, Counter
import statistics

# Configuration
SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')
DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

# API Endpoints
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"
DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Settings
TICKETS_PER_PAGE = 100
MAX_TICKETS = 10000
MAX_RETRIES = 3

# Analytics Cache
analytics_cache = {
    'category_stats': defaultdict(lambda: {'count': 0, 'avg_resolution_days': [], 'overdue_count': 0}),
    'technician_stats': defaultdict(lambda: {'active': 0, 'total': 0, 'avg_response_hours': []}),
    'hourly_distribution': defaultdict(int),
    'daily_distribution': defaultdict(int),
    'issue_keywords': Counter(),
    'resolution_patterns': defaultdict(list)
}

# Token cache
_access_token = None
_token_expiry = None

def get_access_token(force_refresh=False):
    """Get a valid access token using refresh token"""
    global _access_token, _token_expiry
    
    if not force_refresh and _access_token and _token_expiry and datetime.now() < _token_expiry:
        return _access_token
    
    print("Getting access token...")
    
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        data = response.json()
        if 'access_token' not in data:
            raise Exception(f"No access_token in response")
        
        _access_token = data['access_token']
        expires_in = data.get('expires_in', 3600)
        _token_expiry = datetime.now() + timedelta(seconds=expires_in - 300)
        print("Access token obtained")
        return _access_token
    else:
        raise Exception(f"Failed to get access token: {response.status_code}")

def fetch_all_tickets():
    """Fetch tickets updated since last sync (incremental) or all tickets (initial)"""
    access_token = get_access_token()
    all_tickets = []
    seen_ticket_ids = set()
    start_index = 1
    
    # Check for last sync timestamp
    last_sync_time = os.environ.get('LAST_SYNC_TIME')
    
    if last_sync_time:
        try:
            last_sync_ms = int(last_sync_time)
            last_sync_dt = datetime.fromtimestamp(last_sync_ms / 1000)
            print(f"📊 Incremental sync: Fetching tickets updated since {last_sync_dt}")
            incremental = True
        except:
            print("⚠️  Invalid last sync time, performing full sync")
            incremental = False
    else:
        print("🔄 No last sync time found - performing initial full sync")
        incremental = False
    
    print(f"Fetching {'updated' if incremental else 'all'} tickets...")
    
    while len(all_tickets) < MAX_TICKETS:
        headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Accept': 'application/vnd.manageengine.sdp.v3+json'
        }
        
        list_info = {
            "list_info": {
                "row_count": TICKETS_PER_PAGE,
                "start_index": start_index,
                "sort_field": "last_updated_time",
                "sort_order": "desc"
            }
        }
        
        params = {'input_data': json.dumps(list_info)}
        url = f"{SDP_API_BASE}/requests"
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                tickets = result.get('requests', [])
                
                if not tickets:
                    break
                
                # For incremental sync, filter and stop when reaching old tickets
                if incremental:
                    new_tickets_found = False
                    for ticket in tickets:
                        ticket_updated = ticket.get('last_updated_time', {})
                        ticket_updated_ms = None
                        
                        if isinstance(ticket_updated, dict):
                            ticket_updated_ms = ticket_updated.get('value')
                        else:
                            ticket_updated_ms = ticket_updated
                        
                        try:
                            ticket_updated_ms = int(ticket_updated_ms)
                            if ticket_updated_ms > last_sync_ms:
                                ticket_id = ticket.get('id')
                                if ticket_id not in seen_ticket_ids:
                                    all_tickets.append(ticket)
                                    seen_ticket_ids.add(ticket_id)
                                    new_tickets_found = True
                        except:
                            continue
                    
                    if not new_tickets_found:
                        break
                else:
                    for ticket in tickets:
                        ticket_id = ticket.get('id')
                        if ticket_id not in seen_ticket_ids:
                            all_tickets.append(ticket)
                            seen_ticket_ids.add(ticket_id)
                
                response_list_info = result.get('list_info', {})
                has_more = response_list_info.get('has_more_rows', False)
                
                if not has_more or len(tickets) == 0:
                    break
                
                start_index += TICKETS_PER_PAGE
                time.sleep(0.5)
                
            else:
                break
                
        except Exception as e:
            break
    
    sync_type = "incremental" if incremental else "full"
    print(f"✓ Fetched {len(all_tickets)} tickets ({sync_type} sync)")
    return all_tickets

def fetch_with_retry(url, headers, max_retries=MAX_RETRIES, timeout=30):
    """Fetch URL with retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 401:
                access_token = get_access_token(force_refresh=True)
                headers['Authorization'] = f'Zoho-oauthtoken {access_token}'
                continue
            elif response.status_code == 429:
                time.sleep(5)
                continue
            else:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
        except:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
    
    return None

def fetch_ticket_details(ticket_id, access_token):
    """Fetch detailed information for a single ticket"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    url = f"{SDP_API_BASE}/requests/{ticket_id}"
    
    response = fetch_with_retry(url, headers)
    
    if response and response.status_code == 200:
        return response.json().get('request', {})
    return None

def fetch_ticket_conversations(ticket_id, access_token):
    """Fetch ticket conversations with full text content"""
    headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
    
    url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return []
        
        notifications = response.json().get('notifications', [])
        conversations = []
        
        for notif in notifications[:20]:
            notif_id = notif.get('id')
            notif_type = notif.get('type', '')
            
            if any(keyword in notif_type.lower() for keyword in ['conversation', 'reply', 'reqreply', 'technician']):
                try:
                    detail_url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications/{notif_id}"
                    detail_response = requests.get(detail_url, headers=headers, timeout=10)
                    
                    if detail_response.status_code == 200:
                        detail_data = detail_response.json()
                        notification = detail_data.get('notification', {})
                        
                        description = notification.get('description', '')
                        if description and len(description.strip()) > 0:
                            sender = notification.get('sender', {})
                            time_obj = notification.get('time', {})
                            
                            conversations.append({
                                'id': notif_id,
                                'type': notif_type,
                                'sender': sender.get('name', 'Unknown'),
                                'sender_email': sender.get('email_id', ''),
                                'time': time_obj.get('display_value', 'Unknown'),
                                'time_ms': time_obj.get('value'),
                                'description': description,
                                'is_public': notification.get('is_public', True)
                            })
                    
                    time.sleep(0.1)
                    
                except Exception as e:
                    continue
        
        return conversations
        
    except Exception as e:
        return []

def strip_html(html_text):
    """Remove HTML tags and decode HTML entities"""
    if not html_text:
        return ""
    
    try:
        text = unescape(html_text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except:
        return str(html_text)

def safe_string(value, default=''):
    """Safely convert to string"""
    if value is None:
        return default
    try:
        return str(value).strip()
    except:
        return default

def safe_get_name(obj, default='Unknown'):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('name', default))
    return safe_string(obj, default)

def safe_get_display_value(obj, default=''):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_string(obj.get('display_value', default))
    return safe_string(obj, default)

def safe_get_value(obj, default=None):
    """Get the value field from a dict"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get('value', default)
        if val is not None:
            try:
                return int(val)
            except:
                return default
        return default
    try:
        return int(obj)
    except:
        return default

def safe_int_conversion(value, default=None):
    """Safely convert any value to int"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def get_display_id(details):
    """Extract the display ID"""
    if 'display_id' in details:
        display_id = details['display_id']
        if isinstance(display_id, dict):
            return safe_string(display_id.get('display_value', display_id.get('value', details.get('id'))))
        return safe_string(display_id)
    
    return safe_string(details.get('id', 'Unknown'))

def extract_keywords(text):
    """Extract key issue keywords from text"""
    if not text:
        return []
    
    text_lower = text.lower()
    
    keywords = []
    patterns = {
        'access': ['cannot access', 'access denied', 'login', 'password', 'locked out', 'permission'],
        'performance': ['slow', 'hanging', 'freeze', 'timeout', 'not responding', 'lag'],
        'error': ['error', 'exception', 'failed', 'crash', 'bug', 'issue'],
        'hardware': ['laptop', 'computer', 'printer', 'monitor', 'keyboard', 'mouse', 'device'],
        'software': ['application', 'program', 'software', 'app', 'system'],
        'network': ['network', 'internet', 'wifi', 'connection', 'vpn', 'offline'],
        'email': ['email', 'outlook', 'mail', 'inbox', 'send', 'receive'],
        'account': ['account', 'user', 'profile', 'credentials']
    }
    
    for category, terms in patterns.items():
        if any(term in text_lower for term in terms):
            keywords.append(category)
            analytics_cache['issue_keywords'][category] += 1
    
    return keywords

def calculate_complexity_score(ticket_data, conversations, age_days):
    """Calculate ticket complexity score (0-10)"""
    score = 0
    
    if len(conversations) > 10:
        score += 3
    elif len(conversations) > 5:
        score += 2
    elif len(conversations) > 2:
        score += 1
    
    if age_days > 30:
        score += 3
    elif age_days > 14:
        score += 2
    elif age_days > 7:
        score += 1
    
    if ticket_data.get('is_escalated'):
        score += 2
    if ticket_data.get('is_overdue'):
        score += 1
    
    priority = ticket_data.get('priority', '').lower()
    if priority == 'urgent':
        score += 1
    
    return min(score, 10)

def predict_resolution_time(category, priority, complexity_score):
    """Predict resolution time based on historical patterns"""
    
    if category in analytics_cache['category_stats']:
        cat_stats = analytics_cache['category_stats'][category]
        if cat_stats['avg_resolution_days']:
            avg_days = statistics.mean(cat_stats['avg_resolution_days'])
        else:
            avg_days = 3.0
    else:
        avg_days = 3.0
    
    priority_multipliers = {
        'urgent': 0.5,
        'high': 0.75,
        'normal': 1.0,
        'low': 1.5
    }
    multiplier = priority_multipliers.get(priority.lower(), 1.0)
    
    complexity_multiplier = 1.0 + (complexity_score / 20.0)
    
    predicted_days = avg_days * multiplier * complexity_multiplier
    
    return round(predicted_days, 1)

def generate_resolution_suggestions(category, keywords, similar_count=0):
    """Generate AI-powered resolution suggestions"""
    
    suggestions = []
    
    kb_templates = {
        'IT - Office 365 Email/Office Apps': [
            "Clear Microsoft Office cache and credentials",
            "Sign out completely and sign back in",
            "Verify Office 365 license status in admin portal",
            "Check MFA/authentication settings",
            "Test in Outlook Web Access to isolate client issues"
        ],
        'IT - Priority Request/Support': [
            "Verify request has proper approvals",
            "Confirm resource availability and lead time",
            "Set clear expectations on timeline",
            "Document any dependencies or blockers",
            "Schedule follow-up checkpoints"
        ],
        'IT - General IT Support': [
            "Gather detailed information (screenshots, error messages)",
            "Search for similar past tickets for resolution patterns",
            "Attempt basic troubleshooting steps",
            "Check knowledge base for documented solutions",
            "Escalate if issue persists after initial troubleshooting"
        ],
        'IT - Network/Infrastructure': [
            "Check network connectivity and DNS resolution",
            "Verify firewall rules and security groups",
            "Test from different network/location",
            "Review recent infrastructure changes",
            "Check service health dashboard"
        ]
    }
    
    if category in kb_templates:
        suggestions.extend(kb_templates[category])
    
    if 'access' in keywords or 'password' in keywords:
        suggestions.append("🔐 Reset user credentials and verify account is not locked")
    
    if 'performance' in keywords:
        suggestions.append("⚡ Check system resources, clear cache, and restart application")
    
    if 'error' in keywords:
        suggestions.append("🐛 Collect error logs and error codes for troubleshooting")
    
    if 'network' in keywords:
        suggestions.append("🌐 Test connectivity, check DNS, and verify VPN status")
    
    if similar_count > 3:
        suggestions.append(f"📊 {similar_count} similar tickets found - review for common solutions")
    
    return suggestions[:7]

def generate_auto_assignment_suggestion(category, group, current_workload):
    """Suggest best technician for assignment"""
    
    available_techs = []
    
    for tech, stats in analytics_cache['technician_stats'].items():
        if stats['active'] < 10:
            available_techs.append({
                'name': tech,
                'active_tickets': stats['active'],
                'total_experience': stats['total'],
                'avg_response': statistics.mean(stats['avg_response_hours']) if stats['avg_response_hours'] else 24
            })
    
    if not available_techs:
        return "All technicians at capacity - consider load balancing"
    
    available_techs.sort(key=lambda x: (x['active_tickets'], x['avg_response']))
    
    best_tech = available_techs[0]
    
    return f"Suggest: {best_tech['name']} (Active: {best_tech['active_tickets']}, Avg Response: {best_tech['avg_response']:.1f}h)"

def analyze_sentiment_risk(conversations):
    """Analyze conversation sentiment for satisfaction risk"""
    
    if not conversations:
        return 'unknown', 0
    
    negative_keywords = ['frustrated', 'angry', 'disappointed', 'unacceptable', 'terrible', 
                         'urgent', 'asap', 'immediately', 'escalate', 'complaint']
    positive_keywords = ['thank', 'appreciate', 'great', 'excellent', 'resolved', 'perfect', 'helpful']
    
    negative_count = 0
    positive_count = 0
    
    for conv in conversations:
        text = strip_html(conv.get('description', '')).lower()
        negative_count += sum(1 for kw in negative_keywords if kw in text)
        positive_count += sum(1 for kw in positive_keywords if kw in text)
    
    total_signals = negative_count + positive_count
    if total_signals == 0:
        return 'neutral', 0
    
    risk_score = (negative_count / total_signals) * 100
    
    if risk_score > 60:
        return 'high_risk', risk_score
    elif risk_score > 30:
        return 'moderate_risk', risk_score
    elif positive_count > negative_count:
        return 'positive', risk_score
    else:
        return 'neutral', risk_score

def update_analytics_cache(ticket_data, conversations, age_days, is_closed):
    """Update global analytics cache for pattern detection"""
    
    category = ticket_data.get('category', 'Unknown')
    technician = ticket_data.get('technician_name', 'Unassigned')
    
    analytics_cache['category_stats'][category]['count'] += 1
    if ticket_data.get('is_overdue'):
        analytics_cache['category_stats'][category]['overdue_count'] += 1
    if is_closed and age_days:
        analytics_cache['category_stats'][category]['avg_resolution_days'].append(age_days)
    
    if technician != 'Unassigned':
        analytics_cache['technician_stats'][technician]['total'] += 1
        if not is_closed:
            analytics_cache['technician_stats'][technician]['active'] += 1
        
        if conversations and len(conversations) > 0:
            first_conv = conversations[-1]
            first_response_ms = safe_int_conversion(first_conv.get('time_ms'))
            created_ms = ticket_data.get('created_time_ms')
            if first_response_ms and created_ms:
                response_hours = (first_response_ms - created_ms) / (1000 * 60 * 60)
                if response_hours > 0:
                    analytics_cache['technician_stats'][technician]['avg_response_hours'].append(response_hours)
    
    created_ms = ticket_data.get('created_time_ms')
    if created_ms:
        created_dt = datetime.fromtimestamp(created_ms / 1000)
        analytics_cache['hourly_distribution'][created_dt.hour] += 1
        analytics_cache['daily_distribution'][created_dt.strftime('%A')] += 1

def format_ticket_with_intelligence(ticket, details, conversations):
    """Format ticket with COMPLETE intelligence suite - All 4 Phases"""
    
    try:
        internal_id = safe_string(ticket.get('id'))
        display_id = get_display_id(details)
        subject = safe_string(details.get('subject', 'No Subject'))
        
        status = safe_get_name(details.get('status'))
        status_lower = status.lower()
        priority = safe_get_name(details.get('priority'))
        
        requester_obj = details.get('requester', {})
        requester_name = safe_get_name(requester_obj)
        requester_email = safe_string(requester_obj.get('email_id', '') if isinstance(requester_obj, dict) else '')
        
        technician_obj = details.get('technician', {})
        technician_name = safe_get_name(technician_obj, 'Unassigned')
        technician_email = safe_string(technician_obj.get('email_id', '') if isinstance(technician_obj, dict) else '')
        
        category = safe_get_name(details.get('category'), 'Uncategorized')
        subcategory = safe_get_name(details.get('subcategory'), 'None')
        
        group = safe_get_name(details.get('group'), 'Unassigned')
        site = safe_get_name(details.get('site'), 'No site')
        
        request_type = safe_get_name(details.get('request_type'), 'Incident')
        
        created_time_ms = safe_get_value(details.get('created_time'))
        due_by_time_ms = safe_get_value(details.get('due_by_time'))
        resolved_time_ms = safe_get_value(details.get('resolved_time'))
        last_updated_time_ms = safe_get_value(details.get('last_updated_time'))
        
        created_time_display = safe_get_display_value(details.get('created_time'))
        due_by_time_display = safe_get_display_value(details.get('due_by_time'), 'Not set')
        resolved_time_display = safe_get_display_value(details.get('resolved_time'), 'Not resolved')
        
        is_overdue = bool(details.get('is_overdue', False))
        is_escalated = bool(details.get('is_escalated', False))
        
        raw_description = details.get('description', '')
        clean_description = strip_html(raw_description)
        
        resolution_obj = details.get('resolution')
        resolution_text = ''
        if resolution_obj and isinstance(resolution_obj, dict):
            resolution_content = resolution_obj.get('content', '')
            if resolution_content:
                resolution_text = strip_html(resolution_content)
        
        closed_statuses = ['closed', 'resolved', 'cancelled']
        is_closed = status_lower in closed_statuses
        is_active = not is_closed
        
        now_ms = datetime.now().timestamp() * 1000
        age_days = None
        if created_time_ms:
            age_days = (now_ms - created_time_ms) / (1000 * 60 * 60 * 24)
        
        sla_status = 'unknown'
        hours_until_due = None
        if due_by_time_ms and is_active:
            hours_until_due = (due_by_time_ms - now_ms) / (1000 * 60 * 60)
            if hours_until_due < 0:
                sla_status = 'breached'
            elif hours_until_due < 4:
                sla_status = 'critical'
            elif hours_until_due < 24:
                sla_status = 'warning'
            else:
                sla_status = 'on-track'
        
        resolution_time_days = None
        if is_closed and created_time_ms and resolved_time_ms:
            resolution_time_days = (resolved_time_ms - created_time_ms) / (1000 * 60 * 60 * 24)
        
        conversations_structured = []
        conversations_text = ""
        first_response_time_hours = None
        days_since_last_activity = None
        latest_conversation_time_ms = None
        
        if conversations:
            conversations_sorted = sorted(
                conversations,
                key=lambda x: safe_int_conversion(x.get('time_ms'), 0),
                reverse=True
            )
            
            latest_conv_ms_raw = conversations_sorted[0].get('time_ms')
            latest_conversation_time_ms = safe_int_conversion(latest_conv_ms_raw)
            
            if latest_conversation_time_ms:
                days_since_last_activity = (now_ms - latest_conversation_time_ms) / (1000 * 60 * 60 * 24)
            
            if len(conversations_sorted) > 0 and created_time_ms:
                first_conv_ms_raw = conversations_sorted[-1].get('time_ms')
                first_conv_ms = safe_int_conversion(first_conv_ms_raw)
                
                if first_conv_ms:
                    first_response_time_hours = (first_conv_ms - created_time_ms) / (1000 * 60 * 60)
            
            conversations_text = "\n\n## 💬 Conversations & Replies\n\n"
            conversations_text += f"Total: {len(conversations_sorted)} (newest first)\n\n"
            
            for idx, conv in enumerate(conversations_sorted, 1):
                try:
                    sender = conv.get('sender', 'Unknown')
                    sender_email = conv.get('sender_email', '')
                    time_str = conv.get('time', 'Unknown time')
                    time_ms = safe_int_conversion(conv.get('time_ms'))
                    conv_type = conv.get('type', 'Unknown')
                    description = conv.get('description', '')
                    is_public = conv.get('is_public', True)
                    
                    clean_text = strip_html(description)
                    
                    if len(clean_text.strip()) < 10:
                        continue
                    
                    conversations_structured.append({
                        'sender': sender,
                        'sender_email': sender_email,
                        'time': time_str,
                        'time_ms': time_ms,
                        'type': conv_type,
                        'is_public': is_public,
                        'text': clean_text[:500]
                    })
                    
                    visibility = "Public" if is_public else "Private"
                    conversations_text += f"### Reply {idx} - {time_str}\n"
                    conversations_text += f"**From:** {sender}"
                    if sender_email:
                        conversations_text += f" ({sender_email})"
                    conversations_text += f"\n**Type:** {conv_type} ({visibility})\n\n"
                    conversations_text += f"{clean_text}\n\n"
                    conversations_text += "---\n\n"
                    
                except Exception as e:
                    continue
        
        conversation_count = len(conversations_structured)
        
        keywords = extract_keywords(f"{subject} {clean_description}")
        
        complexity_score = calculate_complexity_score(
            {'is_escalated': is_escalated, 'is_overdue': is_overdue, 'priority': priority},
            conversations_structured,
            age_days or 0
        )
        
        sentiment, sentiment_score = analyze_sentiment_risk(conversations)
        
        predicted_resolution_days = predict_resolution_time(category, priority, complexity_score)
        
        similar_ticket_count = analytics_cache['category_stats'][category]['count']
        
        resolution_suggestions = generate_resolution_suggestions(category, keywords, similar_ticket_count)
        
        assignment_suggestion = generate_auto_assignment_suggestion(category, group, analytics_cache['technician_stats'])
        
        update_analytics_cache(
            {
                'category': category,
                'technician_name': technician_name,
                'is_overdue': is_overdue,
                'created_time_ms': created_time_ms
            },
            conversations_structured,
            age_days,
            is_closed
        )
        
        created_dt = None
        if created_time_ms:
            created_dt = datetime.fromtimestamp(created_time_ms / 1000)
        
        structured_data = {
            "ticket_id": display_id,
            "internal_id": internal_id,
            "subject": subject,
            "status": status,
            "status_lower": status_lower,
            "is_closed": is_closed,
            "is_open": is_active,
            "is_active": is_active,
            "priority": priority,
            "request_type": request_type,
            "requester_name": requester_name,
            "requester_email": requester_email,
            "technician_name": technician_name,
            "technician_email": technician_email,
            "group": group,
            "site": site,
            "category": category,
            "subcategory": subcategory,
            "category_path": f"{category}/{subcategory}",
            "created_time": created_time_display,
            "due_by_time": due_by_time_display,
            "resolved_time": resolved_time_display,
            "is_overdue": is_overdue,
            "is_escalated": is_escalated,
            "description": clean_description[:1000],
            "resolution": resolution_text[:1000] if resolution_text else "",
            "conversations": conversations_structured,
            "conversation_count": conversation_count,
            "age_days": round(age_days, 2) if age_days else None,
            "sla_status": sla_status,
            "hours_until_due": round(hours_until_due, 2) if hours_until_due else None,
            "resolution_time_days": round(resolution_time_days, 2) if resolution_time_days else None,
            "first_response_time_hours": round(first_response_time_hours, 2) if first_response_time_hours else None,
            "days_since_last_activity": round(days_since_last_activity, 2) if days_since_last_activity else None,
            "keywords": keywords,
            "complexity_score": complexity_score,
            "complexity_level": "high" if complexity_score >= 7 else "medium" if complexity_score >= 4 else "low",
            "sentiment": sentiment,
            "sentiment_score": round(sentiment_score, 1),
            "satisfaction_risk": sentiment == 'high_risk',
            "predicted_resolution_days": predicted_resolution_days,
            "similar_ticket_count": similar_ticket_count,
            "created_hour": created_dt.hour if created_dt else None,
            "created_day_of_week": created_dt.strftime('%A') if created_dt else None,
            "created_month": created_dt.strftime('%B') if created_dt else None,
            "is_business_hours": (created_dt.hour >= 9 and created_dt.hour <= 17) if created_dt else None,
            "is_weekend": (created_dt.strftime('%A') in ['Saturday', 'Sunday']) if created_dt else None,
            "needs_immediate_attention": (is_overdue and is_active and priority.lower() in ['urgent', 'high']),
            "needs_followup": (is_active and days_since_last_activity and days_since_last_activity > 3),
            "auto_assignment_suggestion": assignment_suggestion if technician_name == 'Unassigned' else None
        }
        
        if created_time_ms:
            structured_data["created_time_ms"] = created_time_ms
        if due_by_time_ms:
            structured_data["due_by_time_ms"] = due_by_time_ms
        if resolved_time_ms:
            structured_data["resolved_time_ms"] = resolved_time_ms
        if last_updated_time_ms:
            structured_data["last_updated_time_ms"] = last_updated_time_ms
        if latest_conversation_time_ms:
            structured_data["latest_conversation_time_ms"] = latest_conversation_time_ms
        
        dust_tags = [
            f"status:{status_lower.replace(' ', '-')}",
            f"priority:{priority.lower().replace(' ', '-')}",
            f"ticket-id:{display_id}",
            f"category:{category.lower().replace(' ', '-').replace('/', '-')}",
            f"complexity:{structured_data['complexity_level']}",
        ]
        
        if group and group != 'Unassigned':
            dust_tags.append(f"group:{group.lower().replace(' ', '-')}")
        if technician_name and technician_name != 'Unassigned':
            dust_tags.append(f"technician:{technician_name.lower().replace(' ', '-')}")
        
        if is_active:
            dust_tags.extend(['not-closed', 'active', 'open-status', 'needs-attention'])
        else:
            dust_tags.append('closed-status')
        
        if age_days:
            if age_days < 1:
                dust_tags.append('age:new')
            elif age_days < 7:
                dust_tags.append('age:this-week')
            elif age_days < 30:
                dust_tags.append('age:this-month')
            elif age_days < 90:
                dust_tags.append('age:aging')
            else:
                dust_tags.append('age:stale')
        
        if sla_status != 'unknown':
            dust_tags.append(f"sla:{sla_status}")
        
        if first_response_time_hours:
            if first_response_time_hours < 1:
                dust_tags.append('response:immediate')
            elif first_response_time_hours < 24:
                dust_tags.append('response:same-day')
            else:
                dust_tags.append('response:delayed')
        
        if days_since_last_activity:
            if days_since_last_activity > 14 and is_active:
                dust_tags.append('inactive:2-weeks')
            elif days_since_last_activity > 7 and is_active:
                dust_tags.append('inactive:1-week')
        
        for keyword in keywords:
            dust_tags.append(f"issue-type:{keyword}")
        
        if sentiment == 'high_risk':
            dust_tags.append('satisfaction:at-risk')
        elif sentiment == 'positive':
            dust_tags.append('satisfaction:positive')
        
        if created_dt:
            if created_dt.hour >= 9 and created_dt.hour <= 17:
                dust_tags.append('created:business-hours')
            else:
                dust_tags.append('created:after-hours')
            
            if created_dt.strftime('%A') in ['Saturday', 'Sunday']:
                dust_tags.append('created:weekend')
        
        if is_overdue:
            dust_tags.append('overdue')
        if is_escalated:
            dust_tags.append('escalated')
        if conversation_count > 0:
            dust_tags.append('has-conversations')
        if conversation_count > 5:
            dust_tags.append('high-conversation-volume')
        if technician_name == 'Unassigned':
            dust_tags.append('unassigned')
        if is_overdue and is_active:
            dust_tags.append('urgent-attention')
        if structured_data['needs_immediate_attention']:
            dust_tags.append('critical-action-required')
        
        status_indicator = "🔴 ACTIVE TICKET" if is_active else "✅ CLOSED"
        status_emoji = "🔴" if is_active else "✅"
        complexity_emoji = "🔥" if complexity_score >= 7 else "⚠️" if complexity_score >= 4 else "📝"
        
        text_content = f"""{status_indicator} - Ticket #{display_id}: {subject}

## 🎯 STATUS INFORMATION
CURRENT_STATUS: {status.upper()}
STATUS_VALUE: {status}
STATUS_CATEGORY: {'ACTIVE' if is_active else 'CLOSED'}
IS_ACTIVE: {'YES' if is_active else 'NO'}
IS_CLOSED: {'YES' if is_closed else 'NO'}
IS_OPEN: {'YES' if is_active else 'NO'}
TICKET_STATE: {'NOT_CLOSED' if is_active else 'CLOSED'}

---

{status_emoji} **Ticket ID:** {display_id}
📊 **Status:** {status} | **Priority:** {priority}
{complexity_emoji} **Complexity:** {structured_data['complexity_level'].upper()} ({complexity_score}/10)
📋 **Type:** {request_type}
"""

        if age_days:
            text_content += f"📅 **Age:** {age_days:.1f} days\n"
        
        if sla_status != 'unknown':
            sla_emoji = "🔥" if sla_status == 'breached' else "⚠️" if sla_status == 'critical' else "✅"
            text_content += f"{sla_emoji} **SLA Status:** {sla_status.upper()}\n"
            if hours_until_due:
                text_content += f"⏰ **Time Until Due:** {abs(hours_until_due):.1f} hours {'(OVERDUE)' if hours_until_due < 0 else ''}\n"
        
        text_content += f"""
## 👥 Assignment
- **Requester:** {requester_name}
  - Email: {requester_email}
- **Technician:** {technician_name}
  - Email: {technician_email}
- **Group:** {group}
- **Site:** {site}
"""

        if technician_name == 'Unassigned' and assignment_suggestion:
            text_content += f"\n💡 **Assignment Recommendation:** {assignment_suggestion}\n"

        text_content += f"""
## 📁 Classification
- **Category:** {category}
- **Subcategory:** {subcategory}
- **Keywords:** {', '.join(keywords) if keywords else 'None detected'}
"""

        text_content += f"""
## 📅 Timeline
- **Created:** {created_time_display}
- **Due By:** {due_by_time_display}
"""

        if resolved_time_display != 'Not resolved':
            text_content += f"- **Resolved:** {resolved_time_display}\n"
        
        if resolution_time_days:
            text_content += f"- **Resolution Time:** {resolution_time_days:.1f} days\n"
        
        if first_response_time_hours:
            text_content += f"- **First Response:** {first_response_time_hours:.1f} hours\n"
        
        if days_since_last_activity:
            text_content += f"- **Last Activity:** {days_since_last_activity:.1f} days ago\n"

        text_content += f"""
- **Overdue:** {'⚠️ YES' if is_overdue else 'No'}
- **Escalated:** {'🔥 YES' if is_escalated else 'No'}

## 📝 Description
{clean_description}
"""

        text_content += "\n## 🤖 AI-Powered Insights & Recommendations\n\n"
        
        if is_active:
            text_content += f"### 📊 Predictive Analytics\n"
            text_content += f"- **Predicted Resolution Time:** {predicted_resolution_days} days\n"
            text_content += f"- **Similar Tickets in Category:** {similar_ticket_count}\n"
            text_content += f"- **Complexity Assessment:** {structured_data['complexity_level'].upper()} complexity\n"
        
        if sentiment != 'unknown':
            sentiment_emoji = "🚨" if sentiment == 'high_risk' else "⚠️" if sentiment == 'moderate_risk' else "😊" if sentiment == 'positive' else "😐"
            text_content += f"- **Customer Sentiment:** {sentiment_emoji} {sentiment.replace('_', ' ').title()} ({sentiment_score:.0f}% risk)\n"
        
        text_content += "\n"
        
        alerts = []
        
        if structured_data['needs_immediate_attention']:
            alerts.append("🔥 **CRITICAL:** This ticket requires IMMEDIATE attention (overdue + high priority)")
        
        if is_active and conversation_count == 0 and age_days and age_days > 2:
            alerts.append("💬 **NO RESPONSES YET:** Ticket created {:.0f} days ago with no conversation - reach out to requester".format(age_days))
        
        if is_active and days_since_last_activity and days_since_last_activity > 7:
            alerts.append("⏰ **STALE TICKET:** No activity in {:.0f} days - needs follow-up".format(days_since_last_activity))
        
        if conversation_count > 10:
            alerts.append("🔄 **HIGH CONVERSATION VOLUME:** Multiple exchanges detected - consider scheduling a call/meeting")
        
        if sentiment == 'high_risk':
            alerts.append("🚨 **SATISFACTION RISK:** Negative sentiment detected - prioritize customer communication")
        
        if complexity_score >= 7:
            alerts.append("🧩 **COMPLEX ISSUE:** High complexity score - may require senior technician or escalation")
        
        if technician_name == 'Unassigned':
            alerts.append("👤 **UNASSIGNED:** No technician assigned - review priority and assign appropriate team member")
        
        if alerts:
            text_content += "### ⚠️ Action Required\n"
            for alert in alerts:
                text_content += f"{alert}\n\n"
        
        if resolution_suggestions and is_active:
            text_content += "### 💡 Suggested Resolution Steps\n"
            for idx, suggestion in enumerate(resolution_suggestions, 1):
                text_content += f"{idx}. {suggestion}\n"
            text_content += "\n"
        
        cat_stats = analytics_cache['category_stats'].get(category, {})
        if cat_stats.get('count', 0) > 5:
            text_content += f"### 📈 Category Insights\n"
            text_content += f"- **Total tickets in {category}:** {cat_stats['count']}\n"
            if cat_stats.get('avg_resolution_days'):
                avg_res = statistics.mean(cat_stats['avg_resolution_days'])
                text_content += f"- **Average resolution time:** {avg_res:.1f} days\n"
            if cat_stats.get('overdue_count'):
                overdue_pct = (cat_stats['overdue_count'] / cat_stats['count']) * 100
                text_content += f"- **Overdue rate:** {overdue_pct:.0f}%\n"
            text_content += "\n"
        
        if resolution_text:
            text_content += f"\n## ✅ Resolution\n{resolution_text}\n"
        
        text_content += conversations_text
        
        text_content += f"""

---
## 🔍 Quick Reference
- Ticket: #{display_id}
- Current Status: {status}
- Active: {'Yes' if is_active else 'No'}
- Closed: {'Yes' if is_closed else 'No'}
- Priority: {priority}
- Complexity: {structured_data['complexity_level']}
- Assigned To: {technician_name}
- Category: {category}
- Age: {age_days:.1f} days if age_days else 'N/A'
- Conversations: {conversation_count}
- Keywords: {', '.join(keywords) if keywords else 'None'}
- Sentiment: {sentiment}
- Predicted Resolution: {predicted_resolution_days} days
"""
        
        timestamp = last_updated_time_ms if last_updated_time_ms else int(time.time() * 1000)
        
        return {
            "document_id": f"ticket-{internal_id}",
            "timestamp": timestamp,
            "tags": dust_tags,
            "source_url": f"https://{SDP_DOMAIN}/app/itdesk/ui/requests/{internal_id}",
            "text": text_content,
            "structured_data": structured_data
        }
        
    except Exception as e:
        print(f"      Format error: {str(e)[:100]}")
        return None

def upload_to_dust(document):
    if not document:
        return False
    
    headers = {
        'Authorization': f'Bearer {DUST_API_KEY}',
        'Content-Type': 'application/json'
    }
    url = f"{DUST_API_BASE}/{document['document_id']}"
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(url, headers=headers, json=document, timeout=30)
            if response.status_code in [200, 201]:
                return True
            
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
                continue
            return False
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
                continue
            return False
    
    return False

def save_analytics_report():
    """Save analytics cache to JSON for pattern analysis"""
    
    try:
        report = {
            'generated_at': datetime.now().isoformat(),
            'category_statistics': dict(analytics_cache['category_stats']),
            'technician_statistics': dict(analytics_cache['technician_stats']),
            'hourly_distribution': dict(analytics_cache['hourly_distribution']),
            'daily_distribution': dict(analytics_cache['daily_distribution']),
            'top_issue_keywords': dict(analytics_cache['issue_keywords'].most_common(20))
        }
        
        filename = f"analytics_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        print(f"\n📊 Analytics report saved: {filename}")
        
    except Exception as e:
        print(f"Could not save analytics report: {e}")

def print_analytics_summary():
    """Print analytics summary to console"""
    
    print("\n" + "="*60)
    print("📊 ANALYTICS SUMMARY")
    print("="*60)
    
    print("\n🏆 Top Categories:")
    top_cats = sorted(analytics_cache['category_stats'].items(), 
                     key=lambda x: x[1]['count'], reverse=True)[:5]
    for cat, stats in top_cats:
        print(f"  {cat}: {stats['count']} tickets")
        if stats['avg_resolution_days']:
            avg = statistics.mean(stats['avg_resolution_days'])
            print(f"    Avg resolution: {avg:.1f} days")
    
    print("\n👥 Technician Workload:")
    for tech, stats in sorted(analytics_cache['technician_stats'].items(), 
                             key=lambda x: x[1]['active'], reverse=True)[:5]:
        print(f"  {tech}: {stats['active']} active / {stats['total']} total")
        if stats['avg_response_hours']:
            avg = statistics.mean(stats['avg_response_hours'])
            print(f"    Avg response: {avg:.1f} hours")
    
    print("\n⏰ Peak Hours:")
    top_hours = sorted(analytics_cache['hourly_distribution'].items(), 
                      key=lambda x: x[1], reverse=True)[:5]
    for hour, count in top_hours:
        print(f"  {hour:02d}:00 - {count} tickets")
    
    print("\n🔍 Top Issue Types:")
    for keyword, count in analytics_cache['issue_keywords'].most_common(5):
        print(f"  {keyword}: {count} tickets")
    
    print("="*60 + "\n")

def main():
    print("=" * 60)
    print(f"🚀 SERVICEDESK INTELLIGENCE SUITE v2.0 - Starting at {datetime.now()}")
    print("=" * 60)
    print("✨ INCREMENTAL SYNC - Near real-time ticket intelligence")
    print("   • Advanced tagging & search")
    print("   • Predictive analytics")
    print("   • Pattern detection")
    print("   • Resolution recommendations")
    print("   • Fast incremental updates")
    print()
    
    required_vars = ['SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 
                     'SDP_REFRESH_TOKEN', 'DUST_API_KEY', 'DUST_WORKSPACE_ID']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"ERROR: Missing: {', '.join(missing_vars)}")
        return
    
    tickets = fetch_all_tickets()
    print(f"\nTotal tickets to process: {len(tickets)}\n")
    
    if not tickets:
        print("No tickets to process - system is up to date!")
        return
    
    access_token = get_access_token()
    success_count = 0
    error_count = 0
    conv_count = 0
    active_count = 0
    closed_count = 0
    high_complexity_count = 0
    at_risk_count = 0
    
    for idx, ticket in enumerate(tickets, 1):
        ticket_id = ticket.get('id')
        
        if idx % 100 == 0 or idx == 1:
            print(f"[{idx}/{len(tickets)}] Processing...")
        
        try:
            details = fetch_ticket_details(ticket_id, access_token)
            if not details:
                error_count += 1
                continue
            
            status = safe_get_name(details.get('status'))
            if status.lower() in ['closed', 'resolved', 'cancelled']:
                closed_count += 1
            else:
                active_count += 1
            
            conversations = fetch_ticket_conversations(ticket_id, access_token)
            
            if conversations:
                conv_count += 1
            
            document = format_ticket_with_intelligence(ticket, details, conversations)
            
            if document:
                structured = document.get('structured_data', {})
                if structured.get('complexity_score', 0) >= 7:
                    high_complexity_count += 1
                if structured.get('satisfaction_risk'):
                    at_risk_count += 1
                
                if upload_to_dust(document):
                    success_count += 1
                    if idx % 100 == 0:
                        print(f"  ✓ {success_count} updated | Active: {active_count} | Complex: {high_complexity_count} | At-Risk: {at_risk_count}")
                else:
                    error_count += 1
            else:
                error_count += 1
            
        except Exception as e:
            error_count += 1
            continue
        
        time.sleep(0.3)
        
        if idx % 250 == 0:
            access_token = get_access_token(force_refresh=True)
            print(f"  ℹ️  Checkpoint: {success_count}/{idx}\n")
    
    print(f"\n{'='*60}")
    print(f"✅ INTELLIGENCE SYNC COMPLETED")
    print(f"  Total Processed: {success_count}/{len(tickets)}")
    print(f"  Active Tickets: {active_count}")
    print(f"  Closed Tickets: {closed_count}")
    print(f"  With Conversations: {conv_count}")
    print(f"  High Complexity: {high_complexity_count}")
    print(f"  Satisfaction At-Risk: {at_risk_count}")
    print(f"  Errors: {error_count}")
    print(f"  Finished: {datetime.now()}")
    print(f"{'='*60}")
    
    print_analytics_summary()
    save_analytics_report()
    
    # Save current timestamp for next incremental sync
    current_time_ms = int(datetime.now().timestamp() * 1000)
    print(f"\n📅 Current sync timestamp: {current_time_ms}")
    print(f"   (Will be used for next incremental sync)")
    
    # Save to file for GitHub Actions to read
    with open('last_sync_time.txt', 'w') as f:
        f.write(str(current_time_ms))
    
    print("\n🎯 Enhanced Features Active:")
    print("  ✓ Phase 1: Status tagging, SLA tracking, aging metrics")
    print("  ✓ Phase 2: AI insights, complexity scoring, resolution suggestions")
    print("  ✓ Phase 3: Pattern detection, workload analysis, auto-assignment")
    print("  ✓ Phase 4: Predictive analytics, sentiment analysis, trend forecasting")
    print("  ✓ INCREMENTAL: Near real-time updates (only changed tickets)")
    print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
