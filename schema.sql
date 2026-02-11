-- Create tracks table to store base44 track data
CREATE TABLE IF NOT EXISTS tracks (
    id SERIAL PRIMARY KEY,
    base44_id VARCHAR(255) UNIQUE NOT NULL,

    -- Basic track info
    title VARCHAR(500) NOT NULL,
    artist VARCHAR(500),
    album VARCHAR(500),
    duration_minutes DECIMAL(10, 2),

    -- Spotify integration
    spotify_id VARCHAR(255),
    spotify_album_art TEXT,
    spotify_url TEXT,

    -- Musical characteristics (useful for choreography)
    bpm DECIMAL(10, 2),
    intensity VARCHAR(50),
    track_type VARCHAR(100),
    focus_area VARCHAR(100),
    position VARCHAR(100),

    -- Cycling-specific fields
    base_rpm INTEGER,
    base_effortlevel INTEGER,
    resistance_min DECIMAL(10, 2),
    resistance_max DECIMAL(10, 2),
    cadence_min DECIMAL(10, 2),
    cadence_max DECIMAL(10, 2),

    -- Choreography data (structured as JSON for rich querying)
    choreography JSONB,
    cues JSONB,
    notes TEXT,

    -- Timestamps
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_tracks_base44_id ON tracks(base44_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_bpm ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_intensity ON tracks(intensity);
CREATE INDEX IF NOT EXISTS idx_tracks_track_type ON tracks(track_type);

-- Create sync_log table to track sync operations
CREATE TABLE IF NOT EXISTS sync_log (
    id SERIAL PRIMARY KEY,
    sync_started_at TIMESTAMP NOT NULL,
    sync_completed_at TIMESTAMP,
    tracks_added INTEGER DEFAULT 0,
    tracks_updated INTEGER DEFAULT 0,
    tracks_total INTEGER DEFAULT 0,
    status VARCHAR(50),
    error_message TEXT
);
