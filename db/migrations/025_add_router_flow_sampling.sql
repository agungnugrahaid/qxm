-- Per-router traffic-flow packet sampling. Maps 1:1 to RouterOS
-- /ip traffic-flow: packet-sampling samples `interval` consecutive packets,
-- skips `space`, repeats -> sampled packet fraction = interval/(interval+space).
-- Both NULL (or interval 0/NULL) = sampling OFF = full capture (the default,
-- and what light routers should stay on). Set only on busy routers as a
-- CPU/ingest/storage reducer -- byte TOTALS still come from interface counters,
-- so sampling only trades flow-composition precision, not report accuracy.
-- See FLOW_COLLECTION_PLAN.md (lever 2) and routeros/deploy_lib.py.
ALTER TABLE routers ADD COLUMN IF NOT EXISTS flow_sampling_interval INTEGER;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS flow_sampling_space INTEGER;
