-- Device name UniFi reports for each client (e.g. "LGwebOSID") -- answers
-- "which device" directly instead of just a MAC address, for the
-- per-client problem-drilldown table.
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS hostname TEXT;
