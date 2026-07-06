# Pilot Environment — Setup & Test Guide

This spins up the full stack on your own server using Docker Compose: TimescaleDB (storage), an ingestion API (for MikroTik CPE push), an async collector (for UniFi poll), Grafana (dashboard), and Adminer (a no-code way to browse the database while testing). Scope the pilot to **one UniFi controller with 2-3 sites, and 1-2 test CPE routers** — don't point this at all 5 controllers/250 routers yet.

## Prerequisites on your server

- Docker Engine and the Docker Compose plugin installed (`docker --version` and `docker compose version` should both work).
- Network access from this server to your pilot UniFi controller (same LAN or VPN).
- A couple of ports free: 5432 (Postgres), 8000 (ingestion API), 8081 (Adminer), 3000 (Grafana).

## 1. Get the project onto your server

Copy the `qoe-pilot/` folder (docker-compose.yml, `db/`, `ingestion-api/`, `collector/`) to your server, e.g. via `scp` or git if you put it in a repo.

## 2. Configure secrets and pilot scope

```
cd qoe-pilot
cp .env.example .env
```

Edit `.env` and set real values for `DB_PASSWORD` and `GRAFANA_PASSWORD`.

Edit `collector/controllers.yaml` and fill in your **one pilot controller's** real URL, credentials, and 2-3 real site names (as they appear in the UniFi UI). Leave `is_unifi_os: true` if it's a UDM/UDM Pro console.

Edit `db/init.sql` before first startup if you want to seed a real test router instead of the placeholder — or just start with the placeholder and update it later via Adminer (see step 5).

## 3. Bring the stack up

```
docker compose up -d --build
docker compose ps
```

All five services (timescaledb, adminer, ingestion-api, collector, grafana) should show as running. Check the collector is actually reaching your controller:

```
docker compose logs -f collector
```

You should see lines like `[pilot-controller-1/default] saved 14 client rows at ...` every 5 minutes (the default `poll_interval_seconds`). Errors here almost always mean a URL, credential, or `is_unifi_os` mismatch — fix `controllers.yaml` and run `docker compose restart collector`.

## 4. Confirm data is landing

Open Adminer at `http://<your-server>:8081` — system: PostgreSQL, server: `timescaledb`, username: `qoe`, password: from your `.env`, database: `qoe`. Browse the `client_metrics` table — you should see rows accumulating with real signal/satisfaction values from your pilot sites.

## 5. Test the MikroTik push side

Before touching a real router, simulate a push with `curl` to confirm the ingestion API and auth are wired correctly:

```
curl -X POST http://<your-server>:8000/ingest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer REPLACE_WITH_A_LONG_RANDOM_TOKEN" \
  -d '{"router_id":"pilot-router-1","rx_bytes":123456,"tx_bytes":78910,"uptime":"2d3h"}'
```

(Token and `router_id` must match a row in the `routers` table — the seed data in `db/init.sql` uses `pilot-router-1` with the placeholder token; update both to match, or update the table via Adminer instead.) A `{"status":"ok"}` response means it worked — check `router_metrics` in Adminer to confirm the row landed and `routers.last_seen_at` updated.

Once that works, put the RouterOS scheduler script (from the ISP-scale addendum) on one real pilot CPE router, pointed at `http://<your-server>:8000/ingest`, with that router's own token added to the `routers` table first.

## 6. Wire up Grafana

Open `http://<your-server>:3000` (login `admin` / your `GRAFANA_PASSWORD`). Add a data source: PostgreSQL, host `timescaledb:5432`, database `qoe`, user `qoe`, password from `.env`, SSL disabled. Build one simple panel first — e.g. average `signal` from `client_metrics` over time for your pilot site — just to confirm the data path end to end before building anything elaborate.

## Pilot success criteria

Before expanding to the other 4 controllers and the rest of the CPE fleet, confirm:

- Collector has polled your pilot sites reliably for at least 24-48 hours without silent failures.
- At least one real CPE router has pushed data successfully and shows up correctly in `router_metrics`.
- Adminer/Grafana both show sensible, non-empty data for the pilot scope.
- You've deliberately broken something (wrong password, unreachable controller, expired token) and confirmed it fails loudly in the logs rather than silently — you'll want that visibility once this runs against 250 routers unattended.

## Scaling up from here

Add the remaining controllers/sites to `controllers.yaml`, and start filling in `customers`/`routers`/site-to-customer mappings for real. Once you're past a handful of controllers, revisit the concurrency notes in `isp_scale_addendum.md` — the pilot collector polls controllers one at a time, which is fine for 1-2 but should be parallelized before pointing it at all 5.
