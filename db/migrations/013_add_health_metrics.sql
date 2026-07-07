-- System health sensors (temperature, fan speed/state, PSU state,
-- voltage, current, power draw). Flexible key/value shape rather than
-- fixed columns, since RouterOS 6 and 7 expose genuinely different
-- sensor sets (v6: voltage/current/temperature/cpu-temperature/
-- power-consumption/fan1-speed as fixed singleton properties; v7:
-- a variable list of named "gauges" -- per-component temps, fan
-- speeds/state, PSU state -- that varies by hardware). Value is TEXT
-- since gauges mix numeric readings ("41") and status strings ("ok").
CREATE TABLE IF NOT EXISTS health_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    gauge_name TEXT NOT NULL,
    value TEXT,
    unit TEXT
);
SELECT create_hypertable('health_metrics', 'time', if_not_exists => true);
CREATE INDEX IF NOT EXISTS health_metrics_router_id_gauge_name_time_idx
    ON health_metrics (router_id, gauge_name, time DESC);

ALTER TABLE health_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, gauge_name');
SELECT add_compression_policy('health_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('health_metrics', INTERVAL '90 days', if_not_exists => true);
