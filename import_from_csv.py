#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timedelta

SDP_DOMAIN = os.environ.get('SDP_DOMAIN')
SDP_CLIENT_ID = os.environ.get('SDP_CLIENT_ID')
SDP_CLIENT_SECRET = os.environ.get('SDP_CLIENT_SECRET')
SDP_REFRESH_TOKEN = os.environ.get('SDP_REFRESH_TOKEN')

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SDP_API_BASE = f"https://{SDP_DOMAIN}/api/v3"

print("Getting token...", flush=True)
response = requests.post(ZOHO_TOKEN_URL, data={
    'refresh_token': SDP_REFRESH_TOKEN,
    'client_id': SDP_CLIENT_ID,
    'client_secret': SDP_CLIENT_SECRET,
    'grant_type': 'refresh_token'
})

access_token = response.json()['access_token']
print(f"✅ Token obtained\n", flush=True)

# Test ticket 15667 that you see in ServiceDesk Plus
ticket_id = 15667

headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
url = f"{SDP_API_BASE}/requests/{ticket_id}"

print(f"Testing ticket #{ticket_id}...", flush=True)
print(f"URL: {url}", flush=True)

response = requests.get(url, headers=headers, timeout=10)

print(f"Status code: {response.status_code}", flush=True)
print(f"Response: {response.text[:500]}", flush=True)

if response.status_code == 200:
    ticket = response.json().get('request', {})
    print(f"\n✅ SUCCESS! Ticket found!", flush=True)
    print(f"Subject: {ticket.get('subject')}", flush=True)
    print(f"Display ID: {ticket.get('display_id')}", flush=True)
else:
    print(f"\n❌ Ticket not found via API", flush=True)
    print(f"But you can see it in ServiceDesk Plus!", flush=True)
    print(f"\nThis means the API has restricted access to this ticket.", flush=True)
