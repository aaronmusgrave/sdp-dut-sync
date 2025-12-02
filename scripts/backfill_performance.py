#!/usr/bin/env python3
"""
Backfill performance data - WITH REAL-TIME PROGRESS
Shows each date as it completes
"""

import subprocess
import time
import json
import os
import sys
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed

# Configuration
DAYS_TO_BACKFILL = 60  # Adjust as needed: 7, 14, 30, 60, 90
SCRIPT_PATH = 'scripts/track_meeting_load.py'
PARALLEL_WORKERS = 4
TIMEOUT_PER_DAY = 600
PROGRESS_FILE = 'backfill_progress.json'

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'completed_dates': [], 'failed_dates': [], 'last_processed': None, 'in_progress': []}

def save_progress(progress):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def format_duration(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

def process_single_date(date_str):
    """Process a single date with environment passed"""
    # Print start message (with flush for real-time output)
    print(f"🔄 Processing {date_str}...", flush=True)
    
    start_time = time.time()
    try:
        env = os.environ.copy()
        
        result = subprocess.run(
            ['python', SCRIPT_PATH, '--date', date_str],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_PER_DAY,
            env=env,
            cwd=os.getcwd()
        )
        
        duration = time.time() - start_time
        success = result.returncode == 0
        
        filename = f"tech_performance_{date_str.replace('-', '')}.json"
        file_size = 0
        if os.path.exists(filename):
            file_size = os.path.getsize(filename) / 1024
        
        return {
            'date': date_str,
            'success': success,
            'returncode': result.returncode,
            'stderr': result.stderr[-300:] if result.stderr else '',
            'filename': filename if success else None,
            'file_size': file_size if success else 0,
            'duration': duration
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return {
            'date': date_str,
            'success': False,
            'returncode': -1,
            'stderr': 'Timeout after 10 minutes',
            'filename': None,
            'file_size': 0,
            'duration': duration
        }
    except Exception as e:
        duration = time.time() - start_time
        return {
            'date': date_str,
            'success': False,
            'returncode': -2,
            'stderr': str(e)[:200],
            'filename': None,
            'file_size': 0,
            'duration': duration
        }

def main():
    # Disable output buffering
    sys.stdout.reconfigure(line_buffering=True)
    
    print("="*70, flush=True)
    print(f"🚀 PERFORMANCE DATA BACKFILL - LAST {DAYS_TO_BACKFILL} DAYS", flush=True)
    print(f"   Environment: ✓ | Workers: {PARALLEL_WORKERS} | Timeout: 10min/date", flush=True)
    print("="*70, flush=True)
    print(flush=True)
    
    if not os.path.exists(SCRIPT_PATH):
        print(f"❌ Script not found: {SCRIPT_PATH}", flush=True)
        sys.exit(1)
    
    required_vars = [
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET',
        'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN',
        'DUST_API_KEY', 'DUST_WORKSPACE_ID'
    ]
    
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}", flush=True)
        sys.exit(1)
    
    print("✓ All environment variables found", flush=True)
    print(flush=True)
    
    progress = load_progress()
    
    all_dates = []
    for days_ago in range(DAYS_TO_BACKFILL, 0, -1):
        date = datetime.now() - timedelta(days=days_ago)
        all_dates.append(date.strftime('%Y-%m-%d'))
    
    dates_to_process = [
        d for d in all_dates 
        if d not in progress['completed_dates'] 
        and d not in progress.get('in_progress', [])
    ]
    
    if len(dates_to_process) == 0:
        print("✅ All dates already processed!", flush=True)
        print(f"   Completed: {len(progress['completed_dates'])} days", flush=True)
        return
    
    print(f"📅 Date Range: {all_dates[0]} to {all_dates[-1]}", flush=True)
    print(f"📊 Total: {len(all_dates)} | Remaining: {len(dates_to_process)}", flush=True)
    print(flush=True)
    
    estimated_minutes = (len(dates_to_process) * 6) / PARALLEL_WORKERS
    print(f"⏱️  Estimated: ~{estimated_minutes:.0f} minutes ({estimated_minutes/60:.1f} hours)", flush=True)
    print(f"   (Based on ~6 min/date observed performance)", flush=True)
    print(flush=True)
    
    if sys.stdin.isatty() and len(dates_to_process) > 10:
        try:
            response = input("Continue? (yes/no): ")
            if response.lower() not in ['yes', 'y']:
                print("Cancelled.", flush=True)
                return
            print(flush=True)
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", flush=True)
            return
    elif len(dates_to_process) > 10:
        print("⚠️  Automated mode - proceeding...", flush=True)
        print(flush=True)
    
    start_time = time.time()
    successful = 0
    failed = 0
    completed_count = 0
    total_file_size = 0
    
    print(f"{'='*70}", flush=True)
    print(f"⚡ PROCESSING {len(dates_to_process)} DAYS WITH {PARALLEL_WORKERS} WORKERS", flush=True)
    print('='*70, flush=True)
    print(flush=True)
    
    # Show which dates are being submitted
    print(f"📋 Submitting {min(PARALLEL_WORKERS, len(dates_to_process))} dates to workers...", flush=True)
    for i, date in enumerate(dates_to_process[:PARALLEL_WORKERS]):
        print(f"   Worker {i+1}: {date}", flush=True)
    print(flush=True)
    
    try:
        with ProcessPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            future_to_date = {
                executor.submit(process_single_date, date_str): date_str 
                for date_str in dates_to_process
            }
            
            progress['in_progress'] = dates_to_process.copy()
            save_progress(progress)
            
            print("⏳ Workers running... Results will appear as dates complete", flush=True)
            print(flush=True)
            
            for future in as_completed(future_to_date):
                completed_count += 1
                result = future.result()
                
                elapsed = time.time() - start_time
                remaining = len(dates_to_process) - completed_count
                avg_time_actual = elapsed / completed_count if completed_count > 0 else 0
                eta = remaining * avg_time_actual
                progress_pct = (completed_count / len(dates_to_process)) * 100
                
                if result['success']:
                    successful += 1
                    total_file_size += result['file_size']
                    progress['completed_dates'].append(result['date'])
                    progress['last_processed'] = result['date']
                    
                    if result['file_size'] > 3:
                        status_icon = "✅"
                        status = f"({result['file_size']:5.1f} KB)"
                    else:
                        status_icon = "⚠️ "
                        status = "(small)"
                    
                    # REAL-TIME OUTPUT with flush=True
                    print(f"{status_icon} [{completed_count:3d}/{len(dates_to_process)}] {result['date']} {status} in {format_duration(result['duration'])} | Progress: {progress_pct:5.1f}% | ETA: {format_duration(eta)}", flush=True)
                else:
                    failed += 1
                    progress['failed_dates'].append(result['date'])
                    error_msg = result['stderr'][:50] if result['stderr'] else 'Unknown'
                    print(f"❌ [{completed_count:3d}/{len(dates_to_process)}] {result['date']} FAILED: {error_msg}", flush=True)
                
                if result['date'] in progress.get('in_progress', []):
                    progress['in_progress'].remove(result['date'])
                
                # Save progress every 5 completions
                if completed_count % 5 == 0:
                    save_progress(progress)
                    print(f"💾 Progress saved ({completed_count} completed)", flush=True)
            
            progress['in_progress'] = []
            save_progress(progress)
    
    except KeyboardInterrupt:
        print("\n\n🛑 INTERRUPTED", flush=True)
        save_progress(progress)
        sys.exit(0)
    
    total_time = time.time() - start_time
    print(f"\n{'='*70}", flush=True)
    print("✅ BACKFILL COMPLETE", flush=True)
    print('='*70, flush=True)
    print(f"\n📊 Summary:", flush=True)
    print(f"   Total time: {format_duration(total_time)}", flush=True)
    print(f"   Successful: {successful}/{len(dates_to_process)}", flush=True)
    print(f"   Failed: {failed}/{len(dates_to_process)}", flush=True)
    
    if successful + failed > 0:
        success_rate = successful/(successful+failed)*100
        print(f"   Success rate: {success_rate:.1f}%", flush=True)
    
    if successful > 0:
        print(f"   Total data: {total_file_size/1024:.1f} MB", flush=True)
        avg_size = total_file_size / successful if successful > 0 else 0
        print(f"   Avg file size: {avg_size:.1f} KB", flush=True)
        avg_time = total_time / successful if successful > 0 else 0
        print(f"   Avg time/date: {format_duration(avg_time)}", flush=True)
    
    if progress['failed_dates']:
        print(f"\n❌ Failed: {len(progress['failed_dates'])}", flush=True)
        for i, date in enumerate(progress['failed_dates'][:5], 1):
            print(f"   {i}. {date}", flush=True)
    
    if failed == 0 and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print(f"\n✓ Progress file removed", flush=True)
    
    print(flush=True)
    print("="*70, flush=True)
    print(f"💡 To backfill more days, increase DAYS_TO_BACKFILL", flush=True)
    print(f"   Current: {DAYS_TO_BACKFILL} days", flush=True)
    if DAYS_TO_BACKFILL < 90:
        next_target = min(DAYS_TO_BACKFILL * 2, 90)
        print(f"   Next: Try {next_target} days", flush=True)
    print("="*70, flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
