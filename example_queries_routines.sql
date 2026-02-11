-- Example SQL queries for working with routines (classes/workouts)

-- ============================================
-- Basic Routine Queries
-- ============================================

-- Get all routines with basic info
SELECT id, name, difficulty, total_duration_minutes, theme
FROM routines
ORDER BY name;

-- Find routines by difficulty level
SELECT name, total_duration_minutes, description
FROM routines
WHERE difficulty = 'intermediate'
ORDER BY total_duration_minutes;

-- Find routines by duration range
SELECT name, difficulty, total_duration_minutes
FROM routines
WHERE total_duration_minutes BETWEEN 30 AND 45
ORDER BY total_duration_minutes;

-- ============================================
-- Routine with Tracks
-- ============================================

-- Get a specific routine with all its tracks in order
SELECT
    r.name as routine_name,
    r.difficulty,
    r.total_duration_minutes as routine_duration,
    rt.track_order,
    t.title as track_title,
    t.artist,
    t.duration_minutes,
    t.bpm,
    t.intensity,
    t.track_type
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
LEFT JOIN tracks t ON rt.track_id = t.id
WHERE r.name = 'Your Routine Name Here'
ORDER BY rt.track_order;

-- Get all routines with track count
SELECT
    r.name,
    r.difficulty,
    r.total_duration_minutes,
    COUNT(rt.id) as track_count
FROM routines r
LEFT JOIN routine_tracks rt ON r.id = rt.routine_id
GROUP BY r.id, r.name, r.difficulty, r.total_duration_minutes
ORDER BY r.name;

-- Find routines that use a specific track
SELECT DISTINCT
    r.name as routine_name,
    r.difficulty,
    t.title as track_title,
    t.artist,
    rt.track_order as position_in_routine
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
JOIN tracks t ON rt.track_id = t.id
WHERE t.title = 'Your Track Name'
ORDER BY r.name, rt.track_order;

-- ============================================
-- Routine Analysis
-- ============================================

-- Get routine with average BPM and intensity breakdown
SELECT
    r.name,
    r.difficulty,
    r.total_duration_minutes,
    ROUND(AVG(t.bpm)::numeric, 1) as avg_bpm,
    COUNT(CASE WHEN t.intensity = 'high' THEN 1 END) as high_intensity_tracks,
    COUNT(CASE WHEN t.intensity = 'medium' THEN 1 END) as medium_intensity_tracks,
    COUNT(CASE WHEN t.intensity = 'low' THEN 1 END) as low_intensity_tracks
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
LEFT JOIN tracks t ON rt.track_id = t.id
GROUP BY r.id, r.name, r.difficulty, r.total_duration_minutes
ORDER BY r.name;

-- Show routine structure with track types
SELECT
    r.name as routine,
    rt.track_order,
    t.track_type,
    t.title,
    t.duration_minutes,
    t.intensity
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
LEFT JOIN tracks t ON rt.track_id = t.id
WHERE r.id = 1  -- Replace with your routine ID
ORDER BY rt.track_order;

-- ============================================
-- Finding Routines by Tags
-- ============================================

-- Find routines with specific tags
SELECT name, difficulty, total_duration_minutes, tags
FROM routines
WHERE tags @> '["your-tag-here"]'::jsonb
ORDER BY name;

-- List all unique tags across routines
SELECT DISTINCT jsonb_array_elements_text(tags) as tag
FROM routines
WHERE tags IS NOT NULL
ORDER BY tag;

-- Count routines by tag
SELECT
    jsonb_array_elements_text(tags) as tag,
    COUNT(*) as routine_count
FROM routines
WHERE tags IS NOT NULL
GROUP BY tag
ORDER BY routine_count DESC;

-- ============================================
-- Routine Building and Planning
-- ============================================

-- Find similar routines based on duration and difficulty
SELECT
    r1.name as original_routine,
    r2.name as similar_routine,
    r2.difficulty,
    r2.total_duration_minutes,
    ABS(r1.total_duration_minutes - r2.total_duration_minutes) as duration_diff
FROM routines r1
CROSS JOIN routines r2
WHERE r1.id != r2.id
  AND r1.name = 'Your Routine Name'
  AND r2.difficulty = r1.difficulty
  AND ABS(r1.total_duration_minutes - r2.total_duration_minutes) < 5
ORDER BY duration_diff
LIMIT 5;

-- Get routines by theme keywords
SELECT name, theme, difficulty, total_duration_minutes
FROM routines
WHERE theme ILIKE '%confidence%'
   OR theme ILIKE '%energy%'
ORDER BY total_duration_minutes;

-- ============================================
-- Quality Checks
-- ============================================

-- Find routines with missing track data
SELECT
    r.name as routine_name,
    rt.track_base44_id,
    rt.track_order,
    CASE WHEN rt.track_id IS NULL THEN 'Missing' ELSE 'OK' END as track_status
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
WHERE rt.track_id IS NULL
ORDER BY r.name, rt.track_order;

-- Count routines with complete track data
SELECT
    COUNT(DISTINCT r.id) as total_routines,
    COUNT(DISTINCT CASE WHEN rt.track_id IS NULL THEN r.id END) as routines_with_missing_tracks,
    COUNT(DISTINCT CASE WHEN rt.track_id IS NOT NULL THEN r.id END) as routines_complete
FROM routines r
LEFT JOIN routine_tracks rt ON r.id = rt.routine_id;

-- ============================================
-- Statistics
-- ============================================

-- Routine statistics by difficulty
SELECT
    difficulty,
    COUNT(*) as count,
    ROUND(AVG(total_duration_minutes)::numeric, 1) as avg_duration,
    MIN(total_duration_minutes) as min_duration,
    MAX(total_duration_minutes) as max_duration
FROM routines
WHERE difficulty IS NOT NULL
GROUP BY difficulty
ORDER BY
    CASE difficulty
        WHEN 'beginner' THEN 1
        WHEN 'intermediate' THEN 2
        WHEN 'advanced' THEN 3
        WHEN 'expert' THEN 4
    END;

-- Most used tracks across all routines
SELECT
    t.title,
    t.artist,
    t.bpm,
    t.intensity,
    COUNT(rt.id) as times_used
FROM tracks t
JOIN routine_tracks rt ON t.id = rt.track_id
GROUP BY t.id, t.title, t.artist, t.bpm, t.intensity
ORDER BY times_used DESC
LIMIT 10;

-- Tracks never used in any routine
SELECT
    t.title,
    t.artist,
    t.bpm,
    t.track_type,
    t.intensity
FROM tracks t
LEFT JOIN routine_tracks rt ON t.id = rt.track_id
WHERE rt.id IS NULL
ORDER BY t.title;

-- ============================================
-- Combined Routine and Track Search
-- ============================================

-- Find routines with high-intensity climbs
SELECT DISTINCT
    r.name,
    r.difficulty,
    r.total_duration_minutes,
    COUNT(t.id) as climb_track_count
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
JOIN tracks t ON rt.track_id = t.id
WHERE t.track_type ILIKE '%climb%'
  AND t.intensity IN ('high', 'extreme')
GROUP BY r.id, r.name, r.difficulty, r.total_duration_minutes
HAVING COUNT(t.id) >= 2
ORDER BY climb_track_count DESC;

-- Export routine as ordered playlist
SELECT
    rt.track_order,
    t.title,
    t.artist,
    t.spotify_url,
    t.duration_minutes,
    t.bpm
FROM routines r
JOIN routine_tracks rt ON r.id = rt.routine_id
JOIN tracks t ON rt.track_id = t.id
WHERE r.name = 'Your Routine Name'
ORDER BY rt.track_order;
