#!/usr/bin/env python3
"""
Check a few random tickets in Dust to see their tags
"""

import os
import requests

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = 'servicedesk-tickets'

DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}"

def check_random_tickets():
    headers = {'Authorization': f'Bearer {DUST_API_KEY}'}
    
    # List some documents
    url = f"{DUST_API_BASE}/documents"
    params = {'limit': 20, 'offset': 0}
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        data = response.json()
        documents = data.get('documents', [])
        
        print("="*60)
        print("CHECKING TAGS IN DUST DOCUMENTS")
        print("="*60)
        
        status_tags = {}
        
        for doc in documents:
            doc_id = doc.get('document_id', 'Unknown')
            tags = doc.get('tags', [])
            
            # Find status tag
            status_tag = [t for t in tags if t.startswith('status:')]
            
            if status_tag:
                status = status_tag[0]
                status_tags[status] = status_tags.get(status, 0) + 1
                
                # Print first few examples
                if status_tags[status] <= 2:
                    print(f"\nDoc: {doc_id}")
                    print(f"  All tags: {tags}")
                    print(f"  Status tag: {status}")
        
        print("\n" + "="*60)
        print("STATUS TAG DISTRIBUTION (sample):")
        print("="*60)
        for tag, count in sorted(status_tags.items()):
            print(f"{tag}: {count} tickets")
    else:
        print(f"Failed: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    check_random_tickets()
