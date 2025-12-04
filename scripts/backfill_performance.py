# ... (keep all the previous functions unchanged until process_single_date)

def process_single_date(target_date, technicians, graph_token, sdp_token):
    """Process all technicians for a single date"""
    logger.info(f"Processing: {target_date.strftime('%Y-%m-%d')}")
    
    tech_data = {}
    
    with ThreadPoolExecutor(max_workers=PARALLEL_TECHNICIANS) as executor:
        futures = {
            executor.submit(
                process_technician,
                tech_name,
                tech_email,
                graph_token,
                sdp_token,
                target_date
            ): tech_name
            for tech_name, tech_email in technicians.items()
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                tech_data[result['name']] = {
                    'meetings': result['meetings'],
                    'tickets': result['tickets'],
                    'performance': result['performance']
                }
            except Exception as e:
                tech_name = futures[future]
                logger.error(f"Failed to process {tech_name}: {e}")
    
    # Display results (same format as daily script)
    print()
    print("="*70)
    print(f"{target_date.strftime('%B %d, %Y')}")
    print("="*70)
    
    for tech_name, data in sorted(tech_data.items(), key=lambda x: x[1]['performance']['score'], reverse=True):
        m = data['meetings']
        t = data['tickets']
        p = data['performance']
        print(f"{tech_name:25} | {t['closed_today']:2} closed | {m['count']:2} mtgs | Score: {p['score']:5.1f}")
    
    print()
    
    # Save and upload
    filename = save_to_file(tech_data, target_date)
    uploaded = upload_to_dust(tech_data, target_date)
    
    logger.info(f"✓ Saved: {filename}, Uploaded to Dust: {uploaded}/{len(tech_data)}")
    
    return filename

def main():
    """Main backfill execution"""
    parser = argparse.ArgumentParser(description='Backfill performance data for date range')
    parser.add_argument('--days', type=int, help='Number of days to backfill')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (YYYY-MM-DD)')
    args = parser.parse_args()
    
    print("="*70)
    print("PERFORMANCE DATA BACKFILL v15.0.2")
    print("="*70)
    print()
    
    # Interactive mode if no arguments provided
    if not args.days and not args.start_date:
        print("Choose backfill mode:")
        print("  1. Backfill last N days")
        print("  2. Backfill specific date range")
        print()
        
        choice = input("Enter choice (1 or 2): ").strip()
        
        if choice == "1":
            days_input = input("How many days to backfill? (default: 30): ").strip()
            args.days = int(days_input) if days_input else 30
        elif choice == "2":
            args.start_date = input("Start date (YYYY-MM-DD): ").strip()
            args.end_date = input("End date (YYYY-MM-DD): ").strip()
        else:
            print("Invalid choice. Exiting.")
            sys.exit(1)
        
        print()
    
    # Get tokens
    try:
        graph_token = get_graph_token()
        sdp_token = get_sdp_token()
    except Exception as e:
        logger.error(f"Failed to get tokens: {e}")
        sys.exit(1)
    
    # Discover technicians
    try:
        logger.info("Discovering technicians...")
        technicians = discover_technicians(sdp_token)
        logger.info(f"✓ Found {len(technicians)} technicians")
        print()
    except Exception as e:
        logger.error(f"Failed to discover technicians: {e}")
        sys.exit(1)
    
    if not technicians:
        logger.error("No technicians found")
        sys.exit(1)
    
    # Build date list
    if args.start_date:
        start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
        if args.end_date:
            end_date = datetime.strptime(args.end_date, '%Y-%m-%d')
        else:
            logger.error("--end-date required when using --start-date")
            sys.exit(1)
        
        dates = []
        current = start_date
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=1)
    else:
        # Backfill last N days
        days = args.days if args.days else 30
        dates = [(datetime.now() - timedelta(days=i)) for i in range(1, days + 1)]
        dates.reverse()  # Process oldest first
    
    # Load progress
    progress = load_progress()
    completed_dates = {d for d in progress['completed']}
    dates_to_process = [d for d in dates if d.strftime('%Y-%m-%d') not in completed_dates]
    
    if not dates_to_process:
        logger.info("All dates already processed!")
        return
    
    logger.info(f"Processing {len(dates_to_process)} dates")
    if completed_dates:
        logger.info(f"(Skipping {len(completed_dates)} already completed dates)")
    
    # Confirm before proceeding
    if len(dates_to_process) > 5:
        confirm = input(f"\nProcess {len(dates_to_process)} dates? (yes/no): ").strip().lower()
        if confirm not in ['yes', 'y']:
            print("Cancelled.")
            return
    
    print()
    
    # Process dates sequentially
    start_time = time.time()
    
    for i, date in enumerate(dates_to_process, 1):
        try:
            process_single_date(date, technicians, graph_token, sdp_token)
            progress['completed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
            
            # Show progress
            elapsed = time.time() - start_time
            avg_time = elapsed / i
            remaining = len(dates_to_process) - i
            eta = remaining * avg_time
            
            print(f"Progress: [{i}/{len(dates_to_process)}] ({i/len(dates_to_process)*100:.0f}%) | Elapsed: {elapsed/60:.1f} min | ETA: {eta/60:.1f} min")
            print()
            
            # Brief pause between dates to avoid rate limiting
            if i < len(dates_to_process):
                time.sleep(2)
            
        except Exception as e:
            logger.error(f"Failed to process {date.strftime('%Y-%m-%d')}: {e}")
            progress['failed'].append(date.strftime('%Y-%m-%d'))
            save_progress(progress)
    
    # Summary
    total_time = time.time() - start_time
    print()
    print("="*70)
    print("BACKFILL COMPLETE")
    print("="*70)
    print(f"Total time: {total_time/60:.1f} minutes")
    print(f"Processed: {len(dates_to_process)} dates")
    if progress['failed']:
        print(f"Failed: {len(progress['failed'])} dates: {', '.join(progress['failed'])}")
    else:
        print("Failed: 0 dates")
    print()
    
    # Clean up progress file if all succeeded
    if not progress['failed']:
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
            logger.info("✓ Progress file removed")
    else:
        logger.info(f"✓ Progress saved to {PROGRESS_FILE} - run again to retry failed dates")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted - progress saved")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
