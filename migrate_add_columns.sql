-- Migration to add missing columns to tracks table

-- Add focus_area column
ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS focus_area VARCHAR(100);

-- Add position column
ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS position VARCHAR(100);

-- Add resistance range columns
ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS resistance_min DECIMAL(10, 2);

ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS resistance_max DECIMAL(10, 2);

-- Add cadence range columns
ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS cadence_min DECIMAL(10, 2);

ALTER TABLE tracks
ADD COLUMN IF NOT EXISTS cadence_max DECIMAL(10, 2);

-- Update bpm column type if needed
ALTER TABLE tracks
ALTER COLUMN bpm TYPE DECIMAL(10, 2);

-- Add indexes for new columns
CREATE INDEX IF NOT EXISTS idx_tracks_focus_area ON tracks(focus_area);
CREATE INDEX IF NOT EXISTS idx_tracks_position ON tracks(position);

ANALYZE tracks;
