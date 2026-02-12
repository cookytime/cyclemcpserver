#!/usr/bin/env python3
import sys
import requests
import psycopg2
from datetime import datetime
from config import Config

class Base44TrackFeedbackSync:
    def __init__(self):
        Config.validate()
        self.api_key = Config.BASE44_API_KEY
        self.api_url = Config.BASE44_API_URL
        self.conn = None

    def connect_db(self):
        try:
            self.conn = psycopg2.connect(Config.get_db_connection_string())
            print("✓ Connected to PostgreSQL database")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to database: {e}")
            return False

    def fetch_feedback_from_base44(self):
        headers = {
            'api_key': self.api_key,
            'Content-Type': 'application/json'
        }

        try:
            url = f'{self.api_url}/apps/{Config.BASE44_APP_ID}/entities/TrackFeedback'
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            feedback = response.json()

            print(f"✓ Fetched {len(feedback)} track feedback entries from base44")
            return feedback

        except requests.exceptions.RequestException as e:
            print(f"✗ Failed to fetch track feedback from base44: {e}")
            return None

    def sync_feedback(self, entry, cursor):
        cursor.execute("SAVEPOINT feedback_sync")

        try:
            base44_id = entry.get('id')
            track_title = entry.get('track_title') or None
            track_artist = entry.get('track_artist') or None
            spotify_id = entry.get('spotify_id') or None
            rating = entry.get('rating') or None
            context = entry.get('context') or None
            audience = entry.get('audience') or None

            if not base44_id or not track_title or not rating:
                print(f"⚠ Skipping feedback with missing required fields: {entry}")
                cursor.execute("RELEASE SAVEPOINT feedback_sync")
                return False

            # `base44_id` may or may not be uniquely constrained depending on migrations.
            # Use update-then-insert to avoid relying on ON CONFLICT(base44_id).
            cursor.execute("""
                UPDATE track_feedback
                SET track_title = %s,
                    track_artist = %s,
                    spotify_id = %s,
                    rating = %s,
                    context = %s,
                    audience = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE base44_id = %s
            """, (track_title, track_artist, spotify_id, rating, context, audience, base44_id))

            if cursor.rowcount > 0:
                cursor.execute("RELEASE SAVEPOINT feedback_sync")
                return False

            cursor.execute("""
                INSERT INTO track_feedback (
                    base44_id, track_title, track_artist, spotify_id,
                    rating, context, audience, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """, (base44_id, track_title, track_artist, spotify_id, rating, context, audience))

            cursor.execute("RELEASE SAVEPOINT feedback_sync")
            return True

        except Exception as e:
            cursor.execute("ROLLBACK TO SAVEPOINT feedback_sync")
            print(f"✗ Error syncing feedback for '{entry.get('track_title', 'unknown')}' (ID: {entry.get('id', 'unknown')}): {e}")
            return None

    def run_sync(self):
        sync_start = datetime.now()
        added = 0
        updated = 0

        try:
            if not self.connect_db():
                return False

            cursor = self.conn.cursor()

            print("\nFetching track feedback from base44...")
            feedback = self.fetch_feedback_from_base44()

            if feedback is None:
                raise Exception("Failed to fetch track feedback from base44")

            print(f"\nSyncing {len(feedback)} feedback entries to database...")
            for i, entry in enumerate(feedback, 1):
                result = self.sync_feedback(entry, cursor)
                if result is True:
                    added += 1
                elif result is False:
                    updated += 1

                if i % 10 == 0:
                    print(f"  Progress: {i}/{len(feedback)} entries processed")
                    self.conn.commit()

            self.conn.commit()

            sync_end = datetime.now()
            print(f"\n✓ Track feedback sync completed successfully!")
            print(f"  - Feedback added: {added}")
            print(f"  - Feedback updated: {updated}")
            print(f"  - Total entries: {len(feedback)}")
            print(f"  - Duration: {(sync_end - sync_start).total_seconds():.2f} seconds")

            return True

        except Exception as e:
            print(f"\n✗ Track feedback sync failed: {e}")
            return False

        finally:
            if self.conn:
                self.conn.close()

def main():
    print("=== base44 Track Feedback → PostgreSQL Sync ===\n")

    syncer = Base44TrackFeedbackSync()
    success = syncer.run_sync()

    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
