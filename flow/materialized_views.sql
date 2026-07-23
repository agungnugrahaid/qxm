-- Hourly rollups so flow history survives the 7-day flows_raw TTL and the
-- Monthly Report can span a full period. See FLOW_COLLECTION_PLAN.md.
--
-- Provider labels are resolved at INSERT time via asn_dict/cdn_dict (fast,
-- low-cardinality, scale-ready). Trade-off: a later cdn_override change is NOT
-- retroactive on already-aggregated rows -- re-backfill provider_hourly to
-- relabel history (see the backfill block at the bottom).
--
-- Apply (dictionaries must already exist): pause the collector so backfill
-- doesn't overlap the MV, then:
--   docker compose -f docker-compose.flow.yml stop flow-collector
--   CHP=$(grep ^CH_PASS= .env | cut -d= -f2)
--   docker exec -i qxm-clickhouse clickhouse-client --user flow --password "$CHP" \
--     --database flow --multiquery < flow/materialized_views.sql
--   ... run the backfill INSERTs (commented at the bottom) ...
--   docker compose -f docker-compose.flow.yml start flow-collector

-- ---- target tables (13-month retention) ----
CREATE TABLE IF NOT EXISTS provider_hourly (
  exporter_ip String,
  hour        DateTime,
  provider    String,
  bytes       UInt64,
  packets     UInt64
) ENGINE = SummingMergeTree
ORDER BY (exporter_ip, hour, provider)
TTL hour + INTERVAL 13 MONTH;

CREATE TABLE IF NOT EXISTS user_hourly (
  exporter_ip String,
  hour        DateTime,
  internal_ip String,
  bytes       UInt64,
  packets     UInt64
) ENGINE = SummingMergeTree
ORDER BY (exporter_ip, hour, internal_ip)
TTL hour + INTERVAL 13 MONTH;

CREATE TABLE IF NOT EXISTS traffic_hourly (
  exporter_ip String,
  hour        DateTime,
  download    UInt64,
  upload      UInt64
) ENGINE = SummingMergeTree
ORDER BY (exporter_ip, hour)
TTL hour + INTERVAL 13 MONTH;

-- ---- materialized views (fire on INSERT into flows_raw) ----
CREATE MATERIALIZED VIEW IF NOT EXISTS provider_hourly_mv TO provider_hourly AS
SELECT exporter_ip, toStartOfHour(ts) AS hour,
  multiIf(
    dictGetString('flow.cdn_dict','label', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip))) != '',
    dictGetString('flow.cdn_dict','label', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip))),
    concat(dictGetString('flow.asn_dict','org', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip))),
           ' (AS', toString(dictGetUInt32('flow.asn_dict','asn', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip)))), ')')
  ) AS provider,
  sum(bytes) AS bytes, sum(packets) AS packets
FROM (
  SELECT exporter_ip, ts, bytes, packets, if(dp, src_addr, dst_addr) AS ext_ip
  FROM (
    SELECT exporter_ip, ts, bytes, packets, src_addr, dst_addr,
      (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10')) AS sp,
      (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10')) AS dp
    FROM flows_raw
  ) WHERE sp != dp
)
GROUP BY exporter_ip, hour, provider;

CREATE MATERIALIZED VIEW IF NOT EXISTS user_hourly_mv TO user_hourly AS
SELECT exporter_ip, toStartOfHour(ts) AS hour, if(dp, dst_addr, src_addr) AS internal_ip,
  sum(bytes) AS bytes, sum(packets) AS packets
FROM (
  SELECT exporter_ip, ts, bytes, packets, src_addr, dst_addr,
    (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10')) AS sp,
    (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10')) AS dp
  FROM flows_raw
) WHERE sp != dp
GROUP BY exporter_ip, hour, internal_ip;

CREATE MATERIALIZED VIEW IF NOT EXISTS traffic_hourly_mv TO traffic_hourly AS
SELECT exporter_ip, toStartOfHour(ts) AS hour,
  sumIf(bytes, dp) AS download, sumIf(bytes, sp) AS upload
FROM (
  SELECT exporter_ip, ts, bytes, src_addr, dst_addr,
    (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10')) AS sp,
    (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10')) AS dp
  FROM flows_raw
) WHERE sp != dp
GROUP BY exporter_ip, hour;
