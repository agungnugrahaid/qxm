-- On-net CDN cache override for the "Top Content Providers" panel.
--
-- Why: on-net CDN caches (Google, Meta, Microsoft, Edgenext) are announced from
-- gmedia's AS55666, so the ASN lookup labels their traffic "GMEDIA". This table
-- relabels those specific IP ranges; the panel prefers a cdn_override label over
-- the ASN org. Labels use the *-CDN convention (renamed from *-CACHE 2026-07-27).
-- Only the listed ranges are relabelled -- gmedia's other AS55666 traffic
-- (DNS, services, the QXM server) still shows as GMEDIA.
--
-- The panel reads via the cdn_dict dictionary (range_hashed over this table,
-- LIFETIME 300 = auto-reload every 5 min). After editing, wait 5 min OR force:
--   SYSTEM RELOAD DICTIONARY flow.cdn_dict
--
-- Run these against database `flow` as the flow user:
--   CHP=$(grep ^CH_PASS= .env | cut -d= -f2)
--   docker exec -it qxm-clickhouse clickhouse-client --user flow --password "$CHP" --database flow

-- ============================================================================
-- Dictionary (create once; password injected at apply time, NOT in the repo).
-- Apply: sed "s/__CH_PASS__/$CHP/" flow/cdn_override.sql | docker exec -i ... --multiquery
-- ============================================================================
CREATE DICTIONARY IF NOT EXISTS cdn_dict (
  dummy    UInt8,
  ip_start UInt32,
  ip_end   UInt32,
  label    String
) PRIMARY KEY dummy
SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'flow' PASSWORD '__CH_PASS__' DB 'flow'
  QUERY 'SELECT 0 AS dummy, ip_start, ip_end, label FROM flow.cdn_override'))
LAYOUT(RANGE_HASHED())
RANGE(MIN ip_start MAX ip_end)
LIFETIME(300);

-- ============================================================================
-- CURRENT POPULATED SET (source of truth). cdn_override is TinyLog, so to change
-- a label or range we TRUNCATE and re-insert the whole set. Re-applying this file
-- resets the table to exactly these ranges, then reloads the dict. Labels carry a
-- site suffix: GMEDIA-JOG = Jogja caches, GMEDIA-SMG = Semarang caches.
-- IMPORTANT: cdn_override changes are NOT retroactive on provider_hourly -- after
-- editing, re-backfill provider_hourly (procedure at the bottom of
-- flow/materialized_views.sql) to relabel already-aggregated history.
-- ============================================================================
TRUNCATE TABLE cdn_override;

INSERT INTO cdn_override
SELECT toUInt32(tupleElement(r,1)), toUInt32(tupleElement(r,2)), 'META-CDN (GMEDIA-JOG)'
FROM (SELECT IPv4CIDRToRange(toIPv4('112.78.36.128'), 26) AS r);

INSERT INTO cdn_override
SELECT toUInt32(tupleElement(r,1)), toUInt32(tupleElement(r,2)), 'GOOGLE-CDN (GMEDIA-JOG)'
FROM (SELECT IPv4CIDRToRange(toIPv4('43.245.187.0'), 26) AS r);

INSERT INTO cdn_override
SELECT toUInt32(tupleElement(r,1)), toUInt32(tupleElement(r,2)), 'GOOGLE-CDN (GMEDIA-SMG)'
FROM (SELECT IPv4CIDRToRange(toIPv4('119.2.50.0'), 26) AS r);

-- nginx HTTP/80 cache node, unlabelled until 2026-07-27: it was the single
-- biggest "GMEDIA (AS55666)" talker fleet-wide (13+ GiB/7d, every flow customer;
-- 902 MiB of Prima Inn's first 5 min of flow). Found via the AS55666 breakdown.
INSERT INTO cdn_override
SELECT toUInt32(tupleElement(r,1)), toUInt32(tupleElement(r,2)), 'MICROSOFT-CDN (GMEDIA-JOG)'
FROM (SELECT IPv4CIDRToRange(toIPv4('112.78.33.88'), 30) AS r);

INSERT INTO cdn_override
SELECT toUInt32(tupleElement(r,1)), toUInt32(tupleElement(r,2)), 'EDGENEXT-CDN (GMEDIA-JOG)'
FROM (SELECT IPv4CIDRToRange(toIPv4('119.2.54.80'), 29) AS r);

SYSTEM RELOAD DICTIONARY flow.cdn_dict;

-- View current entries (human-readable):
--   SELECT IPv4NumToString(ip_start) AS start, IPv4NumToString(ip_end) AS end, label FROM cdn_override;
