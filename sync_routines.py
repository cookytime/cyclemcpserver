#!/usr/bin/env python3
import sys
import json
import requests
import psycopg2
from psycopg2.extras import Json
from datetime import datetime
from config import Config

class Base44RoutineSync:
    def __init__(self):
        Config.validate()
        self.api_key = Config.BASE44_API_KEY
        self.api_url = Config.BASE44_API_URL
        self.conn = None

    def connect_db(self):
        """Connect to PostgreSQL database"""
        try:
            self.conn = psycopg2.connect(Config.get_db_connection_string())
            print("✓ Connected to PostgreSQL database")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to database: {e}")
            return False

    def fetch_routines_from_base44(self):
        """Fetch routines from base44 API"""
        headers = {
            'api_key': self.api_key,
            'Content-Type': 'application/json'
        }

        try:
            # base44 API endpoint for Routine entities
            url = f'{self.api_url}/apps/{Config.BASE44_APP_ID}/entities/Routine'
            response = requests.get(
                url,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            routines = response.json()

            print(f"✓ Fetched {len(routines)} routines from base44")
            return routines

        except requests.exceptions.RequestException as e:
            print(f"✗ Failed to fetch routines from base44: {e}")
            return None

    def sync_routine(self, routine, cursor, conn):
        """Sync a single routine to the database"""
        # Create a savepoint so we can rollback just this routine on error
        cursor.execute("SAVEPOINT routine_sync")

        try:
            # Extract routine data from base44 API response
            base44_id = routine.get('id')
            name = routine.get('name')
            description = routine.get('description')
            theme = routine.get('theme')
            intensity_arc = routine.get('intensity_arc')
            resistance_scale_notes = routine.get('resistance_scale_notes')
            class_summary = routine.get('class_summary')
            total_duration_minutes = routine.get('total_duration_minutes')
            difficulty = routine.get('difficulty')
            spotify_playlist_id = routine.get('spotify_playlist_id')
            track_ids = routine.get('track_ids', [])
            tags = routine.get('tags', [])

            if not base44_id or not name:
                print(f"⚠ Skipping routine with missing required fields: {routine}")
                cursor.execute("RELEASE SAVEPOINT routine_sync")
                return False

            # Convert tags array to JSON for PostgreSQL JSONB storage
            tags_json = Json(tags) if tags else None

            # Insert or update routine
            cursor.execute("""
                INSERT INTO routines (
                    base44_id, name, description, theme, intensity_arc,
                    resistance_scale_notes, class_summary, total_duration_minutes,
                    difficulty, spotify_playlist_id, tags, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (base44_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    theme = EXCLUDED.theme,
                    intensity_arc = EXCLUDED.intensity_arc,
                    resistance_scale_notes = EXCLUDED.resistance_scale_notes,
                    class_summary = EXCLUDED.class_summary,
                    total_duration_minutes = EXCLUDED.total_duration_minutes,
                    difficulty = EXCLUDED.difficulty,
                    spotify_playlist_id = EXCLUDED.spotify_playlist_id,
                    tags = EXCLUDED.tags,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id, (xmax = 0) AS inserted
            """, (
                base44_id, name, description, theme, intensity_arc,
                resistance_scale_notes, class_summary, total_duration_minutes,
                difficulty, spotify_playlist_id, tags_json
            ))

            result = cursor.fetchone()
            routine_id = result[0]
            was_inserted = result[1]

            # Delete existing track associations for this routine
            cursor.execute("DELETE FROM routine_tracks WHERE routine_id = %s", (routine_id,))

            # Insert track associations in order
            for order, track_base44_id in enumerate(track_ids, start=1):
                # Try to find the track_id if the track exists in our database
                cursor.execute("SELECT id FROM tracks WHERE base44_id = %s", (track_base44_id,))
                track_row = cursor.fetchone()
                track_id = track_row[0] if track_row else None

                cursor.execute("""
                    INSERT INTO routine_tracks (routine_id, track_base44_id, track_id, track_order)
                    VALUES (%s, %s, %s, %s)
                """, (routine_id, track_base44_id, track_id, order))

            cursor.execute("RELEASE SAVEPOINT routine_sync")
            return was_inserted

        except Exception as e:
            # Rollback just this routine's changes
            cursor.execute("ROLLBACK TO SAVEPOINT routine_sync")
            print(f"✗ Error syncing routine '{routine.get('name', 'unknown')}' (ID: {routine.get('id', 'unknown')}): {e}")
            return None

    def run_sync(self):
        """Main sync process"""
        sync_start = datetime.now()
        routines_added = 0
        routines_updated = 0
        error_message = None

        try:
            if not self.connect_db():
                return False

            cursor = self.conn.cursor()

            # Fetch routines from base44
            print("\nFetching routines from base44...")
            routines = self.fetch_routines_from_base44()

            if routines is None:
                raise Exception("Failed to fetch routines from base44")

            # Sync each routine
            print(f"\nSyncing {len(routines)} routines to database...")
            for i, routine in enumerate(routines, 1):
                result = self.sync_routine(routine, cursor, self.conn)
                if result is True:
                    routines_added += 1
                elif result is False:
                    routines_updated += 1

                if i % 5 == 0:
                    print(f"  Progress: {i}/{len(routines)} routines processed")
                    self.conn.commit()

            self.conn.commit()

            sync_end = datetime.now()
            print(f"\n✓ Routine sync completed successfully!")
            print(f"  - Routines added: {routines_added}")
            print(f"  - Routines updated: {routines_updated}")
            print(f"  - Total routines: {len(routines)}")
            print(f"  - Duration: {(sync_end - sync_start).total_seconds():.2f} seconds")

            return True

        except Exception as e:
            error_message = str(e)
            print(f"\n✗ Routine sync failed: {error_message}")
            return False

        finally:
            if self.conn:
                self.conn.close()

def main():
    print("=== base44 Routines → PostgreSQL Sync ===\n")

    syncer = Base44RoutineSync()
    success = syncer.run_sync()

    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
