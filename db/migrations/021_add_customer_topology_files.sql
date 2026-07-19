-- Per-customer network topology attachments (PDF/image), uploaded and
-- served from the admin-ui customer detail page. Stored as BYTEA so they
-- ride the nightly pg-backup dump; admin-ui enforces a 16 MB/file cap and
-- a PDF/PNG/JPEG/WebP whitelist. CASCADE so topology files never block
-- customer deletion (routers/sites are the intended blockers).

CREATE TABLE customer_topology_files (
    id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    label TEXT,                      -- optional display name, falls back to filename
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INT NOT NULL,
    data BYTEA NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_topology_files_customer ON customer_topology_files (customer_id);
