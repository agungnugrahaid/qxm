-- 5-minute continuous aggregates that back 3 of the monthly PDF report
-- collectors (reporting/report_lib.py collect_path / collect_resource /
-- collect_wifi_quality). collect_clients and collect_traffic stay on raw -- see
-- the notes below. The report is fully native (matplotlib, no Grafana render
-- since dbf9cc8) but was still scanning 30 days of raw high-frequency metrics
-- per run (~12-18s); reading these rollups instead takes it to ~4-9s. Speed
-- only -- raw retention (90-180d) already covers the window.
--
-- Core-only aggregates: the image is timescale/timescaledb (no timescaledb_toolkit),
-- so no hyperloglog / counter_agg. NOTE: uplink traffic (panel 10) is NOT a cagg
-- either -- uplink_metrics is polled ~per 5-min bucket, so a first()/last() cagg
-- sees first==last (zero delta) in most buckets; the per-poll LAG rate stays on
-- raw (already ~0.2s, exact). NOTE: panel 6's distinct client count is NOT a
-- cagg here -- count(DISTINCT client_mac) across a customer's sites can't be
-- pre-aggregated per-site without double-counting cross-site MACs (overcounts
-- busy buckets up to ~2x), and core has no distinct rollup; report_lib
-- collect_clients keeps that one on raw (exact, matches the dashboard).
--
-- NOTE: continuous aggregates CANNOT be created inside a transaction block, so
-- this file must be run statement-by-statement (plain psql autocommit -- no
-- BEGIN/COMMIT). Apply: docker exec -i qxm-timescaledb-1 psql -U qoe -d qoe < db/migrations/027_report_caggs.sql
-- Backfill after apply (each is a full-history scan of its source hypertable):
--   CALL refresh_continuous_aggregate('path_metrics_5m',    NULL, NULL);
--   CALL refresh_continuous_aggregate('router_metrics_5m',  NULL, NULL);
--   CALL refresh_continuous_aggregate('wifi_quality_5m',    NULL, NULL);

-- Panel 105: path latency / jitter / loss, per router+target.
CREATE MATERIALIZED VIEW IF NOT EXISTS path_metrics_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket('5 minutes', time) AS bucket,
       router_id, target_name,
       avg(rtt_avg_ms)     AS latency,
       avg(jitter_ms)      AS jitter,
       avg(packet_loss_pct) AS loss
FROM path_metrics
GROUP BY bucket, router_id, target_name
WITH NO DATA;

-- Panel 9: router CPU / RAM / Disk. RAM%/Disk% ratios are computed in the
-- collector from these component averages (a ratio of averages re-aggregates
-- correctly; an average of ratios would not).
CREATE MATERIALIZED VIEW IF NOT EXISTS router_metrics_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket('5 minutes', time) AS bucket,
       router_id,
       avg(cpu_load_pct)     AS cpu,
       avg(ram_used_bytes)   AS ram_used,
       avg(ram_total_bytes)  AS ram_total,
       avg(disk_used_bytes)  AS disk_used,
       avg(disk_total_bytes) AS disk_total
FROM router_metrics
GROUP BY bucket, router_id
WITH NO DATA;

-- Panel 7: Wi-Fi quality per site. Store sums+counts so the customer-level
-- averages are correctly weighted across sites in the collector (satisfaction
-- keeps UniFi's -1 "unknown" here, matching the current panel-7 behaviour).
CREATE MATERIALIZED VIEW IF NOT EXISTS wifi_quality_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket('5 minutes', time) AS bucket,
       site_id,
       sum(signal)           AS sig_sum,
       count(signal)         AS sig_cnt,
       sum(satisfaction)     AS sat_sum,
       count(satisfaction)   AS sat_cnt,
       sum(tx_retries)       AS retries,
       sum(wifi_tx_attempts) AS attempts
FROM client_metrics
GROUP BY bucket, site_id
WITH NO DATA;

-- Refresh policies. Real-time aggregation stays ON (default) so a report ending
-- "now" still sees the last few minutes from raw. end_offset 10m keeps the
-- still-filling bucket out of the materialized set; start_offset 3d bounds each
-- incremental refresh.
SELECT add_continuous_aggregate_policy('path_metrics_5m',    start_offset => INTERVAL '3 days', end_offset => INTERVAL '10 minutes', schedule_interval => INTERVAL '30 minutes');
SELECT add_continuous_aggregate_policy('router_metrics_5m',  start_offset => INTERVAL '3 days', end_offset => INTERVAL '10 minutes', schedule_interval => INTERVAL '30 minutes');
SELECT add_continuous_aggregate_policy('wifi_quality_5m',    start_offset => INTERVAL '3 days', end_offset => INTERVAL '10 minutes', schedule_interval => INTERVAL '30 minutes');

-- Keep the rollups 13 months so they outlive raw (90/180d) -- enables longer
-- report windows later without touching raw retention.
SELECT add_retention_policy('path_metrics_5m',    INTERVAL '13 months');
SELECT add_retention_policy('router_metrics_5m',  INTERVAL '13 months');
SELECT add_retention_policy('wifi_quality_5m',    INTERVAL '13 months');
