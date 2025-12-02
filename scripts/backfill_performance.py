#!/usr/bin/env python3
"""
Backfill performance data - FIXED to pass environment variables
"""

import subprocess
import time
import json
import os
import sys
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed

# Configuration
DAYS_TO_BACKFILL = 90
SCRIPT_PATH = 'scripts/track_meeting_load.py'
PARALLEL_WORKERS = 4
TIMEOUT_PER_DAY = 600  # Increased to 10 minutes
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
    """Process a single date - FIXED to pass environment"""
    start_time = time.time()
    try:
        # CRITICAL FIX: Pass environment variables to subprocess
        env = os.environ.copy()
        
        result = subprocess.run(
            ['python', SCRIPT_PATH, '--date', date_str],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_PER_DAY,
            env=env,  # ← THIS IS THE FIX!
            cwd=os.getcwd()  # Ensure correct working directory
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
            'stdout': result.stdout[-300:] if result.stdout else '',
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
            'stdout': '',
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
            'stdout': '',
            'filename': None,
            'file_size': 0,
            'duration': duration
        }

def main():
    print("="*70)
    print("🚀 PERFORMANCE DATA BACKFILL - FIXED")
    print(f"   Environment variables properly passed")
    print(f"   {PARALLEL_WORKERS} workers, 10-minute timeout per date")
    print("="*70)
    print()
    
    if not os.path.exists(SCRIPT_PATH):
        print(f"❌ Script not found: {SCRIPT_PATH}")
        sys.exit(1)
    
    # Verify required environment variables
    required_vars = [
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET',
        'SDP_DOMAIN', 'SDP_CLIENT_ID', 'SDP_CLIENT_SECRET', 'SDP_REFRESH_TOKEN',
        'DUST_API_KEY', 'DUST_WORKSPACE_ID'
    ]
    
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    print("✓ All required environment variables found")
    print()
    
    progress = load_progress()
    
    # Generate dates
    all_dates = []
    for days_ago in range(DAYS_TO_BACKFILL, 0, -1):
        date = datetime.now() - timedelta(days=days_ago)
        all_dates.append(date.strftime('%Y-%m-%d'))
    
    # Filter completed
    dates_to_process = [
        d for d in all_dates 
        if d not in progress['completed_dates'] 
        and d not in progress.get('in_progress', [])
    ]
    
    if len(dates_to_process) == 0:
        print("✅ All dates already processed!")
        print(f"   Completed: {len(progress['completed_dates'])} days")
        print(f"   Failed: {len(progress['failed_dates'])} days")
        return
    
    print(f"📅 Date Range: {all_dates[0]} to {all_dates[-1]}")
    print(f"📊 Total: {len(all_dates)} | Completed: {len(progress['completed_dates'])} | Remaining: {len(dates_to_process)}")
    print()
    
    # Estimate
    avg_time = 180  # 3 minutes per date
    estimated_time = (len(dates_to_process) * avg_time) / PARALLEL_WORKERS / 60
    print(f"⏱️  Estimated time: ~{estimated_time:.0f} minutes ({estimated_time/60:.1f} hours)")
    print()
    
    # Confirmation
    if sys.stdin.isatty() and len(dates_to_process) > 10:
        try:
            response = input("Continue? (yes/no): ")
            if response.lower() not in ['yes', 'y']:
                print("Cancelled.")
                return
            print()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
    elif len(dates_to_process) > 10:
        print("⚠️  Running in automated mode")
        print()
    
    # Process dates
    start_time = time.time()
    successful = 0
    failed = 0
    completed_count = 0
    total_file_size = 0
    
    print(f"{'='*70}")
    print(f"⚡ PROCESSING {len(dates_to_process)} DAYS WITH {PARALLEL_WORKERS} WORKERS")
    print('='*70)
    print()
    
    try:
        with ProcessPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            future_to_date = {
                executor.submit(process_single_date, date_str): date_str 
                for date_str in dates_to_process
            }
            
            progress['in_progress'] = dates_to_process.copy()
            save_progress(progress)
            
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
                    
                    # Verify data exists
                    if result['file_size'] > 5:  # At least 5 KB
                        status_icon = "✅"
                        status = f"({result['file_size']:5.1f} KB)"
                    else:
                        status_icon = "⚠️ "
                        status = "(EMPTY)"
                    
                    print(f"{status_icon} [{completed_count:3d}/{len(dates_to_process)}] {result['date']} {status} in {format_duration(result['duration'])} | Progress: {progress_pct:5.1f}% | ETA: {format_duration(eta)}")
                else:
                    failed += 1
                    progress['failed_dates'].append(result['date'])
                    error_msg = result['stderr'][:60] if result['stderr'] else 'Unknown error'
                    print(f"❌ [{completed_count:3d}/{len(dates_to_process)}] {result['date']} FAILED: {error_msg}")
                
                if result['date'] in progress.get('in_progress', []):
                    progress['in_progress'].remove(result['date'])
                
                # Save progress every 5 completions
                if completed_count % 5 == 0:
                    save_progress(progress)
            
            progress['in_progress'] = []
            save_progress(progress)
    
    except KeyboardInterrupt:
        print("\n\n🛑 INTERRUPTED")
        save_progress(progress)
        sys.exit(0)
    
    # Summary
    total_time = time.time() - start_time
    print(f"\n{'='*70}")
    print("✅ BACKFILL COMPLETE")
    print('='*70)
    print(f"\n📊 Summary:")
    print(f"   Total time: {format_duration(total_time)}")
    print(f"   Successful: {successful}/{len(dates_to_process)}")
    print(f"   Failed: {failed}/{len(dates_to_process)}")
    
    if successful + failed > 0:
        success_rate = successful/(successful+failed)*100
        print(f"   Success rate: {success_rate:.1f}%")
    
    if successful > 0:
        print(f"   Total data: {total_file_size/1024:.1f} MB")
        avg_size = total_file_size / successful if successful > 0 else 0
        print(f"   Avg file size: {avg_size:.1f} KB")
        
        if avg_size < 5:
            print(f"\n⚠️  WARNING: Files are suspiciously small!")
            print(f"   Most files may not contain data.")
        else:
            print(f"\n✅ File sizes look good!")
    
    if progress['failed_dates']:
        print(f"\n❌ Failed dates ({len(progress['failed_dates'])}):")
        for i, date in enumerate(progress['failed_dates'][:10], 1):
            print(f"   {i:2d}. {date}")
        if len(progress['failed_dates']) > 10:
            print(f"   ... and {len(progress['failed_dates']) - 10} more")
    
    if failed == 0 and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    
    print()
    print("="*70)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
