-- Deploy-progress tracking (see admin-ui/main.py's background /deploy-all)
-- so NOC has live per-router visibility on a 200+ router rollout instead
-- of only ever seeing the result of one blocking HTTP request.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS last_deploy_status TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS last_deploy_at TIMESTAMPTZ;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS last_deploy_detail TEXT;

-- Lets a phased rollout ("critical customer first") target a subset of
-- routers instead of only all-or-nothing.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'standard';

-- Which vendor's API this controller speaks -- see collector.py's
-- vendor-dispatch poll_controller. Existing rows are all UniFi.
ALTER TABLE controllers ADD COLUMN IF NOT EXISTS vendor TEXT DEFAULT 'unifi';
