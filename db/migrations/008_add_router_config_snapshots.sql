-- Run manually (e.g. via Adminer) if you already initialized the DB
-- before this table existed.
--
-- Daily RouterOS config snapshots (from `/export compact`, pushed
-- alongside the existing daily firmware check). Flat 90-day retention --
-- config text compresses well and even at fleet scale this is a small
-- amount of storage, so no tiered daily/weekly/monthly thinning for now.
CREATE TABLE IF NOT EXISTS router_config_snapshots (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    config_text TEXT,
    size_bytes INT
);
SELECT create_hypertable('router_config_snapshots', 'time', if_not_exists => true);
CREATE INDEX IF NOT EXISTS router_config_snapshots_router_time_idx ON router_config_snapshots (router_id, time DESC);

ALTER TABLE router_config_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('router_config_snapshots', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('router_config_snapshots', INTERVAL '90 days', if_not_exists => true);
