-- AP's IP address as reported by the controller (stat/device's "ip"
-- field). For offline APs especially: an IP lets NOC ping/reach the AP
-- directly to distinguish "device is dead" from "controller lost
-- adoption but the AP is still up".
ALTER TABLE ap_inventory ADD COLUMN IF NOT EXISTS ip TEXT;
