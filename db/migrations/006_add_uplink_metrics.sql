-- Run manually (e.g. via Adminer) if you already initialized the DB
-- before this table existed.
--
-- Some routers have a backup/failover uplink in addition to the main
-- WAN -- both need traffic tracked separately, not folded into a single
-- number. One row per configured uplink per push (mirrors how
-- path_metrics/dhcp_pool_metrics already do one row per target/pool),
-- rather than adding fixed main/backup columns -- this doesn't hardcode
-- "at most 2 uplinks" and stays consistent with the rest of the schema.
--
-- router_metrics.rx_bytes/tx_bytes are left as-is (still the main
-- uplink, for backward compatibility with existing history); the
-- Uplink Traffic dashboard panels now read from this table instead.
CREATE TABLE IF NOT EXISTS uplink_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    uplink_label TEXT NOT NULL,      -- 'main' or 'backup'
    interface_name TEXT,
    rx_bytes BIGINT,
    tx_bytes BIGINT
);
SELECT create_hypertable('uplink_metrics', 'time', if_not_exists => true);
CREATE INDEX IF NOT EXISTS uplink_metrics_router_label_time_idx ON uplink_metrics (router_id, uplink_label, time DESC);

ALTER TABLE uplink_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, uplink_label');
SELECT add_compression_policy('uplink_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('uplink_metrics', INTERVAL '180 days', if_not_exists => true);

-- Optional second uplink interface -- NULL means this router has no
-- backup uplink configured.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS wan_interface_backup TEXT;
