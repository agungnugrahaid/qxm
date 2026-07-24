-- Per-router flow exporter IP ranges -> source of truth for the ClickHouse
-- flow.exporter_map sync (flow attribution). A router can present multiple
-- exporter (NAT egress) IPs -- main/backup/LTE uplinks each masquerade to their
-- own public IP -- so this is a child table, one row per known IP or CIDR.
-- The flow-sync service reads this (joined to routers.customer_id) and rewrites
-- flow.exporter_map. See FLOW_COLLECTION_PLAN.md (Per-customer attribution).
CREATE TABLE IF NOT EXISTS router_flow_exporters (
    id         SERIAL PRIMARY KEY,
    router_id  INTEGER NOT NULL REFERENCES routers(id) ON DELETE CASCADE,
    cidr       TEXT NOT NULL,   -- exporter public IP/CIDR, e.g. '111.68.29.39/32'
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_router_flow_exporters_router ON router_flow_exporters(router_id);
