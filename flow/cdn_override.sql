-- On-net CDN cache override for the "Top Content Providers" panel.
--
-- Why: Google/Meta caches hosted on-net are announced from gmedia's AS55666,
-- so the ASN lookup labels their traffic "GMEDIA". This table relabels those
-- specific IP ranges; the panel prefers a cdn_override label over the ASN org.
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
-- POPULATE / CHANGE (cdn_override is TinyLog: INSERT appends; to change or
-- remove an entry, TRUNCATE and re-insert the whole set).
-- ============================================================================

-- Add a range from CIDR (recommended -- computes start/end for you):
--   INSERT INTO cdn_override
--   SELECT toUInt32(tupleElement(r,1)), toUInt32(tupleElement(r,2)), 'Google Cache (on-net)'
--   FROM (SELECT IPv4CIDRToRange(toIPv4('112.78.36.0'), 24) AS r);

-- Add a range from explicit first/last IP:
--   INSERT INTO cdn_override VALUES
--     (toUInt32(toIPv4('43.245.187.0')), toUInt32(toIPv4('43.245.187.255')), 'Meta Cache (on-net)');

-- Replace the whole set (the way to edit/remove entries):
--   TRUNCATE TABLE cdn_override;
--   ... re-run the INSERTs you want ...
--   SYSTEM RELOAD DICTIONARY flow.cdn_dict;

-- View current entries (human-readable):
--   SELECT IPv4NumToString(ip_start) AS start, IPv4NumToString(ip_end) AS end, label FROM cdn_override;
