-- Physical interface (ether/SFP) health: link state + error/drop counters,
-- one row per physical port per push. Lets QoE alerting catch cabling/SFP
-- degradation and duplex mismatches that ping-based path_metrics can't see
-- (a link can pass a 5-packet ping test fine while still shedding frames
-- under real load).
CREATE TABLE IF NOT EXISTS interface_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    interface_name TEXT NOT NULL,
    running BOOLEAN,
    disabled BOOLEAN,
    rx_fcs_error BIGINT,
    rx_too_short BIGINT,
    rx_too_long BIGINT,
    rx_overflow BIGINT,
    tx_collision BIGINT,
    tx_late_collision BIGINT,
    tx_underrun BIGINT
);
SELECT create_hypertable('interface_metrics', 'time', if_not_exists => true);
CREATE INDEX IF NOT EXISTS interface_metrics_router_id_interface_name_time_idx
    ON interface_metrics (router_id, interface_name, time DESC);

ALTER TABLE interface_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, interface_name');
SELECT add_compression_policy('interface_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('interface_metrics', INTERVAL '90 days', if_not_exists => true);
