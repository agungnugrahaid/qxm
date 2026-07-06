-- Compression + retention for the high-volume hypertables. Run this once
-- the pilot's data volume is real (no point compressing empty tables), and
-- definitely before cutting over to full scale — this is what keeps disk
-- usage bounded instead of growing forever.
--
-- Adjust the "3 days" compression age and retention windows to your own
-- judgment; these are reasonable starting points, not hard rules.

ALTER TABLE client_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'site_id');
SELECT add_compression_policy('client_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('client_metrics', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE ap_inventory SET (timescaledb.compress, timescaledb.compress_segmentby = 'site_id');
SELECT add_compression_policy('ap_inventory', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('ap_inventory', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE router_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('router_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('router_metrics', INTERVAL '180 days', if_not_exists => true);

ALTER TABLE path_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('path_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('path_metrics', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE dhcp_pool_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('dhcp_pool_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('dhcp_pool_metrics', INTERVAL '90 days', if_not_exists => true);

-- router_firmware is tiny (one row/router/day) — no compression/retention
-- needed, keeping full history is cheap and useful for fleet audits.
