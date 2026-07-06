# Production VM Sizing & Readiness Checklist

Short answer: yes, once this covers all 5 controllers / ~250 sites / ~250 CPE routers, this needs a real production-spec VM — the pilot can run on almost anything, but full scale genuinely needs headroom, mainly for Postgres/TimescaleDB.

## Why Postgres is the number that matters

Working from your numbers (250 sites, 50-200 APs/site — call it ~125 average, so ~31,000 APs total) and assuming a rough 15 concurrent clients per AP (adjust once you have real pilot data — this is the single biggest unknown), a 5-minute poll cycle writes on the order of:

- ~470,000 `client_metrics` rows per poll cycle → ~135 million rows/day
- ~31,000 `ap_inventory` rows per poll cycle
- CPE-side tables (`router_metrics`, `path_metrics`, `dhcp_pool_metrics`) are tiny by comparison — only 250 routers pushing a handful of rows each every 5 minutes.

That client-metrics volume is the real sizing driver. It's very workable with TimescaleDB (that's exactly what it's built for), but it means: real SSD/NVMe storage (not spinning disk), a retention/compression policy from day one (not an afterthought), and enough RAM for Postgres to keep recent data cached.

**Once the pilot has run a few days, check the actual row-growth rate in Adminer/Grafana and re-check this sizing against real numbers instead of the estimate above** — actual client density varies a lot by site type (a hotel lobby AP and a warehouse AP look very different).

## Suggested starting spec

| Resource | Recommendation | Why |
|---|---|---|
| vCPU | 8-16 | Postgres write/compression load, concurrent polling across 5 controllers, Grafana query load |
| RAM | 32-64 GB | Postgres benefits heavily from OS page cache at this row volume; more RAM = snappier Grafana dashboards |
| Disk | NVMe/SSD, start 500GB-1TB, plan to grow | Spinning disk will not keep up with this insert rate; VMs make disk resize easy later, so it's fine to start modest and expand |
| Network | Modest — well under 100 Mbps sustained | Payloads are small JSON; even 250 routers pushing every 5 min is negligible traffic |
| OS | Ubuntu Server 22.04/24.04 LTS (or any Docker-supported Linux) | Just needs Docker Engine + Compose plugin |

This is a starting point, not a hard ceiling — the advantage of a VM is you can resize CPU/RAM/disk without re-architecting anything.

## Beyond hardware — what makes it "production ready"

Hardware alone doesn't make this production-grade. Before treating it as live infrastructure:

- **TLS in front of the ingestion API.** This is the one service 250 remote CPEs need to reach over the public internet — put a reverse proxy (Caddy or Nginx with Let's Encrypt) in front of it rather than serving plain HTTP, since the RouterOS scripts already assume an `https://` URL.
- **Lock down the internal tools.** Grafana, Adminer, and admin-ui have no reason to be reachable from the open internet — restrict them to a VPN/management network or put them behind the same reverse proxy with authentication/IP allowlisting. Only the ingestion endpoint needs to be public.
- **Automated Postgres backups.** Nothing in the current stack backs up the database. At minimum, a nightly `pg_dump`; for less data-loss risk, continuous WAL archiving (e.g. pgBackRest) for point-in-time recovery.
- **Compression + retention policy on the hypertables**, not just the idea of one — concretely, add TimescaleDB compression policies and a retention window (e.g. 30-90 days raw, downsampled/aggregated beyond that) before the disk fills up.
- **Boot resilience.** Make sure `docker compose up -d` runs automatically after a VM reboot (a systemd unit calling `docker compose up -d` in the project directory is the simplest approach) — right now a reboot would leave the whole stack down until someone notices.
- **Monitor the monitor.** An external check hitting `/health` on the ingestion API (and alerting if it's down) closes the loop — otherwise the one system meant to tell you when things break has no one watching it break.

None of this is exotic, but each item is a real gap between "it runs" and "it's production infrastructure" — worth treating as a punch list before cutting over the full fleet.
