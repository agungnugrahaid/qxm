-- Connection tracking table utilization -- a busy CGNAT/customer-heavy
-- router can silently start dropping new connections once this fills,
-- which looks like random "sites won't load" complaints rather than an
-- obvious outage. Lives on router_metrics (same cadence/row as CPU/RAM/disk).
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS conntrack_count BIGINT;
ALTER TABLE router_metrics ADD COLUMN IF NOT EXISTS conntrack_max BIGINT;
