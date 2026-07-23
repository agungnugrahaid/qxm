-- QXM flow storage (Phase 0/1). See FLOW_COLLECTION_PLAN.md.
-- Run against database `flow`.

CREATE TABLE IF NOT EXISTS flows_raw (
  ts            DateTime,
  exporter_ip   String,              -- sampler_address = NAT egress IP (attribution key)
  src_addr      String,
  dst_addr      String,
  src_port      UInt16,
  dst_port      UInt16,
  proto         LowCardinality(String),
  bytes         UInt64,
  packets       UInt64,
  in_if         UInt32,
  out_if        UInt32,
  sampling_rate UInt32               -- 0 = unsampled; else multiply bytes/packets
) ENGINE = MergeTree
ORDER BY (exporter_ip, ts)
TTL ts + INTERVAL 7 DAY;             -- raw is a short buffer; reports run off aggregates

-- exporter (NAT) IP -> customer. Seeded manually for the canary; later synced
-- from Postgres. Keyed on the flow export IP, NOT routers.mgmt_host.
CREATE TABLE IF NOT EXISTS exporter_map (
  exporter_ip   String,
  customer_id   UInt32,
  customer_name String
) ENGINE = TinyLog;

-- Free iptoasn.com IPv4 range -> ASN + org. Refreshed periodically.
CREATE TABLE IF NOT EXISTS asn (
  ip_start UInt32,
  ip_end   UInt32,
  asn      UInt32,
  country  String,
  org      String
) ENGINE = MergeTree ORDER BY ip_start;

-- Curated override for on-net CDN caches: Google/Meta caches announced from
-- gmedia's AS55666 resolve to "GMEDIA" via the asn table, hiding the real
-- provider. Populate with exact cache ranges + labels; queries prefer this.
CREATE TABLE IF NOT EXISTS cdn_override (
  ip_start UInt32,
  ip_end   UInt32,
  label    String
) ENGINE = TinyLog;
