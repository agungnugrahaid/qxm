-- Jitter (mean absolute difference between consecutive RTT samples within
-- one ping burst) alongside the existing rtt/loss columns in path_metrics.
-- Only ever populated on v7 routers -- same limitation as the rest of
-- structured ping data (see routeros/README.md: `/ping ... as-value`
-- doesn't parse at all on RouterOS 6.49.8).
ALTER TABLE path_metrics ADD COLUMN IF NOT EXISTS jitter_ms NUMERIC;
