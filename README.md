# QoE Pilot

Quality-of-experience monitoring for an ISP's MikroTik CPE fleet and UniFi customer sites — metrics, alerting, config history, and a small admin UI for onboarding routers, all in one `docker compose` stack.

## What's in the stack

| Service | Purpose |
|---|---|
| `timescaledb` | Postgres + TimescaleDB — all metrics/config history live here |
| `ingestion-api` | Public HTTP endpoint MikroTik routers push metrics/firmware to |
| `collector` | Polls UniFi controllers for client/AP stats |
| `admin-ui` | Onboard routers/customers, push RouterOS scripts, browse config history |
| `sftp` / `config-snapshot-watcher` | Daily RouterOS config snapshots land here, then get moved into Postgres |
| `grafana` + provisioned dashboard | Per-customer QoE dashboard (router status, uplink traffic, ping, DHCP, CPU/RAM/disk, UniFi clients) |
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
