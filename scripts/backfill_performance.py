#!/usr/bin/env python3
"""
Backfill historical performance data for 365 days
Resumes from where it left off if interrupted
"""

import subprocess
import time
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

# Configuration
DAYS_TO_BACKFILL = 365
SCRIPT_PATH = 'scripts/track_meeting_load.py'
DELAY_BETWEEN_RUNS = 2  # seconds
TIMEOUT_PER_DAY = 300  # 5 minutes
PROGRESS_FILE = 'backfill_progress.json'

def load_progress():
    """Load progress from previous run"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {
        'completed_dates': [],
        'failed_dates': [],
        'last_processed': None
    }

def save_progress(progress):
    """Save current progress"""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

def format_duration(seconds):
    """Format seconds into human-readable duration"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

def main():
    print("="*70)
    print("🚀 PERFORMANCE DATA BACKFILL - 365 DAYS")
    print("="*70)
    print()
    
    # Check if script exists
    if not os.path.exists(SCRIPT_PATH):
        print(f"❌ Script not found: {SCRIPT_PATH}")
        print(f"   Make sure you're running from the project root directory")
        return
    
    # Load previous progress
    progress = load_progress()
    
    # Generate list of dates to process
    all_dates = []
    for days_ago in range(DAYS_TO_BACKFILL, 0, -1):
        date = datetime.now() - timedelta(days=days_ago)
        date_str = date.strftime('%Y-%m-%d')
        all_dates.append(date_str)
    
    # Filter out already completed dates
    dates_to_process = [d for d in all_dates if d not in progress['completed_dates']]
    
    if len(dates_to_process) == 0:
        print("✅ All dates already processed!")
        print(f"   Completed: {len(progress['completed_dates'])} days")
        print(f"   Failed: {len(progress['failed_dates'])} days")
        if progress['failed_dates']:
            print(f"\n   Failed dates: {', '.join(progress['failed_dates'][:10])}")
            if len(progress['failed_dates']) > 10:
                print(f"   ... and {len(progress['failed_dates']) - 10} more")
        return
    
    print(f"📅 Date Range: {all_dates[0]} to {all_dates[-1]}")
    print(f"📊 Total days: {len(all_dates)}")
    print(f"✅ Already completed: {len(progress['completed_dates'])}")
    print(f"❌ Previously failed: {len(progress['failed_dates'])}")
    print(f"⏳ Remaining: {len(dates_to_process)}")
    print()
    
    if progress['completed_dates']:
        print(f"📍 Resuming from: {dates_to_process[0]}")
        print()
    
    estimated_time = len(dates_to_process) * (TIMEOUT_PER_DAY / 60 + DELAY_BETWEEN_RUNS / 60)
    print(f"⏱️  Estimated time: ~{estimated_time:.0f} minutes ({estimated_time/60:.1f} hours)")
    print()
    
    # Confirm before starting
    if len(dates_to_process) > 30:
        response = input("Continue? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("Cancelled.")
            return
        print()
    
    # Process dates
    start_time = time.time()
    successful = 0
    failed = 0
    
    for i, date_str in enumerate(dates_to_process, 1):
        remaining = len(dates_to_process) - i
        elapsed = time.time() - start_time
        avg_time_per_day = elapsed / i if i > 0 else 0
        estimated_remaining = remaining * avg_time_per_day
        
        print(f"\n{'='*70}")
        print(f"📅 Day {i}/{len(dates_to_process)}: {date_str}")
        print(f"⏱️  Elapsed: {format_duration(elapsed)} | Remaining: ~{format_duration(estimated_remaining)}")
        print('='*70)
        
        day_start = time.time()
        
        try:
            # Run the script for this date
            result = subprocess.run(
                ['python', SCRIPT_PATH, '--date', date_str],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_PER_DAY
            )
            
            day_duration = time.time() - day_start
            
            if result.returncode == 0:
                print(f"✅ SUCCESS in {format_duration(day_duration)}")
                successful += 1
                progress['completed_dates'].append(date_str)
                progress['last_processed'] = date_str
                
                # Check if file was created
                filename = f"tech_performance_{date_str.replace('-', '')}.json"
                if os.path.exists(filename):
                    file_size = os.path.getsize(filename) / 1024
                    print(f"📁 Created: {filename} ({file_size:.1f} KB)")
            else:
                print(f"❌ FAILED after {format_duration(day_duration)}")
                print(f"   Error code: {result.returncode}")
                if result.stderr:
                    print(f"   Error: {result.stderr[:200]}")
                failed += 1
                progress['failed_dates'].append(date_str)
            
            # Save progress after each date
            save_progress(progress)
            
        except subprocess.TimeoutExpired:
            day_duration = time.time() - day_start
            print(f"⏱️  TIMEOUT after {format_duration(day_duration)}")
            failed += 1
            progress['failed_dates'].append(date_str)
            save_progress(progress)
            
        except KeyboardInterrupt:
            print("\n\n🛑 INTERRUPTED BY USER")
            print(f"\n📊 Progress saved to: {PROGRESS_FILE}")
            print(f"   Successful: {successful}")
            print(f"   Failed: {failed}")
            print(f"   Last processed: {date_str}")
            print(f"\n💡 Run this script again to resume from where you left off")
            save_progress(progress)
            return
        
        except Exception as e:
            print(f"❌ ERROR: {e}")
            failed += 1
            progress['failed_dates'].append(date_str)
            save_progress(progress)
        
        # Delay between runs to avoid rate limits
        if i < len(dates_to_process):
            time.sleep(DELAY_BETWEEN_RUNS)
    
    # Final summary
    total_time = time.time() - start_time
    print(f"\n{'='*70}")
    print("✅ BACKFILL COMPLETE")
    print('='*70)
    print(f"\n📊 Summary:")
    print(f"   Total time: {format_duration(total_time)}")
    print(f"   Successful: {successful}/{len(dates_to_process)}")
    print(f"   Failed: {failed}/{len(dates_to_process)}")
    print(f"   Success rate: {successful/(successful+failed)*100:.1f}%")
    
    if progress['failed_dates']:
        print(f"\n❌ Failed dates ({len(progress['failed_dates'])}):")
        for date in progress['failed_dates'][:20]:
            print(f"   - {date}")
        if len(progress['failed_dates']) > 20:
            print(f"   ... and {len(progress['failed_dates']) - 20} more")
        print(f"\n💡 You can retry failed dates manually:")
        print(f"   python scripts/track_meeting_load.py --date YYYY-MM-DD")
    
    # Cleanup progress file on success
    if failed == 0:
        os.remove(PROGRESS_FILE)
        print(f"\n🗑️  Removed progress file (all dates completed)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
