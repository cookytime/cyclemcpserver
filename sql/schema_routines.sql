-- Create routines table to store base44 routine/class data
CREATE TABLE IF NOT EXISTS routines (
    id SERIAL PRIMARY KEY,
    base44_id VARCHAR(255) UNIQUE NOT NULL,

    -- Basic routine info
    name VARCHAR(500) NOT NULL,
    description TEXT,
    theme TEXT,
    intensity_arc TEXT,
    resistance_scale_notes TEXT,
    class_summary TEXT,

    -- Duration and difficulty
    total_duration_minutes DECIMAL(10, 2),
    difficulty VARCHAR(50),

    -- Integration
    spotify_playlist_id VARCHAR(255),

    -- Tags stored as JSONB array
    tags JSONB,

    -- Timestamps
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create junction table for routine-track relationship with ordering
CREATE TABLE IF NOT EXISTS routine_tracks (
    id SERIAL PRIMARY KEY,
    routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
    track_base44_id VARCHAR(255) NOT NULL,
    track_order INTEGER NOT NULL,

    -- Foreign key to tracks table (optional, allows NULL if track not synced yet)
    track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,

    UNIQUE(routine_id, track_order),
    UNIQUE(routine_id, track_base44_id)
);

-- Create indexes for routines
CREATE INDEX IF NOT EXISTS idx_routines_base44_id ON routines(base44_id);
CREATE INDEX IF NOT EXISTS idx_routines_difficulty ON routines(difficulty);
CREATE INDEX IF NOT EXISTS idx_routines_duration ON routines(total_duration_minutes);
CREATE INDEX IF NOT EXISTS idx_routines_tags ON routines USING GIN(tags);

-- Create indexes for routine_tracks junction table
CREATE INDEX IF NOT EXISTS idx_routine_tracks_routine_id ON routine_tracks(routine_id);
CREATE INDEX IF NOT EXISTS idx_routine_tracks_track_id ON routine_tracks(track_id);
CREATE INDEX IF NOT EXISTS idx_routine_tracks_track_base44_id ON routine_tracks(track_base44_id);
CREATE INDEX IF NOT EXISTS idx_routine_tracks_order ON routine_tracks(routine_id, track_order);
