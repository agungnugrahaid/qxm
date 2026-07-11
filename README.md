# QXM — Quality eXperience Monitoring

Self-hosted quality-of-experience monitoring for an ISP's MikroTik CPE fleet and its UniFi + Ruijie cloud-managed wireless — all in one `docker compose` stack.

QXM pulls it together end to end: MikroTik routers push their own metrics/firmware/config via RouterOS scripts the stack deploys for them, the collector polls UniFi controllers and Ruijie Cloud for per-client and per-AP wireless health, and it all lands in TimescaleDB behind per-customer Grafana dashboards. On top of that sits an admin UI for onboarding routers/customers/sites and pushing RouterOS baseline config, daily config-snapshot history with diffs, log aggregation, and customer-facing PDF reports (on demand or emailed monthly).

## What's in the stack

| Service | Purpose |
|---|---|
| `timescaledb` | Postgres + TimescaleDB — all metrics/config history live here |
| `ingestion-api` | Public HTTP endpoint MikroTik routers push metrics/firmware to |
| `collector` | Polls UniFi controllers and Ruijie Cloud for client/AP stats (vendor-dispatched; sites collect only once paired to a customer) |
| `admin-ui` | Onboard routers/customers/sites, push RouterOS scripts, browse config history, download/share customer reports |
| `sftp` / `config-snapshot-watcher` | Daily RouterOS config snapshots land here, then get moved into Postgres |
| `grafana` + provisioned dashboard | Per-customer QoE dashboard (router status, uplink traffic, ping, DHCP, CPU/RAM/disk, UniFi/Ruijie wireless) |
| `renderer` / `reporter` | Grafana image renderer + a service that builds customer PDF reports (on-demand button, plus monthly email) |
| `loki` / `promtail` | Log aggregation, including MikroTik remote syslog |
| `caddy` | Reverse proxy + automatic TLS — the only public HTTP(S) entry point |
| `adminer` | Ad-hoc DB browser |
| `pg-backup` | Nightly Postgres dumps |
| `watchdog` | Alerts (webhook) if `ingestion-api`/`admin-ui` health checks start failing |

## Deploying this

Start with **[DEPLOY_NOW.md](DEPLOY_NOW.md)** — it's the current, consolidated walkthrough (supersedes `PILOT_SETUP_GUIDE.md`, kept only for history). In short:

```
cp .env.example .env    # fill in real values
```

Then follow `DEPLOY_NOW.md` for DNS, Caddy domains, and bringing the stack up. Also read:

- **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)** — placeholders that need real values before this is actually live, including the SFTP firewall allowlist (`deploy/setup-sftp-firewall.sh`) — the SFTP port gets found and brute-forced within days if left open to the whole internet.
- **[routeros/README.md](routeros/README.md)** — RouterOS-side details: the v6/v7 script split, `api-ssl` setup, DNS reachability check, DHCP pool math, and how the daily config snapshot gets pushed over SFTP.
- **[LOGGING_AND_ALERTS.md](LOGGING_AND_ALERTS.md)** — Loki/Promtail setup and first alert rules.
- **[PRODUCTION_VM_SIZING.md](PRODUCTION_VM_SIZING.md)** — sizing guidance beyond pilot scale.

## Repo layout

```
admin-ui/            Router/customer/site management + config-history browser (FastAPI)
ingestion-api/       Public endpoint routers push metrics/firmware to (FastAPI)
collector/           UniFi controller poller
config-snapshot-watcher/  Moves SFTP-uploaded config exports into Postgres
routeros/            RouterOS push scripts + deploy tooling (deploy_lib.py, bulk_deploy.py)
db/                  Schema (init.sql) + migrations, applied in order for existing installs
grafana/             Provisioned datasources + the Customer Overview dashboard
caddy/               Reverse proxy config (TLS, basic auth for internal tools)
deploy/              systemd unit + firewall setup script
```

## What gets monitored

Per router, every ~5 minutes: uplink traffic (main + backup, if configured), per-core CPU load, RAM/disk usage, ping latency/loss (including a DNS-resolution check, not just raw-IP reachability), and DHCP pool utilization. Once a day: firmware/hardware info and a full RouterOS config snapshot (diffable and downloadable from `admin-ui`). UniFi side: per-site client counts, signal/satisfaction, and AP inventory/health.

RouterOS versions differ enough in scripting behavior that the metrics script has two variants (v6/v7) — `deploy_lib.py` detects each router's actual version and pushes the right one automatically; see `routeros/README.md` for the specifics that forced this.

## Changing things later

Everything here is just files. After editing config/schema/scripts, redeploy with `docker compose up -d --build` (rebuilds only what changed) — no need to rebuild the whole stack for an incremental change. Schema changes go in a new `db/migrations/NNN_*.sql` file (also folded into `db/init.sql` for fresh installs) and get applied manually to the running database.

## Recent changes (July 2026)

### Customer CRUD
`admin-ui` now has full create / edit / delete for customers (`/customers`). Name and address are editable. Deleting a customer is blocked if it still has routers assigned (FK constraint — the UI surfaces the error). The customer form is at `admin-ui/templates/customer_form.html`; backend routes in `admin-ui/main.py` (`GET/POST /customers/{id}/edit`, `POST /customers/{id}/delete`).

### Routers behind NAT — manual setup page
Routers that can't be reached via API (no management IP, or behind NAT) have a **Manual Setup** page (`/routers/{id}/manual-script`) that serves pre-filled, ready-to-paste RouterOS scripts: metrics (v6 and v7 variants), firmware/config snapshot, and the scheduler + syslog CLI commands. No deployment required — the operator pastes the scripts directly into the RouterOS terminal.

### Config snapshots include sensitive credentials (`show-sensitive`)
`routeros/qoe-push-firmware.rsc` now runs `:export compact show-sensitive` instead of `:export compact`, so PPPoE passwords, hotspot passwords, WiFi PSKs, and RADIUS shared keys are included in the daily snapshot. This makes a snapshot a real recovery artefact — paste it onto a replacement router (minus `/user`) and it comes up fully configured. `routeros/deploy_lib.py`'s `SCRIPT_POLICY` was updated to include `sensitive`, which is required for RouterOS to honour `show-sensitive` in a scheduled script (without the policy flag it silently falls back to masked output). Snapshots are stored in `router_config_snapshots` in TimescaleDB — same DB that already holds router admin passwords, no change to threat model.

### Config diff boolean normalisation
`admin-ui/main.py`'s diff renderer now normalises both sides of a diff to `yes`/`no` before computing it. RouterOS 6.x exports booleans as `true`/`false`; 7.x uses `yes`/`no`. A firmware upgrade previously caused a wall of spurious `±disabled=false` / `±disabled=no` noise that buried real changes. The normalisation is display-only — stored snapshots are untouched.

### Syslog IP in manual setup CLI commands
The **Scheduler & Syslog CLI** tab now shows the server's raw IP (`43.245.184.67`) in the `remote=` field instead of the hostname. RouterOS 6.x rejects hostnames in the logging-action `remote` field (`invalid value for argument ip`); using the IP works on both v6 and v7. Configured via `SYSLOG_IP` in `.env` (must also be listed in `docker-compose.yml`'s `admin-ui` environment block — it is). Falls back to `SYSLOG_HOST` if `SYSLOG_IP` is not set.
