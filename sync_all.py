#!/usr/bin/env python3
"""
Combined sync script that syncs both tracks and routines from base44.
This ensures tracks are synced first, so routine-track associations can be properly linked.
"""
import sys
from datetime import datetime
from sync import Base44Sync
from sync_routines import Base44RoutineSync

def main():
    print("=== base44 Complete Sync (Tracks + Routines) ===\n")
    overall_start = datetime.now()

    # Step 1: Sync tracks first
    print("STEP 1: Syncing Tracks")
    print("=" * 50)
    track_syncer = Base44Sync()
    tracks_success = track_syncer.run_sync()

    if not tracks_success:
        print("\n✗ Track sync failed. Aborting routine sync.")
        sys.exit(1)

    print("\n")

    # Step 2: Sync routines (which reference tracks)
    print("STEP 2: Syncing Routines")
    print("=" * 50)
    routine_syncer = Base44RoutineSync()
    routines_success = routine_syncer.run_sync()

    overall_end = datetime.now()

    # Summary
    print("\n" + "=" * 50)
    print("SYNC COMPLETE")
    print("=" * 50)

    if tracks_success and routines_success:
        print("✓ All syncs completed successfully!")
    elif tracks_success:
        print("⚠ Tracks synced successfully, but routine sync had issues")
    else:
        print("✗ Sync completed with errors")

    print(f"\nTotal time: {(overall_end - overall_start).total_seconds():.2f} seconds")

    sys.exit(0 if (tracks_success and routines_success) else 1)

if __name__ == '__main__':
    main()
