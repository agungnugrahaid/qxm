-- Real RouterOS-version update check (via /system package update
-- check-for-updates), distinct from the existing current_firmware/
-- upgrade_firmware columns which are routerboard *bootloader* firmware
-- and almost always match each other -- they never actually reflected
-- whether a newer RouterOS release exists.
ALTER TABLE router_firmware ADD COLUMN IF NOT EXISTS update_channel TEXT;
ALTER TABLE router_firmware ADD COLUMN IF NOT EXISTS latest_routeros_version TEXT;
ALTER TABLE router_firmware ADD COLUMN IF NOT EXISTS update_status TEXT;
