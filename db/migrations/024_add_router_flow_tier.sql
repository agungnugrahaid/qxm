-- Flow attribution gating on routers. `router_flow_exporters` (migration 023)
-- says WHICH exporter IPs belong to a router; these two columns say whether
-- flow is turned ON for the router and whether its egress is even attributable.
--
--   flow_enabled  -- the Console's on/off switch. deploy_lib's traffic-flow
--                    rollout step reads this as its gate (register exporters +
--                    tier, THEN flip this on). A router with exporter rows but
--                    flow_enabled=false is "registered, not yet enabled".
--   flow_tier     -- attributability class (see FLOW_COLLECTION_PLAN.md,
--                    Per-customer attribution):
--                      public-distinct -- one router behind its own public IP(s)
--                      multi-uplink    -- several distinct public IPs, all ours
--                      cgnat           -- shares a public IP with others; flows
--                                         CANNOT be attributed to one customer
--                    NULL = untriaged. CGNAT is a hard limit: leave flow off.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS flow_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS flow_tier TEXT;
