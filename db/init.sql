-- QoE pilot schema. Runs automatically the first time the timescaledb
-- container starts (via docker-entrypoint-initdb.d).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE controllers (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    base_url TEXT NOT NULL,
    api_user TEXT NOT NULL,
    api_password TEXT NOT NULL,
    is_unifi_os BOOLEAN DEFAULT FALSE,
    -- which vendor's API this controller speaks -- see collector.py's
    -- vendor-dispatch poll_controller. Everything today is UniFi.
    vendor TEXT DEFAULT 'unifi'
);

CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT
);

CREATE TABLE sites (
    id SERIAL PRIMARY KEY,
    controller_id INT REFERENCES controllers(id),
    unifi_site_name TEXT NOT NULL,
    -- UniFi's own human-readable site label (the "desc" field from
    -- GET /api/self/sites) -- unifi_site_name above is the internal
    -- random-code identifier used in API URLs, not meant for display.
    site_desc TEXT,
    customer_id INT REFERENCES customers(id),
    discovered_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (controller_id, unifi_site_name)
);

CREATE TABLE routers (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id),
    identity_name TEXT UNIQUE NOT NULL,
    auth_token TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ,
    -- management-channel details, used by the admin UI / bulk deploy tool
    -- to push scripts to the router. Fine for a pilot; treat as a
    -- candidate for a real secrets store once this goes past pilot scale.
    mgmt_host TEXT,
    mgmt_port INT DEFAULT 8728,
    admin_user TEXT,
    admin_password TEXT,
    -- which RouterOS interface is the WAN/uplink on this router — varies
    -- per customer (ether1 for a plain ethernet WAN, pppoe-out1 for PPPoE,
    -- etc.), so this can't be safely hardcoded across the fleet.
    wan_interface TEXT DEFAULT 'ether1',
    -- optional second uplink (failover/backup) -- NULL means this router
    -- only has the one WAN above.
    wan_interface_backup TEXT,
    -- api-ssl (TLS-wrapped, port 8729) instead of plaintext api (8728) --
    -- requires the router to have a certificate assigned to its api-ssl
    -- service first.
    use_ssl BOOLEAN DEFAULT FALSE,
    -- deploy-progress tracking so a background /deploy-all gives NOC live
    -- per-router visibility instead of only the result of one blocking
    -- HTTP request (see admin-ui/main.py).
    last_deploy_status TEXT,
    last_deploy_at TIMESTAMPTZ,
    last_deploy_detail TEXT,
    -- lets a phased rollout ("critical customer first") target a subset
    -- of routers instead of only all-or-nothing.
    priority TEXT DEFAULT 'standard'
);

CREATE TABLE client_metrics (
    time TIMESTAMPTZ NOT NULL,
    site_id INT REFERENCES sites(id),
    client_mac TEXT,
    ap_mac TEXT,
    signal INT,
    satisfaction INT,
    radio TEXT,
    tx_retries BIGINT,
    wifi_tx_attempts BIGINT,
    tx_rate BIGINT,
    rx_rate BIGINT,
    noise INT,
    channel INT,
    essid TEXT,
    is_wired BOOLEAN,
    hostname TEXT,
    -- controller-reported client IP -- NOC incident lookups often start
    -- from an IP (firewall log, abuse report) rather than a device name.
    ip TEXT
);
SELECT create_hypertable('client_metrics', 'time');
CREATE INDEX ON client_metrics (site_id, time DESC);

CREATE TABLE router_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    rx_bytes BIGINT,
    tx_bytes BIGINT,
    uptime TEXT,
    cpu_load_pct NUMERIC,
    ram_used_bytes BIGINT,
    ram_total_bytes BIGINT,
    disk_used_bytes BIGINT,
    disk_total_bytes BIGINT,
    conntrack_count BIGINT,
    conntrack_max BIGINT
);
SELECT create_hypertable('router_metrics', 'time');
CREATE INDEX ON router_metrics (router_id, time DESC);

-- Per-core CPU load, one row per core per push -- router_metrics.cpu_load_pct
-- above is a single system-wide average, which can look fine even when one
-- core is individually maxed out.
CREATE TABLE cpu_core_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    core_name TEXT NOT NULL,
    load_pct NUMERIC
);
SELECT create_hypertable('cpu_core_metrics', 'time');
CREATE INDEX ON cpu_core_metrics (router_id, core_name, time DESC);

-- Path-quality pings from the CPE to a couple of fixed, reliable
-- anycast targets (Google/Cloudflare DNS). One row per target per push.
CREATE TABLE path_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    target_name TEXT,
    target_host TEXT,
    rtt_min_ms NUMERIC,
    rtt_avg_ms NUMERIC,
    rtt_max_ms NUMERIC,
    packet_loss_pct NUMERIC,
    jitter_ms NUMERIC
);
SELECT create_hypertable('path_metrics', 'time');
CREATE INDEX ON path_metrics (router_id, time DESC);

-- Uplink traffic, one row per configured uplink per push (a router may
-- have a backup/failover WAN in addition to its main one). Mirrors the
-- one-row-per-target/pool pattern below rather than fixed main/backup
-- columns, so it isn't hardcoded to exactly two uplinks.
CREATE TABLE uplink_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    uplink_label TEXT NOT NULL,
    interface_name TEXT,
    rx_bytes BIGINT,
    tx_bytes BIGINT
);
SELECT create_hypertable('uplink_metrics', 'time');
CREATE INDEX ON uplink_metrics (router_id, uplink_label, time DESC);

