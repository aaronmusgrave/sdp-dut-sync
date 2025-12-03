#!/usr/bin/env python3
"""
Backfill performance data - WITH REAL-TIME PROGRESS
Uses v14.5.3 tracker script (pagination bug fixed!)
Processes dates in REVERSE (newest first - December before November)
"""

import subprocess
import time
import json
import os
import sys
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed

# ============================================================================
# CONFIGURATION - CHANGE THESE VALUES (or set via workflow)
# ============================================================================
DAYS_TO_BACKFILL = 90        # SET THIS: Number of days to backfill
PARALLEL_WORKERS = 5          # Process 5 dates simultaneously
TIMEOUT_PER_DAY = 600         # Max 10 minutes per date
DELAY_BETWEEN_RUNS = 2        # Delay between date submissions (seconds)
# ============================================================================

SCRIPT_PATH = 'scripts/track_meeting_load.py'
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
    sys.stdout.reconfigure(line_buffering=True)
    
    print("="*70, flush=True)
    print(f"PERFORMANCE DATA BACKFILL - LAST {DAYS_TO_BACKFILL} DAYS", flush=True)
    print(f"   {PARALLEL_WORKERS} WORKERS | v14.5.3 (pagination fixed!) | NEWEST FIRST!", flush=True)
    print("="*70, flush=True)
    print(flush=True)
    
    if not os.path.exists(SCRIPT_PATH):
        print(f"Script not found: {SCRIPT_PATH}", flush=True)
        sys.exit(1)
    
    required_vars = [
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET',
        'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN',
        'DUST_API_KEY', 'DUST_WORKSPACE_ID'
    ]
    
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", flush=True)
        sys.exit(1)
    
    print("All environment variables found", flush=True)
    print(flush=True)
    
    progress = load_progress()
    
    # BUILD DATE RANGE - NEWEST FIRST (Dec before Nov)
    all_dates = []
    for days_ago in range(1, DAYS_TO_BACKFILL + 1):  # Changed: process recent first
        date = datetime.now() - timedelta(days=days_ago)
        all_dates.append(date.strftime('%Y-%m-%d'))
    # all_dates is now: [yesterday, 2 days ago, ..., N days ago]
    
    dates_to_process = [
        d for d in all_dates 
        if d not in progress['completed_dates'] 
        and d not in progress.get('in_progress', [])
    ]
    
    if len(dates_to_process) == 0:
        print("All dates already processed!", flush=True)
        print(f"   Completed: {len(progress['completed_dates'])} days", flush=True)
        if progress['failed_dates']:
            print(f"   Failed: {len(progress['failed_dates'])} days", flush=True)
        return
    
    print(f"Date Range: {all_dates[0]} (newest) to {all_dates[-1]} (oldest)", flush=True)
    print(f"Total: {len(all_dates)} | Completed: {len(progress['completed_dates'])} | Remaining: {len(dates_to_process)}", flush=True)
    print(flush=True)
    
    estimated_minutes = (len(dates_to_process) * 3.5) / PARALLEL_WORKERS
    print(f"Estimated: ~{estimated_minutes:.0f} minutes ({estimated_minutes/60:.1f} hours)", flush=True)
    print(f"   (Based on v14.5.3 with {PARALLEL_WORKERS} workers - pagination fixed)", flush=True)
    if DELAY_BETWEEN_RUNS > 0:
        print(f"   Delay between submissions: {DELAY_BETWEEN_RUNS} second(s)", flush=True)
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
        print("Automated mode - proceeding...", flush=True)
        print(flush=True)
    
    start_time = time.time()
    successful = 0
    failed = 0
    completed_count = 0
    total_file_size = 0
    
    print(f"{'='*70}", flush=True)
    print(f"PROCESSING {len(dates_to_process)} DAYS WITH {PARALLEL_WORKERS} WORKERS", flush=True)
    print(f"ORDER: NEWEST FIRST (Dec -> Nov -> Oct -> Sept)", flush=True)
    print('='*70, flush=True)
    print(flush=True)
    
    print(f"Submitting first {min(PARALLEL_WORKERS, len(dates_to_process))} dates to workers...", flush=True)
    for i, date in enumerate(dates_to_process[:PARALLEL_WORKERS]):
        print(f"   Worker {i+1}: {date}", flush=True)
    print(flush=True)
    
    try:
        with ProcessPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            future_to_date = {}
            
            for date_str in dates_to_process:
                future_to_date[executor.submit(process_single_date, date_str)] = date_str
                if DELAY_BETWEEN_RUNS > 0 and len(future_to_date) < len(dates_to_process):
                    time.sleep(DELAY_BETWEEN_RUNS)
            
            progress['in_progress'] = dates_to_process.copy()
            save_progress(progress)
            
            print("Workers running... Results will appear as dates complete", flush=True)
            print(flush=True)
            
            for future in as_completed(future_to_date):
                completed_count += 1
                result = future.result()
                
                elapsed = time.time() - start_time
                remaining = len(dates_to_process) - completed_count
                avg_time_actual = elapsed / completed_count if completed_count > 0 else 0
                eta = remaining * avg_time_actual / PARALLEL_WORKERS
                progress_pct = (completed_count / len(dates_to_process)) * 100
                
                if result['success']:
                    successful += 1
                    total_file_size += result['file_size']
                    progress['completed_dates'].append(result['date'])
                    progress['last_processed'] = result['date']
                    
                    if result['file_size'] > 3:
                        status_icon = "OK"
                        status = f"({result['file_size']:5.1f} KB)"
                    else:
                        status_icon = "WARN"
                        status = "(small)"
                    
                    print(f"{status_icon} [{completed_count:3d}/{len(dates_to_process)}] {result['date']} {status} in {format_duration(result['duration'])} | Progress: {progress_pct:5.1f}% | ETA: {format_duration(eta)}", flush=True)
                else:
                    failed += 1
                    progress['failed_dates'].append(result['date'])
                    error_msg = result['stderr'][:50] if result['stderr'] else 'Unknown'
                    print(f"FAIL [{completed_count:3d}/{len(dates_to_process)}] {result['date']} FAILED: {error_msg}", flush=True)
                
                if result['date'] in progress.get('in_progress', []):
                    progress['in_progress'].remove(result['date'])
                
                if completed_count % 5 == 0:
                    save_progress(progress)
                    print(f"Progress saved ({completed_count} completed)", flush=True)
            
            progress['in_progress'] = []
            save_progress(progress)
    
    except KeyboardInterrupt:
        print("\n\nINTERRUPTED", flush=True)
        save_progress(progress)
        sys.exit(0)
    
    total_time = time.time() - start_time
    print(f"\n{'='*70}", flush=True)
    print("BACKFILL COMPLETE", flush=True)
    print('='*70, flush=True)
    print(f"\nSummary:", flush=True)
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
        print(f"\nFailed: {len(progress['failed_dates'])}", flush=True)
        for i, date in enumerate(progress['failed_dates'][:5], 1):
            print(f"   {i}. {date}", flush=True)
        if len(progress['failed_dates']) > 5:
            print(f"   ... and {len(progress['failed_dates']) - 5} more", flush=True)
    
    if failed == 0 and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print(f"\nProgress file removed", flush=True)
    else:
        print(f"\nProgress saved to: {PROGRESS_FILE}", flush=True)
        print(f"   Run again to retry failed dates", flush=True)
    
    print(flush=True)
    print("="*70, flush=True)
    print(f"Configuration:", flush=True)
    print(f"   Days to backfill: {DAYS_TO_BACKFILL}", flush=True)
    print(f"   Parallel workers: {PARALLEL_WORKERS}", flush=True)
    print(f"   Delay between runs: {DELAY_BETWEEN_RUNS}s", flush=True)
    print(f"   Processing order: NEWEST FIRST (recent -> old)", flush=True)
    print(f"   To change: Edit lines 18-21 or use workflow inputs", flush=True)
    print("="*70, flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
