-- Migration to add canonical rpm column to tracks

ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS rpm DECIMAL(10, 2);

-- Backfill rpm from existing cadence fields (legacy schema)
UPDATE tracks
SET rpm = COALESCE(rpm, cadence_min, cadence_max, base_rpm)
WHERE rpm IS NULL;

-- Optional index for rpm-based filtering/sorting
CREATE INDEX IF NOT EXISTS idx_tracks_rpm ON tracks(rpm);

ANALYZE tracks;
