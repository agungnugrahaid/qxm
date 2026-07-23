-- ASN range-lookup dictionary: fast in-memory IP -> ASN/org for the flow
-- panels and reports (replaces a slow range cross-join over flow.asn).
--
-- The CLICKHOUSE dictionary source authenticates as the `flow` user, so the
-- password is injected at apply time and is NOT stored in the repo. Apply:
--
--   CHP=$(grep ^CH_PASS= .env | cut -d= -f2)
--   sed "s/__CH_PASS__/$CHP/" flow/asn_dict.sql | \
--     docker exec -i qxm-clickhouse clickhouse-client \
--       --user flow --password "$CHP" --database flow --multiquery
--
-- Lookup: dictGetString('flow.asn_dict','org', toUInt8(0), toUInt32(toIPv4OrDefault(ip)))
CREATE DICTIONARY IF NOT EXISTS asn_dict (
  dummy    UInt8,
  ip_start UInt32,
  ip_end   UInt32,
  asn      UInt32,
  org      String,
  country  String
) PRIMARY KEY dummy
SOURCE(CLICKHOUSE(HOST 'localhost' PORT 9000 USER 'flow' PASSWORD '__CH_PASS__' DB 'flow'
  QUERY 'SELECT 0 AS dummy, ip_start, ip_end, asn, org, country FROM flow.asn'))
LAYOUT(RANGE_HASHED())
RANGE(MIN ip_start MAX ip_end)
LIFETIME(3600);
