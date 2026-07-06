-- Run this manually (e.g. via Adminer's SQL command box) if you already
-- brought the stack up before the admin UI was added — init.sql only runs
-- on a brand-new, empty database volume.

ALTER TABLE routers ADD COLUMN IF NOT EXISTS mgmt_host TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS mgmt_port INT DEFAULT 8728;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS admin_user TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS admin_password TEXT;
