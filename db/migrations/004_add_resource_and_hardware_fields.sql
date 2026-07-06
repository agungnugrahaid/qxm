-- Run manually (e.g. via Adminer) if you already initialized the DB
-- before these fields existed.

-- Dynamic resource usage, pushed every ~5 min alongside the rest of
-- router_metrics.
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS cpu_load_pct NUMERIC;
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS ram_used_bytes BIGINT;
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS ram_total_bytes BIGINT;
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS disk_used_bytes BIGINT;
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS disk_total_bytes BIGINT;

-- Hardware identity -- doesn't change over time, so it rides along with
-- the once-a-day firmware push rather than the 5-min metrics push.
ALTER TABLE router_firmware ADD COLUMN IF NOT EXISTS architecture TEXT;
ALTER TABLE router_firmware ADD COLUMN IF NOT EXISTS board_name TEXT;
