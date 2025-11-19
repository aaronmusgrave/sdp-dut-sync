#!/usr/bin/env python3
"""
Test if individual notes contain conversation text
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

def find_ticket_with_notes():
    """Find a ticket that has notes"""
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
        
        print("Checking recent tickets for notes...\n")
        
        for ticket in tickets[:20]:
            ticket_id = ticket.get('id')
            
            # Check if ticket has notes
            notes_url = f"{SDP_API_BASE}/requests/{ticket_id}/notes"
            notes_response = requests.get(notes_url, headers={'Authorization': f'Zoho-oauthtoken {access_token}'})
            
            if notes_response.status_code == 200:
                notes_data = notes_response.json()
                notes = notes_data.get('notes', [])
                
                if notes:
                    print(f"{'='*60}")
                    print(f"✓ Found ticket with {len(notes)} notes!")
                    print(f"Ticket ID: {ticket_id}")
                    print(f"{'='*60}\n")
                    
                    # Get details of first note
                    first_note = notes[0]
                    note_id = first_note.get('id')
                    
                    print("List view of first note:")
                    print(json.dumps(first_note, indent=2)[:500])
                    print("\n")
                    
                    # Get detailed view of note
                    if note_id:
                        detail_url = f"{SDP_API_BASE}/requests/{ticket_id}/notes/{note_id}"
                        detail_response = requests.get(detail_url, headers={'Authorization': f'Zoho-oauthtoken {access_token}'})
                        
                        if detail_response.status_code == 200:
                            detail_data = detail_response.json()
                            note_detail = detail_data.get('note', {})
                            
                            print("="*60)
                            print("DETAILED NOTE VIEW:")
                            print("="*60)
                            print(json.dumps(note_detail, indent=2))
                            print("\n")
                            
                            # Check for text fields
                            text_fields = ['description', 'text', 'content', 'body', 'message', 
                                         'note_text', 'comment', 'reply', 'html', 'plain_text']
                            
                            print("Checking for text content:")
                            found_text = False
                            for field in text_fields:
                                if field in note_detail:
                                    value = note_detail[field]
                                    if value:
                                        print(f"✓✓✓ Found '{field}':")
                                        print(f"{str(value)[:500]}")
                                        found_text = True
                            
                            if not found_text:
                                print("✗ No text content found in note")
                            
                            return  # Stop after finding first ticket with notes
                        else:
                            print(f"Failed to get note details: {detail_response.status_code}")
                            print(detail_response.text[:500])
        
        print("\n✗ No tickets with notes found in recent 20 tickets")

if __name__ == "__main__":
    find_ticket_with_notes()
