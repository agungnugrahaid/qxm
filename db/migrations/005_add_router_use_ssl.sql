-- Run manually (e.g. via Adminer) if you already initialized the DB
-- before this field existed.
--
-- Lets a router's management connection go over api-ssl (port 8729,
-- TLS-wrapped) instead of the plaintext api service (port 8728) --
-- worth switching to per-router once you've issued it a certificate,
-- since the plaintext api port is a real brute-force target.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS use_ssl BOOLEAN DEFAULT FALSE;
