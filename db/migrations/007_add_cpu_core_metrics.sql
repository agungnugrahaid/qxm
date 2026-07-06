-- Run manually (e.g. via Adminer) if you already initialized the DB
-- before this table existed.
--
-- Per-core CPU load, one row per core per push -- the existing
-- router_metrics.cpu_load_pct is a single system-wide average, which
-- can look fine even when one core is pegged at 100% (seen in practice
-- on a CCR1009 -- 9-core average was ~15% while two individual cores
-- were maxed out). Mirrors the one-row-per-item pattern used by
-- uplink_metrics/path_metrics/dhcp_pool_metrics.
CREATE TABLE IF NOT EXISTS cpu_core_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    core_name TEXT NOT NULL,
    load_pct NUMERIC
);
SELECT create_hypertable('cpu_core_metrics', 'time', if_not_exists => true);
CREATE INDEX IF NOT EXISTS cpu_core_metrics_router_core_time_idx ON cpu_core_metrics (router_id, core_name, time DESC);

ALTER TABLE cpu_core_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, core_name');
SELECT add_compression_policy('cpu_core_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('cpu_core_metrics', INTERVAL '180 days', if_not_exists => true);
