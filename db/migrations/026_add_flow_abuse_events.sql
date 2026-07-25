-- Incident log for per-client connection-rate / PPS abuse detected from IPFIX
-- flows (flow-sync's scan_abuse()). One rolling row per (router, client,
-- incident_hour) so a sustained flood is a single incident that gets its peak
-- rates / last_seen updated, not a per-cycle spam stream. The detector flags a
-- client when its estimated connection rate clears an absolute floor AND is a
-- large multiple of the router's own median client (adaptive) -- see
-- flow/sync_exporters.py and FLOW_COLLECTION_PLAN.md.
--
-- This is the standing capability generalised from the Grand Ambarrukmo finding
-- (internal host 10.100.99.147 opening ~5,300 SYN/s -> conntrack table 100%).
CREATE TABLE IF NOT EXISTS flow_abuse_events (
  id              BIGSERIAL PRIMARY KEY,
  router_id       INTEGER REFERENCES routers(id) ON DELETE CASCADE,
  customer_id     INTEGER,
  internal_ip     TEXT NOT NULL,
  incident_hour   TIMESTAMPTZ NOT NULL,          -- bucket key: hour the incident falls in
  first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
  peak_conn_rate  DOUBLE PRECISION NOT NULL,     -- est. new-connections/s (sampling-scaled)
  peak_pps        DOUBLE PRECISION NOT NULL,     -- est. packets/s (sampling-scaled)
  syn_ratio       DOUBLE PRECISION,              -- fraction of single-packet TCP flows (0..1)
  sampling_factor DOUBLE PRECISION NOT NULL DEFAULT 1,  -- 1 = unsampled; else (interval+space)/interval
  notified        BOOLEAN NOT NULL DEFAULT false,       -- webhook already fired for this incident
  UNIQUE (router_id, internal_ip, incident_hour)
);

CREATE INDEX IF NOT EXISTS idx_flow_abuse_last_seen ON flow_abuse_events (last_seen DESC);
