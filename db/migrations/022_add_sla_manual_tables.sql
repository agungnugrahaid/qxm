-- Manual SLA + ticket entry for the merged Monthly Report. Data is keyed
-- the same way the future ERP API will deliver it (see ERP_SLA_API.md),
-- so swapping the source later is a drop-in change; these tables then
-- become a fallback/cache. YTD figures are always computed, never stored.

CREATE TABLE customer_sla_services (
    id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    month DATE NOT NULL,                  -- first day of the month
    service_id TEXT NOT NULL,             -- e.g. 05.0169.3
    service_name TEXT NOT NULL,           -- e.g. IDEA ONE 30 Mbps
    node_count INT NOT NULL DEFAULT 1,
    sla_pct NUMERIC(6,3) NOT NULL,        -- monthly uptime %
    UNIQUE (customer_id, month, service_id)
);

CREATE TABLE customer_tickets (
    id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    ticket_no TEXT NOT NULL,
    tanggal DATE NOT NULL,
    description TEXT,
    action TEXT,
    mttr_seconds INT,
    status TEXT NOT NULL DEFAULT 'closed'
);

CREATE INDEX idx_sla_services_customer_month ON customer_sla_services (customer_id, month);
CREATE INDEX idx_tickets_customer_date ON customer_tickets (customer_id, tanggal);
