-- Run manually (e.g. via Adminer) if you already initialized the DB
-- before this field existed.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS wan_interface TEXT DEFAULT 'ether1';
