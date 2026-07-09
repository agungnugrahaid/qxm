-- UniFi's own human-readable site label (the "desc" field from
-- GET /api/self/sites), as opposed to unifi_site_name which is the
-- internal random-code identifier (e.g. "gk7em92p") used in API URLs.
-- Confirmed live: desc values already match this pilot's customer names
-- closely (e.g. "01.0757-01.GRAND-AMBARRUKMO").
ALTER TABLE sites ADD COLUMN IF NOT EXISTS site_desc TEXT;
