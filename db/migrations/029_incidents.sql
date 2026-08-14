-- 029: derived service incidents.
--
-- Answers the one question nothing else in the stack answers: "was my service
-- actually down, when, and for how long". Today that is a phone call to the
-- NOC, and the SLA figure in the report is typed by hand.
--
-- Written by the watchdog's detector loop, read (later) by the customer
-- portal. Deliberately INFORMATIONAL: measured availability derived from this
-- table is not the contractual SLA, which stays with the ERP/manual figures --
-- the two are already known to disagree (UNISA July: manual 100.000%,
-- ERP-derived 99.878%).

CREATE TABLE IF NOT EXISTS incidents (
  id             BIGSERIAL PRIMARY KEY,
  customer_id    INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  router_id      INTEGER REFERENCES routers(id) ON DELETE CASCADE,
  site_id        INTEGER REFERENCES sites(id) ON DELETE CASCADE,
  -- router_unreachable | internet_down | degraded | uplink_down
  -- | aps_offline | dhcp_full | conntrack_full
  kind           TEXT NOT NULL,
  -- outage | degraded | advisory
  severity       TEXT NOT NULL DEFAULT 'outage',
  started_at     TIMESTAMPTZ NOT NULL,
  ended_at       TIMESTAMPTZ,               -- NULL = still open
  detail         JSONB,                     -- {"targets": [...], "gap_seconds": 900}
  -- TRUE when the evidence is "we stopped receiving data from most of the
  -- fleet at once", i.e. OUR outage, not the customer's. Such rows must never
  -- count against a customer's availability -- the collector has frozen for
  -- ~2h before, and without this flag that would surface as a customer outage
  -- in a customer-facing view.
  monitoring_gap BOOLEAN NOT NULL DEFAULT FALSE,
  notified       BOOLEAN NOT NULL DEFAULT FALSE,
  detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (customer, kind, router, start). Re-running the detector over
-- the same window updates the open incident instead of duplicating it --
-- same idempotency shape as flow_abuse_events.
CREATE UNIQUE INDEX IF NOT EXISTS incidents_natural_key
  ON incidents (customer_id, kind, COALESCE(router_id, -1), started_at);

CREATE INDEX IF NOT EXISTS idx_incidents_customer_time
  ON incidents (customer_id, started_at DESC);

-- Open incidents are read on every detector pass and every status banner.
CREATE INDEX IF NOT EXISTS idx_incidents_open
  ON incidents (customer_id) WHERE ended_at IS NULL;
