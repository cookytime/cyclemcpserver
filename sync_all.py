#!/usr/bin/env python3
"""
Combined sync script that syncs tracks, routines, and track feedback from base44.
Tracks are synced first so routine-track associations can be properly linked.
"""

import sys
from datetime import datetime

from sync import Base44Sync
from sync_routines import Base44RoutineSync
from sync_trackfeedback import Base44TrackFeedbackSync


def main():
    print("=== Cycle MCP Server Complete Sync ===\n")
    overall_start = datetime.now()
    results = {}

    steps = [
        ("Tracks", Base44Sync),
        ("Routines", Base44RoutineSync),
        ("Track Feedback", Base44TrackFeedbackSync),
    ]

    for i, (name, syncer_class) in enumerate(steps, 1):
        print(f"STEP {i}: Syncing {name}")
        print("=" * 50)
        syncer = syncer_class()
        results[name] = syncer.run_sync()
        print("\n")

    overall_end = datetime.now()

    # Summary
    print("=" * 50)
    print("SYNC COMPLETE")
    print("=" * 50)

    all_success = True
    for name, success in results.items():
        status = "✓" if success else "✗"
        print(f"  {status} {name}")
        if not success:
            all_success = False

    print(f"\nTotal time: {(overall_end - overall_start).total_seconds():.2f} seconds")

    sys.exit(0 if all_success else 1)


if __name__ == "__main__":
    main()
