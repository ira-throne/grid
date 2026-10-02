-- Pipeline health view (the dashboard's status chips).
-- The rest of the dashboard's views are in 09_california_views.sql.
-- Safe to run more than once.

-- ---------------------------------------------------------------
-- Pipeline health: newest data and latest run per dataset.
-- The dashboard header turns this into "ok / late / failing" chips.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW grid.v_pipeline_health AS
SELECT
    w.dataset,
    w.last_period_utc,
    w.last_run_at,
    r.status       AS last_status,
    r.started_at   AS last_started_at,
    ROUND(EXTRACT(EPOCH FROM now() - w.last_run_at) / 60) AS minutes_since_run
FROM grid.etl_watermark w
LEFT JOIN LATERAL (
    SELECT status, started_at
    FROM grid.etl_run_log l
    WHERE l.dataset = w.dataset
    ORDER BY started_at DESC
    LIMIT 1
) r ON TRUE;
