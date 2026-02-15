-- Normalize track position labels to current naming in Base44 schema

UPDATE tracks
SET position = CASE
    WHEN LOWER(BTRIM(position)) = 'base' THEN 'Base'
    WHEN LOWER(BTRIM(position)) = 'recover' THEN 'Recover'
    WHEN LOWER(BTRIM(position)) = 'ride easy' THEN 'Recover'
    WHEN LOWER(BTRIM(position)) = 'seated' THEN 'Seated'
    WHEN LOWER(BTRIM(position)) = 'seated climb' THEN 'Seated'
    WHEN LOWER(BTRIM(position)) = 'attack' THEN 'Attack'
    WHEN LOWER(BTRIM(position)) = 'racing climb' THEN 'Attack'
    WHEN LOWER(BTRIM(position)) = 'stand' THEN 'Stand'
    WHEN LOWER(BTRIM(position)) = 'standing climb' THEN 'Stand'
    WHEN LOWER(BTRIM(position)) = 'sprint' THEN 'Sprint'
    WHEN LOWER(BTRIM(position)) = 'hover' THEN 'Hover'
    WHEN LOWER(BTRIM(position)) = 'racing' THEN 'Sprint'
    ELSE position
END
WHERE position IS NOT NULL;

-- Normalize choreography[*].position values in existing JSONB cues
UPDATE tracks
SET choreography = COALESCE(
    (
        SELECT jsonb_agg(
            CASE
                WHEN jsonb_typeof(cue) = 'object' AND cue ? 'position' THEN
                    jsonb_set(
                        cue,
                        '{position}',
                        to_jsonb(
                            CASE
                                WHEN LOWER(BTRIM(cue->>'position')) = 'base'
                                    THEN 'Base'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'recover'
                                    THEN 'Recover'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'ride easy'
                                    THEN 'Recover'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'seated'
                                    THEN 'Seated'
                                WHEN LOWER(BTRIM(cue->>'position'))
                                     = 'seated climb'
                                    THEN 'Seated'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'attack'
                                    THEN 'Attack'
                                WHEN LOWER(BTRIM(cue->>'position'))
                                     = 'racing climb'
                                    THEN 'Attack'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'stand'
                                    THEN 'Stand'
                                WHEN LOWER(BTRIM(cue->>'position'))
                                     = 'standing climb'
                                    THEN 'Stand'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'sprint'
                                    THEN 'Sprint'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'hover'
                                    THEN 'Hover'
                                WHEN LOWER(BTRIM(cue->>'position')) = 'racing'
                                    THEN 'Sprint'
                                ELSE cue->>'position'
                            END
                        ),
                        true
                    )
                ELSE cue
            END
            ORDER BY ord
        )
        FROM jsonb_array_elements(choreography) WITH ORDINALITY AS t(cue, ord)
    ),
    '[]'::jsonb
)
WHERE choreography IS NOT NULL
  AND jsonb_typeof(choreography) = 'array';

ANALYZE tracks;
