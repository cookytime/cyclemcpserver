-- Example SQL queries for working with your choreography database

-- ============================================
-- Basic Track Queries
-- ============================================

-- Get all tracks with their basic info
SELECT id, title, artist, bpm, intensity, track_type, duration_minutes
FROM tracks
ORDER BY title;

-- Find high-intensity tracks for intense workouts
SELECT title, artist, bpm, track_type
FROM tracks
WHERE intensity = 'high'
ORDER BY bpm DESC;

-- Find tracks by BPM range (good for specific workout intensities)
SELECT title, artist, bpm, duration_minutes
FROM tracks
WHERE bpm BETWEEN 120 AND 140
ORDER BY bpm;

-- ============================================
-- Track Type Queries
-- ============================================

-- Get all warmup tracks
SELECT title, artist, duration_minutes, intensity
FROM tracks
WHERE track_type = 'warmup'
ORDER BY duration_minutes;

-- Find sprint tracks with high BPM
SELECT title, artist, bpm, base_rpm, base_effortlevel
FROM tracks
WHERE track_type = 'sprint' AND bpm > 140
ORDER BY bpm DESC;

-- ============================================
-- Choreography Queries (JSONB)
-- ============================================

-- Get tracks with choreography data
SELECT title, artist, choreography
FROM tracks
WHERE choreography IS NOT NULL;

-- Count choreography cues per track
SELECT
    title,
    artist,
    jsonb_array_length(choreography) as cue_count
FROM tracks
WHERE choreography IS NOT NULL
ORDER BY cue_count DESC;

-- Find tracks with specific positions in choreography
SELECT DISTINCT title, artist, track_type
FROM tracks, jsonb_array_elements(choreography) as cue
WHERE cue->>'position' = 'Standing Climb';

-- Get all choreography cues for a specific track
SELECT
    title,
    cue->>'timestamp' as time,
    cue->>'position' as position,
    cue->>'resistance' as resistance,
    cue->>'rpmMin' as rpm_min,
    cue->>'rpmMax' as rpm_max,
    cue->>'note' as note
FROM tracks, jsonb_array_elements(choreography) as cue
WHERE title = 'Your Track Name Here'
ORDER BY cue->>'timestamp';

-- Find tracks with climbs (resistance level 5 or higher)
SELECT DISTINCT title, artist, bpm
FROM tracks, jsonb_array_elements(choreography) as cue
WHERE (cue->>'resistance') LIKE '%5)%'
   OR (cue->>'resistance') LIKE '%6)%'
   OR (cue->>'resistance') LIKE '%7)%'
   OR (cue->>'resistance') LIKE '%8)%';

-- ============================================
-- Coaching Cues Queries
-- ============================================

-- Get tracks with coaching cues
SELECT title, artist, cues
FROM tracks
WHERE cues IS NOT NULL AND jsonb_array_length(cues) > 0;

-- Count coaching cues per track
SELECT
    title,
    artist,
    jsonb_array_length(cues) as cue_count
FROM tracks
WHERE cues IS NOT NULL
ORDER BY cue_count DESC;

-- Search for specific phrases in cues
SELECT title, artist, cues
FROM tracks, jsonb_array_elements_text(cues) as cue
WHERE cue ILIKE '%push%' OR cue ILIKE '%breathe%'
GROUP BY title, artist, cues;

-- ============================================
-- Workout Planning Queries
-- ============================================

-- Build a 45-minute workout plan
WITH workout_parts AS (
    SELECT 'warmup' as phase, 5 as target_duration
    UNION ALL SELECT 'endurance', 15
    UNION ALL SELECT 'intervals', 15
    UNION ALL SELECT 'climb', 5
    UNION ALL SELECT 'cooldown', 5
)
SELECT
    wp.phase,
    wp.target_duration as target_mins,
    t.title,
    t.artist,
    t.duration_minutes,
    t.bpm,
    t.intensity
FROM workout_parts wp
LEFT JOIN LATERAL (
    SELECT * FROM tracks
    WHERE track_type = wp.phase
    ORDER BY RANDOM()
    LIMIT 1
) t ON true
ORDER BY
    CASE wp.phase
        WHEN 'warmup' THEN 1
        WHEN 'endurance' THEN 2
        WHEN 'intervals' THEN 3
        WHEN 'climb' THEN 4
        WHEN 'cooldown' THEN 5
    END;

-- Find tracks with similar BPM for smooth transitions
SELECT
    t1.title as current_track,
    t1.bpm as current_bpm,
    t2.title as next_track,
    t2.bpm as next_bpm,
    ABS(t1.bpm - t2.bpm) as bpm_difference
FROM tracks t1
CROSS JOIN tracks t2
WHERE t1.id != t2.id
  AND ABS(t1.bpm - t2.bpm) < 10
  AND t1.title = 'Your Current Track'
ORDER BY bpm_difference
LIMIT 10;

-- ============================================
-- Statistics and Analytics
-- ============================================

-- Track statistics by type
SELECT
    track_type,
    COUNT(*) as count,
    ROUND(AVG(bpm)::numeric, 2) as avg_bpm,
    ROUND(AVG(duration_minutes)::numeric, 2) as avg_duration,
    ROUND(MIN(duration_minutes)::numeric, 2) as min_duration,
    ROUND(MAX(duration_minutes)::numeric, 2) as max_duration
FROM tracks
WHERE track_type IS NOT NULL
GROUP BY track_type
ORDER BY count DESC;

-- BPM distribution
SELECT
    CASE
        WHEN bpm < 100 THEN 'Slow (< 100)'
        WHEN bpm BETWEEN 100 AND 120 THEN 'Moderate (100-120)'
        WHEN bpm BETWEEN 121 AND 140 THEN 'Upbeat (121-140)'
        WHEN bpm > 140 THEN 'Fast (> 140)'
    END as bpm_range,
    COUNT(*) as count
FROM tracks
WHERE bpm IS NOT NULL
GROUP BY bpm_range
ORDER BY MIN(bpm);

-- Tracks missing choreography data
SELECT
    COUNT(*) as total_tracks,
    SUM(CASE WHEN choreography IS NULL THEN 1 ELSE 0 END) as missing_choreography,
    SUM(CASE WHEN cues IS NULL THEN 1 ELSE 0 END) as missing_cues,
    SUM(CASE WHEN bpm IS NULL THEN 1 ELSE 0 END) as missing_bpm
FROM tracks;

-- Recently synced tracks
SELECT title, artist, track_type, updated_at
FROM tracks
ORDER BY updated_at DESC
LIMIT 10;

-- ============================================
-- Sync Log Queries
-- ============================================

-- View sync history
SELECT
    sync_started_at,
    sync_completed_at,
    tracks_added,
    tracks_updated,
    tracks_total,
    status,
    EXTRACT(EPOCH FROM (sync_completed_at - sync_started_at)) as duration_seconds
FROM sync_log
ORDER BY sync_started_at DESC;

-- Last successful sync
SELECT *
FROM sync_log
WHERE status = 'completed'
ORDER BY sync_completed_at DESC
LIMIT 1;
