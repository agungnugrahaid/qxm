# Deploy Now — Consolidated Walkthrough

This supersedes the earlier PILOT_SETUP_GUIDE.md, which predates Caddy, backups, the WAN-interface field, the Sites page, and the Grafana dashboard. Follow this one top to bottom for the current stack.

## 0. What you're deploying

`timescaledb` (storage) · `ingestion-api` (receives MikroTik CPE pushes) · `collector` (polls UniFi controllers) · `admin-ui` (manage routers/sites/customers, push scripts) · `grafana` + provisioned Customer Overview dashboard · `loki`/`promtail` (logs) · `caddy` (TLS + reverse proxy, the only public entry point besides syslog) · `pg-backup` (nightly dumps) · `watchdog` (health alerting) · `adminer` (DB browser).

## 1. Server prerequisites

- Docker Engine + Compose plugin on the VM.
- DNS: 4 A/AAAA records pointing at this VM's public IP — one each for the domains you'll put in `caddy/Caddyfile` (ingestion, admin, grafana, adminer).
- Ports open: 80, 443 (Caddy), 1514/udp (Promtail syslog — only needs to be reachable from wherever your CPE routers actually send logs, likely your existing management channel, not the open internet).
- Network reachability from this VM to your UniFi controllers, and to your CPE routers' management channel (the one confirmed earlier — you already have remote access into the fleet).

## 2. Get the project onto the server and configure secrets

```
scp -r qoe-pilot/ youruser@your-vm:/opt/qoe-pilot
cd /opt/qoe-pilot
cp .env.example .env
```

Edit `.env`: set `DB_PASSWORD`, `GRAFANA_PASSWORD`, `INGEST_BASE_URL` (your real ingestion domain), and `WEBHOOK_URL` if you want watchdog alerts delivered somewhere.

## 3. Configure Caddy (domains + auth)

Edit `caddy/Caddyfile`: replace the four `*.yourisp.com` placeholders with your real domains. Then generate real basic-auth hashes (the ones in the file now are placeholders):

```
docker compose run --rm caddy caddy hash-password
```

Paste each generated hash in place of the `JDJhJDE0...` placeholders (admin/grafana/adminer blocks).

## 4. Configure the UniFi collector

Edit `collector/controllers.yaml`. Start with **one controller, 2-3 real sites** — not all 5/250 yet. Fill in the real controller URL, credentials, and `is_unifi_os` (true for a UDM/UDM Pro console).

## 5. Bring the stack up

```
docker compose up -d --build
docker compose ps
docker compose logs -f collector
```

Confirm the collector logs successful poll cycles, and that `caddy`, `ingestion-api`, `admin-ui`, `grafana`, `timescaledb` all show healthy/running.

## 6. Boot resilience

```
sudo cp deploy/qoe-pilot.service /etc/systemd/system/
# edit WorkingDirectory in that file if not /opt/qoe-pilot
sudo systemctl daemon-reload
sudo systemctl enable --now qoe-pilot.service
```

## 7. Set up customers, routers, and push the scripts

Open `https://admin.yourdomain/` (basic-auth prompt first).

1. **Customers** page — add your pilot customer(s).
2. **Routers** page — add your pilot CPE router(s): identity name (must match RouterOS identity), management host/port/credentials, and `wan_interface` (`ether1` for plain ethernet WAN, `pppoe-out1` for PPPoE, etc. — check the actual router if unsure).
3. Click **Deploy** on that router — this pushes both RouterOS scripts + scheduler entries live over your management channel. Check the result message.

(For onboarding many routers at once from a spreadsheet instead of one at a time, use `routeros/bulk_deploy.py` with a filled-in CSV based on `router_inventory.example.csv` — same underlying deploy logic either way.)

## 8. Assign discovered UniFi sites to customers

Once the collector has polled at least once, go to **Sites** in admin-ui — newly discovered sites show as "unassigned." Assign each to the right customer.

## 9. Set up MikroTik syslog (optional but recommended)

On each pilot router, add the remote logging action from `LOGGING_AND_ALERTS.md` pointed at `<this-server>:1514`.

## 10. Verify data end to end

- **Adminer** (`https://adminer.yourdomain/`) — browse `client_metrics`, `router_metrics`, `path_metrics`, `dhcp_pool_metrics`, `router_firmware`, `ap_inventory` — confirm real rows are landing.
- **Grafana** (`https://grafana.yourdomain/`) — open the **Customer Overview** dashboard, pick your pilot customer from the dropdown, confirm router status, uplink traffic, ping/DHCP/firmware tables, and UniFi client/signal panels all show sensible data.
- **Backups** — `docker compose logs pg-backup` after the first scheduled run (default `@daily`); confirm files exist in the `pg_backups` volume.
- **Watchdog** — if you set `WEBHOOK_URL`, temporarily stop `ingestion-api` (`docker compose stop ingestion-api`) and confirm an alert arrives after ~3 minutes, then start it back up.

## 11. Pilot bar before scaling to all 5 controllers / 250 routers

- Collector has polled the pilot sites reliably for 24-48 hours with no silent failures.
- At least one real CPE has pushed successfully and shows up correctly end to end.
- You've deliberately broken something (bad password, unreachable controller, expired token) and confirmed it fails loudly rather than silently.

Once that holds, add the remaining controllers to `controllers.yaml`, and onboard the rest of the fleet through admin-ui or `bulk_deploy.py`.

## Changing things later

Everything here is just files — ask for a change any time (schema, a new panel, a new metric, a UI tweak), and after I edit them, redeploy with `docker compose up -d --build` (rebuilds only what changed) or run the relevant migration SQL for schema changes. No need to rebuild the whole stack from scratch for an incremental change.
