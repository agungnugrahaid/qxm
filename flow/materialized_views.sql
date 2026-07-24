-- Hourly rollups so flow history survives the 7-day flows_raw TTL and the
-- Monthly Report can span a full period. See FLOW_COLLECTION_PLAN.md.
--
-- Provider labels are resolved at INSERT time via asn_dict/cdn_dict (fast,
-- low-cardinality, scale-ready). Trade-off: a later cdn_override change is NOT
-- retroactive on already-aggregated rows -- re-backfill provider_hourly to
-- relabel history (see the backfill block at the bottom).
--
-- NAT/masquerade handling (sp2/dp2): on a router that masquerades its WAN, the
-- download (reply) direction is recorded with dst = the router's OWN public IP
-- (the exporter_ip), not the pre-NAT client -- so a naive "internal = private
-- client" test scores it as neither up nor down and the download vanishes (seen
-- on Grand Ambarrukmo: 100% of download landed as dst=exporter_ip). We widen
-- "internal side" to (private client OR the exporter's own IP): sp2 = src is
-- internal, dp2 = dst is internal. This recovers download TOTALS (traffic_hourly)
-- and download PROVIDERS (provider_hourly reads the provider off the external
-- src, which is present). It does NOT recover the download's internal CLIENT --
-- that's only in the router's nat-dst-address field, which goflow2 currently
-- drops (see FLOW_COLLECTION_PLAN.md, "NAT client attribution" / plan item).
-- user_hourly therefore stays on the strict private-client test (sp != dp) so a
-- NAT-download is never mis-attributed to the router's own IP as a "top user".
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
  SELECT exporter_ip, ts, bytes, packets, if(dp2, src_addr, dst_addr) AS ext_ip
  FROM (
    SELECT exporter_ip, ts, bytes, packets, src_addr, dst_addr,
      (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10') OR src_addr = exporter_ip) AS sp2,
      (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10') OR dst_addr = exporter_ip) AS dp2
    FROM flows_raw
  ) WHERE sp2 != dp2
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
  sumIf(bytes, dp2) AS download, sumIf(bytes, sp2) AS upload
FROM (
  SELECT exporter_ip, ts, bytes, src_addr, dst_addr,
    (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10') OR src_addr = exporter_ip) AS sp2,
    (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10') OR dst_addr = exporter_ip) AS dp2
  FROM flows_raw
) WHERE sp2 != dp2
GROUP BY exporter_ip, hour;

-- ---- re-apply + backfill (run when the MV logic above changes) ----
-- CREATE ... IF NOT EXISTS won't replace an EXISTING view, so to roll out a
-- logic change you must DROP the affected MVs, recreate them (re-run the file),
-- then TRUNCATE + rebuild their target tables from flows_raw. Pause the
-- collector first so live inserts don't double-count during the rebuild:
--
--   CHP=$(grep ^CH_PASS= .env | cut -d= -f2)
--   docker compose -f docker-compose.flow.yml stop flow-collector
--   CH="docker exec -i qxm-clickhouse clickhouse-client --user flow --password $CHP --database flow"
--   $CH -q "DROP TABLE IF EXISTS provider_hourly_mv; DROP TABLE IF EXISTS traffic_hourly_mv"
--   $CH --multiquery < flow/materialized_views.sql        # recreates the MVs
--   $CH -q "TRUNCATE TABLE provider_hourly"
--   $CH -q "TRUNCATE TABLE traffic_hourly"
--   $CH --multiquery < <the two INSERT...SELECT below>
--   docker compose -f docker-compose.flow.yml start flow-collector
--
-- Backfill provider_hourly (same body as provider_hourly_mv, INSERT into table):
--   INSERT INTO provider_hourly
--   SELECT exporter_ip, toStartOfHour(ts) AS hour,
--     multiIf(
--       dictGetString('flow.cdn_dict','label', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip))) != '',
--       dictGetString('flow.cdn_dict','label', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip))),
--       concat(dictGetString('flow.asn_dict','org', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip))),
--              ' (AS', toString(dictGetUInt32('flow.asn_dict','asn', toUInt8(0), toUInt32(toIPv4OrDefault(ext_ip)))), ')')
--     ) AS provider, sum(bytes) AS bytes, sum(packets) AS packets
--   FROM ( SELECT exporter_ip, ts, bytes, packets, if(dp2, src_addr, dst_addr) AS ext_ip
--     FROM ( SELECT exporter_ip, ts, bytes, packets, src_addr, dst_addr,
--         (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10') OR src_addr = exporter_ip) AS sp2,
--         (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10') OR dst_addr = exporter_ip) AS dp2
--       FROM flows_raw ) WHERE sp2 != dp2 )
--   GROUP BY exporter_ip, hour, provider;
--
-- Backfill traffic_hourly (same body as traffic_hourly_mv):
--   INSERT INTO traffic_hourly
--   SELECT exporter_ip, toStartOfHour(ts) AS hour,
--     sumIf(bytes, dp2) AS download, sumIf(bytes, sp2) AS upload
--   FROM ( SELECT exporter_ip, ts, bytes, src_addr, dst_addr,
--       (isIPAddressInRange(src_addr,'10.0.0.0/8') OR isIPAddressInRange(src_addr,'172.16.0.0/12') OR isIPAddressInRange(src_addr,'192.168.0.0/16') OR isIPAddressInRange(src_addr,'100.64.0.0/10') OR src_addr = exporter_ip) AS sp2,
--       (isIPAddressInRange(dst_addr,'10.0.0.0/8') OR isIPAddressInRange(dst_addr,'172.16.0.0/12') OR isIPAddressInRange(dst_addr,'192.168.0.0/16') OR isIPAddressInRange(dst_addr,'100.64.0.0/10') OR dst_addr = exporter_ip) AS dp2
--     FROM flows_raw ) WHERE sp2 != dp2
--   GROUP BY exporter_ip, hour;
