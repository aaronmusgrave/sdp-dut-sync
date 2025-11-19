#!/usr/bin/env python3
"""
Find tickets with conversations and check individual notifications for text
"""

import os
import requests
import json

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

def get_access_token():
    payload = {
        'refresh_token': SDP_REFRESH_TOKEN,
        'client_id': SDP_CLIENT_ID,
        'client_secret': SDP_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    response = requests.post(ZOHO_TOKEN_URL, data=payload)
    return response.json()['access_token']

def find_tickets_with_conversations():
    """Find tickets with conversations/notifications and check for text"""
    access_token = get_access_token()
    headers = {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Accept': 'application/vnd.manageengine.sdp.v3+json'
    }
    
    # Get recent tickets
    list_info = {"list_info": {"row_count": 50, "start_index": 1, "sort_field": "created_time", "sort_order": "desc"}}
    params = {'input_data': json.dumps(list_info)}
    url = f"{SDP_API_BASE}/requests"
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        tickets = response.json().get('requests', [])
        
        print("Checking recent tickets for conversations/notifications...\n")
        
        for ticket in tickets[:30]:
            ticket_id = ticket.get('id')
            
            # Check conversations
            conv_url = f"{SDP_API_BASE}/requests/{ticket_id}/conversations"
            conv_response = requests.get(conv_url, headers={'Authorization': f'Zoho-oauthtoken {access_token}'})
            
            if conv_response.status_code == 200:
                conv_data = conv_response.json()
                conversations = conv_data.get('conversations', [])
                
                if conversations and len(conversations) > 3:  # Find one with multiple conversations
                    print(f"{'='*60}")
                    print(f"✓ Found ticket with {len(conversations)} conversations!")
                    print(f"Ticket ID: {ticket_id}")
                    print(f"{'='*60}\n")
                    
                    # Check notifications endpoint
                    notif_url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications"
                    notif_response = requests.get(notif_url, headers={'Authorization': f'Zoho-oauthtoken {access_token}'})
                    
                    if notif_response.status_code == 200:
                        notif_data = notif_response.json()
                        notifications = notif_data.get('notifications', [])
                        
                        print(f"Found {len(notifications)} notifications\n")
                        
                        # Try to get details for first few notifications
                        for idx, notif in enumerate(notifications[:3], 1):
                            notif_id = notif.get('id')
                            notif_type = notif.get('type', 'Unknown')
                            
                            print(f"{'='*60}")
                            print(f"Notification {idx}")
                            print(f"Type: {notif_type}")
                            print(f"ID: {notif_id}")
                            print(f"{'='*60}\n")
                            
                            # List view
                            print("List view fields:")
                            print(json.dumps(notif, indent=2)[:800])
                            print("\n")
                            
                            # Get detailed view
                            detail_url = f"{SDP_API_BASE}/requests/{ticket_id}/notifications/{notif_id}"
                            detail_response = requests.get(detail_url, headers={'Authorization': f'Zoho-oauthtoken {access_token}'})
                            
                            if detail_response.status_code == 200:
                                detail_data = detail_response.json()
                                notification_detail = detail_data.get('notification', {})
                                
                                print("Detailed view - All fields:")
                                print(f"Keys: {list(notification_detail.keys())}\n")
                                
                                # Print full detail (truncated)
                                print("Full detailed notification:")
                                print(json.dumps(notification_detail, indent=2)[:2000])
                                print("\n")
                                
                                # Check for text fields
                                text_fields = ['description', 'text', 'content', 'body', 'message', 
                                             'note_text', 'comment', 'reply', 'html', 'plain_text',
                                             'message_body', 'email_content', 'notification_text']
                                
                                print("Checking for text content:")
                                found_text = False
                                for field in text_fields:
                                    if field in notification_detail:
                                        value = notification_detail[field]
                                        if value and isinstance(value, str) and len(value) > 10:
                                            print(f"✓✓✓ Found '{field}':")
                                            print(f"{value[:500]}\n")
                                            found_text = True
                                
                                if not found_text:
                                    print("✗ No text content found in detailed notification\n")
                            else:
                                print(f"Failed to get notification details: {detail_response.status_code}")
                                print(f"{detail_response.text[:500]}\n")
                        
                        return  # Stop after first ticket with conversations
        
        print("\n✗ No tickets with multiple conversations found")

if __name__ == "__main__":
    find_tickets_with_conversations()
