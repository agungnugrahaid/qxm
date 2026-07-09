-- Client's IP address as reported by the UniFi controller (stat/sta's
-- "ip" field). NOC incident lookups often start from an IP (firewall
-- log, abuse report, complaint) rather than a device name -- this makes
-- the Connected Clients table searchable by it.
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS ip TEXT;