-- DHCP pool utilization per router (one row per pool per push).
CREATE TABLE dhcp_pool_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    pool_name TEXT,
    total_addresses INT,
    active_leases INT,
    utilization_pct NUMERIC
);
SELECT create_hypertable('dhcp_pool_metrics', 'time');
CREATE INDEX ON dhcp_pool_metrics (router_id, time DESC);

-- MikroTik firmware/version snapshots. Pushed once a day, not every cycle.
CREATE TABLE router_firmware (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    routeros_version TEXT,
    current_firmware TEXT,
    upgrade_firmware TEXT,
    architecture TEXT,
    board_name TEXT,
    update_channel TEXT,
    latest_routeros_version TEXT,
    update_status TEXT
);
SELECT create_hypertable('router_firmware', 'time');
CREATE INDEX ON router_firmware (router_id, time DESC);

-- Daily RouterOS config snapshots (from `/export compact`), pushed
-- alongside the firmware check above -- one row per day per router.
CREATE TABLE router_config_snapshots (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    config_text TEXT,
    size_bytes INT
);
SELECT create_hypertable('router_config_snapshots', 'time');
CREATE INDEX ON router_config_snapshots (router_id, time DESC);

-- UniFi AP inventory/health snapshots, pulled from stat/device alongside
-- the existing stat/sta client poll — no extra API call needed since
-- firmware version rides along with fields we'd fetch anyway.
CREATE TABLE ap_inventory (
    time TIMESTAMPTZ NOT NULL,
    site_id INT REFERENCES sites(id),
    ap_mac TEXT,
    ap_name TEXT,
    model TEXT,
    version TEXT,
    cpu_pct NUMERIC,
    mem_pct NUMERIC,
    channel_util_2g NUMERIC,
    channel_util_5g NUMERIC,
    state INT,
    satisfaction INT,
    num_sta INT,
    uptime BIGINT
);
SELECT create_hypertable('ap_inventory', 'time');
CREATE INDEX ON ap_inventory (site_id, time DESC);

-- Physical interface (ether/SFP) health: link state + error/drop counters,
-- one row per physical port per push. Lets QoE alerting catch cabling/SFP
-- degradation and duplex mismatches that ping-based path_metrics can't see
-- (a link can pass a 5-packet ping test fine while still shedding frames
-- under real load).
CREATE TABLE interface_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    interface_name TEXT NOT NULL,
    running BOOLEAN,
    disabled BOOLEAN,
    rx_fcs_error BIGINT,
    rx_too_short BIGINT,
    rx_too_long BIGINT,
    rx_overflow BIGINT,
    tx_collision BIGINT,
    tx_late_collision BIGINT,
    tx_underrun BIGINT
);
SELECT create_hypertable('interface_metrics', 'time');
CREATE INDEX ON interface_metrics (router_id, interface_name, time DESC);

-- System health sensors (temperature, fan speed/state, PSU state,
-- voltage, current, power draw). Flexible key/value shape rather than
-- fixed columns, since RouterOS 6 and 7 expose genuinely different
-- sensor sets (v6: voltage/current/temperature/cpu-temperature/
-- power-consumption/fan1-speed as fixed singleton properties; v7:
-- a variable list of named "gauges" -- per-component temps, fan
-- speeds/state, PSU state -- that varies by hardware). Value is TEXT
-- since gauges mix numeric readings ("41") and status strings ("ok").
CREATE TABLE health_metrics (
    time TIMESTAMPTZ NOT NULL,
    router_id INT REFERENCES routers(id),
    gauge_name TEXT NOT NULL,
    value TEXT,
    unit TEXT
);
SELECT create_hypertable('health_metrics', 'time');
CREATE INDEX ON health_metrics (router_id, gauge_name, time DESC);

-- Compression + retention on the high-volume hypertables — see
-- db/migrations/002_compression_retention.sql for the same policies with
-- more explanation. Included here too so a brand-new install gets these
-- from day one instead of needing the migration run separately.
ALTER TABLE client_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'site_id');
SELECT add_compression_policy('client_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('client_metrics', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE ap_inventory SET (timescaledb.compress, timescaledb.compress_segmentby = 'site_id');
SELECT add_compression_policy('ap_inventory', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('ap_inventory', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE router_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('router_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('router_metrics', INTERVAL '180 days', if_not_exists => true);

ALTER TABLE cpu_core_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, core_name');
SELECT add_compression_policy('cpu_core_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('cpu_core_metrics', INTERVAL '180 days', if_not_exists => true);

ALTER TABLE router_config_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('router_config_snapshots', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('router_config_snapshots', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE uplink_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, uplink_label');
SELECT add_compression_policy('uplink_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('uplink_metrics', INTERVAL '180 days', if_not_exists => true);

ALTER TABLE path_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('path_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('path_metrics', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE dhcp_pool_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id');
SELECT add_compression_policy('dhcp_pool_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('dhcp_pool_metrics', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE interface_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, interface_name');
SELECT add_compression_policy('interface_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('interface_metrics', INTERVAL '90 days', if_not_exists => true);

ALTER TABLE health_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'router_id, gauge_name');
SELECT add_compression_policy('health_metrics', INTERVAL '3 days', if_not_exists => true);
SELECT add_retention_policy('health_metrics', INTERVAL '90 days', if_not_exists => true);

-- Seed a couple of pilot test rows for the CPE side so the ingestion API
-- has something to authenticate against right away. Replace with your own
-- customers/routers once the pilot is working.
INSERT INTO customers (name, address) VALUES ('Pilot Customer A', 'Test address A');
INSERT INTO routers (customer_id, identity_name, auth_token)
VALUES (1, 'pilot-router-1', 'REPLACE_WITH_A_LONG_RANDOM_TOKEN');
