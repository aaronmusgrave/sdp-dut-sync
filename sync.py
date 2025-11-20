#!/usr/bin/env python3
"""
Test what error Dust returns for structured data
"""

import os
import requests
import json
import time

DUST_API_KEY = os.environ.get('DUST_API_KEY')
DUST_WORKSPACE_ID = os.environ.get('DUST_WORKSPACE_ID')
DUST_FOLDER_NAME = os.environ.get('DUST_FOLDER_NAME', 'servicedesk')

DUST_API_BASE = f"https://dust.tt/api/v1/w/{DUST_WORKSPACE_ID}/data_sources/{DUST_FOLDER_NAME}/documents"

# Test with structured_data
test_doc = {
    "document_id": "test-structured",
    "timestamp": int(time.time() * 1000),
    "tags": ["test"],
    "source_url": "https://test.com",
    "text": "Test document",
    "structured_data": {
        "ticket_id": "12345",
        "status": "Open",
        "priority": "High"
    }
}

headers = {
    'Authorization': f'Bearer {DUST_API_KEY}',
    'Content-Type': 'application/json'
}

url = f"{DUST_API_BASE}/test-structured"

print("Testing upload with structured_data field...")
print(f"URL: {url}")
print(f"\nDocument:")
print(json.dumps(test_doc, indent=2))

response = requests.post(url, headers=headers, json=test_doc, timeout=30)

print(f"\nResponse Status: {response.status_code}")
print(f"Response Body:")
print(response.text)

if response.status_code in [200, 201]:
    print("\n✅ SUCCESS - structured_data is supported!")
else:
    print(f"\n❌ FAILED - Error: {response.status_code}")
    print("\nThis tells us the exact error Dust is returning")
