-- Migration to add audience column to track_feedback table
ALTER TABLE track_feedback
ADD COLUMN IF NOT EXISTS audience VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_track_feedback_audience ON track_feedback(audience);

-- Also allow base44_id to be NULL for locally-created feedback
ALTER TABLE track_feedback
ALTER COLUMN base44_id DROP NOT NULL;

-- Drop the unique constraint on base44_id so we can have multiple feedback entries
-- (e.g., same track rated for different audiences/contexts)
ALTER TABLE track_feedback
DROP CONSTRAINT IF EXISTS track_feedback_base44_id_key;

-- Add a unique constraint on the combination instead
CREATE UNIQUE INDEX IF NOT EXISTS idx_track_feedback_unique
ON track_feedback(track_title, rating, COALESCE(context, ''), COALESCE(audience, ''));

ANALYZE track_feedback;
