-- Create track_feedback table to store base44 track feedback data
CREATE TABLE IF NOT EXISTS track_feedback (
    id SERIAL PRIMARY KEY,
    base44_id VARCHAR(255) UNIQUE NOT NULL,

    -- Track reference
    track_title VARCHAR(500) NOT NULL,
    track_artist VARCHAR(500) NOT NULL,
    spotify_id VARCHAR(255),

    -- Feedback
    rating VARCHAR(10) NOT NULL,
    context VARCHAR(255),

    -- Timestamps
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_track_feedback_base44_id ON track_feedback(base44_id);
CREATE INDEX IF NOT EXISTS idx_track_feedback_rating ON track_feedback(rating);
CREATE INDEX IF NOT EXISTS idx_track_feedback_spotify_id ON track_feedback(spotify_id);
CREATE INDEX IF NOT EXISTS idx_track_feedback_context ON track_feedback(context);
