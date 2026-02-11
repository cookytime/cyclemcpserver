#!/usr/bin/env python3
import sys
import json
import requests
import psycopg2
from psycopg2.extras import Json
from datetime import datetime
from config import Config

class Base44Sync:
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

    def fetch_tracks_from_base44(self):
        """Fetch tracks from base44 API"""
        headers = {
            'api_key': self.api_key,
            'Content-Type': 'application/json'
        }

        try:
            # base44 API endpoint for Track entities
            url = f'{self.api_url}/apps/{Config.BASE44_APP_ID}/entities/Track'
            response = requests.get(
                url,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            tracks = response.json()

            print(f"✓ Fetched {len(tracks)} tracks from base44")
            return tracks

        except requests.exceptions.RequestException as e:
            print(f"✗ Failed to fetch tracks from base44: {e}")
            return None

    def sync_track(self, track, cursor, conn):
        """Sync a single track to the database"""
        # Create a savepoint so we can rollback just this track on error
        cursor.execute("SAVEPOINT track_sync")

        try:
            # Extract track data from base44 API response
            base44_id = track.get('id')
            title = track.get('title')
            artist = track.get('artist')
            album = track.get('album')
            duration_minutes = track.get('duration_minutes')

            # Spotify data
            spotify_id = track.get('spotify_id')
            spotify_album_art = track.get('spotify_album_art')
            spotify_url = track.get('spotify_url')

            # Musical characteristics
            bpm = track.get('bpm')
            intensity = track.get('intensity')
            track_type = track.get('track_type')
            focus_area = track.get('focus_area')
            position = track.get('position')

            # Cycling-specific
            base_rpm = track.get('base_rpm')
            base_effortlevel = track.get('base_effortlevel')
            resistance_min = track.get('resistance_min')
            resistance_max = track.get('resistance_max')
            cadence_min = track.get('cadence_min')
            cadence_max = track.get('cadence_max')

            # Choreography data
            choreography = track.get('choreography')
            cues = track.get('cues')
            notes = track.get('notes')

            if not base44_id or not title:
                print(f"⚠ Skipping track with missing required fields: {track}")
                cursor.execute("RELEASE SAVEPOINT track_sync")
                return False

            # Convert lists/dicts to JSON for PostgreSQL JSONB storage
            choreography_json = Json(choreography) if choreography else None
            cues_json = Json(cues) if cues else None

            # Insert or update track
            cursor.execute("""
                INSERT INTO tracks (
                    base44_id, title, artist, album, duration_minutes,
                    spotify_id, spotify_album_art, spotify_url,
                    bpm, intensity, track_type, focus_area, position,
                    base_rpm, base_effortlevel,
                    resistance_min, resistance_max, cadence_min, cadence_max,
                    choreography, cues, notes,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (base44_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    artist = EXCLUDED.artist,
                    album = EXCLUDED.album,
                    duration_minutes = EXCLUDED.duration_minutes,
                    spotify_id = EXCLUDED.spotify_id,
                    spotify_album_art = EXCLUDED.spotify_album_art,
                    spotify_url = EXCLUDED.spotify_url,
                    bpm = EXCLUDED.bpm,
                    intensity = EXCLUDED.intensity,
                    track_type = EXCLUDED.track_type,
                    focus_area = EXCLUDED.focus_area,
                    position = EXCLUDED.position,
                    base_rpm = EXCLUDED.base_rpm,
                    base_effortlevel = EXCLUDED.base_effortlevel,
                    resistance_min = EXCLUDED.resistance_min,
                    resistance_max = EXCLUDED.resistance_max,
                    cadence_min = EXCLUDED.cadence_min,
                    cadence_max = EXCLUDED.cadence_max,
                    choreography = EXCLUDED.choreography,
                    cues = EXCLUDED.cues,
                    notes = EXCLUDED.notes,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING (xmax = 0) AS inserted
            """, (
                base44_id, title, artist, album, duration_minutes,
                spotify_id, spotify_album_art, spotify_url,
                bpm, intensity, track_type, focus_area, position,
                base_rpm, base_effortlevel,
                resistance_min, resistance_max, cadence_min, cadence_max,
                choreography_json, cues_json, notes
            ))

            result = cursor.fetchone()
            cursor.execute("RELEASE SAVEPOINT track_sync")
            return result[0]  # True if inserted, False if updated

        except Exception as e:
            # Rollback just this track's changes
            cursor.execute("ROLLBACK TO SAVEPOINT track_sync")
            print(f"✗ Error syncing track '{track.get('title', 'unknown')}' (ID: {track.get('id', 'unknown')}): {e}")
            return None

    def run_sync(self):
        """Main sync process"""
        sync_start = datetime.now()
        tracks_added = 0
        tracks_updated = 0
        error_message = None

        try:
            if not self.connect_db():
                return False

            cursor = self.conn.cursor()

            # Log sync start
            cursor.execute("""
                INSERT INTO sync_log (sync_started_at, status)
                VALUES (%s, 'running')
                RETURNING id
            """, (sync_start,))
            sync_log_id = cursor.fetchone()[0]
            self.conn.commit()

            # Fetch tracks from base44
            print("\nFetching tracks from base44...")
            tracks = self.fetch_tracks_from_base44()

            if tracks is None:
                raise Exception("Failed to fetch tracks from base44")

            # Sync each track
            print(f"\nSyncing {len(tracks)} tracks to database...")
            for i, track in enumerate(tracks, 1):
                result = self.sync_track(track, cursor, self.conn)
                if result is True:
                    tracks_added += 1
                elif result is False:
                    tracks_updated += 1

                if i % 10 == 0:
                    print(f"  Progress: {i}/{len(tracks)} tracks processed")
                    self.conn.commit()

            self.conn.commit()

            # Update sync log
            sync_end = datetime.now()
            cursor.execute("""
                UPDATE sync_log
                SET sync_completed_at = %s,
                    tracks_added = %s,
                    tracks_updated = %s,
                    tracks_total = %s,
                    status = 'completed'
                WHERE id = %s
            """, (sync_end, tracks_added, tracks_updated, len(tracks), sync_log_id))
            self.conn.commit()

            print(f"\n✓ Sync completed successfully!")
            print(f"  - Tracks added: {tracks_added}")
            print(f"  - Tracks updated: {tracks_updated}")
            print(f"  - Total tracks: {len(tracks)}")
            print(f"  - Duration: {(sync_end - sync_start).total_seconds():.2f} seconds")

            return True

        except Exception as e:
            error_message = str(e)
            print(f"\n✗ Sync failed: {error_message}")

            if self.conn:
                cursor = self.conn.cursor()
                cursor.execute("""
                    UPDATE sync_log
                    SET status = 'failed',
                        error_message = %s
                    WHERE id = %s
                """, (error_message, sync_log_id))
                self.conn.commit()

            return False

        finally:
            if self.conn:
                self.conn.close()

def main():
    print("=== base44 → PostgreSQL Sync ===\n")

    syncer = Base44Sync()
    success = syncer.run_sync()

    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
