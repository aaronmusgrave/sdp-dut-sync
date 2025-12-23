#!/usr/bin/env python3
"""
ServiceDesk Intelligence Suite v3.2.2-PRODUCTION-FIXED
Dual Data Source + Token Retry Logic + Rate Limit Handling + Secret Cleaning

Changes in v3.2.2-FIXED:
- Re-added secret cleaning (strip quotes, backslashes, whitespace)
- All other v3.2.2 features intact

Changes in v3.2.2:
- Added 401 error retry logic with automatic token refresh
- Proactive token refresh (when < 10 minutes remaining)
- Rate limit handling (delays between conversation fetches)
- Enhanced error tracking and recovery
- Improved logging for troubleshooting
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
    """
    Remove quotes, backslashes, and whitespace from secrets.
    Handles common secret format issues from GitHub Actions.
    """
    if not value:
        return ''
    # Strip whitespace, then quotes, then backslashes, then whitespace again
    cleaned = value.strip()
    cleaned = cleaned.strip('"').strip("'")
    cleaned = cleaned.replace('\\', '')
    cleaned = cleaned.strip()
    return cleaned

# Load and clean all secrets
DUST_API_KEY = clean_secret(os.getenv('DUST_API_KEY', ''))
DUST_WORKSPACE_ID = clean_secret(os.getenv('DUST_WORKSPACE_ID', ''))
DUST_MAIN_DATA_SOURCE = 'servicedesk'          # All tickets
DUST_ACTIVE_DATA_SOURCE = 'servicedesk-active'  # Active only

SDP_DOMAIN = clean_secret(os.getenv('SDP_DOMAIN', ''))
SDP_CLIENT_ID = clean_secret(os.getenv('SDP_CLIENT_ID', ''))
SDP_CLIENT_SECRET = clean_secret(os.getenv('SDP_CLIENT_SECRET', ''))
SDP_REFRESH_TOKEN = clean_secret(os.getenv('SDP_REFRESH_TOKEN', ''))

CHECKPOINT_FILE = 'sync_checkpoint.json'
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 3.0
CONVERSATION_FETCH_DELAY = 0.3  # Delay every 10 conversations

# Version
VERSION = "v3.2.2-PRODUCTION-FIXED"

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ... rest of the script remains the same ...
