# Deployment Checklist — Production-Readiness Items

Everything from the "beyond hardware" list is now in the stack. Most of it works as-is; a handful of values are placeholders that only get filled in once you have real DNS/webhook/domain details for your actual deployment.

## What's now in the stack

- **`caddy/`** — reverse proxy + automatic Let's Encrypt TLS, fronting `ingestion-api` (public), and `admin-ui`/`grafana`/`adminer` (basic-auth gated). `timescaledb`, `adminer`, `ingestion-api`, `admin-ui`, and `grafana` no longer publish host ports directly — Caddy (ports 80/443) is now the only public entry point, except Promtail's syslog port (1514/udp), which stays exposed directly since Caddy doesn't proxy raw syslog.
- **`pg-backup` service** — nightly `pg_dump` via `prodrigestivill/postgres-backup-local`, keeping 7 daily / 4 weekly / 6 monthly backups in the `pg_backups` volume.
- **`db/migrations/002_compression_retention.sql`** (and folded into `init.sql` for fresh installs) — compression + retention policies on all the high-volume hypertables.
- **`deploy/qoe-pilot.service`** — systemd unit so the stack survives a VM reboot.
- **`watchdog/`** — a small container polling `ingestion-api` and `admin-ui`'s `/health` endpoints, posting to a webhook after 3 consecutive failures.
- **Docker healthchecks** added to `ingestion-api` and `admin-ui`.
- **`sftp`/`config-snapshot-watcher`** — daily RouterOS config snapshots (`/export compact`), pushed via SFTP (not HTTP — RouterOS's `/file get contents` silently fails above a size threshold most real router configs exceed; SFTP transfers the file directly from flash instead) and browsable/diffable from admin-ui's `/config-snapshots/{router_id}`.

## Placeholders you need to fill in before this is actually live

1. **`caddy/Caddyfile`** — replace `monitor.yourisp.com`, `admin.yourisp.com`, `grafana.yourisp.com`, `adminer.yourisp.com` with your real (sub)domains, and point DNS A/AAAA records at this VM's public IP. Each domain needs its own DNS record for Let's Encrypt to issue a certificate.
2. **Basic auth password hash** — the `JDJhJDE0...` values in the Caddyfile are placeholders, not real hashes. Generate real ones with:
   ```
   docker compose run --rm caddy caddy hash-password
   ```
   and paste the output in place of each placeholder.
3. **`.env`** — copy from `.env.example` and fill in real values. Set `WEBHOOK_URL` to a real Slack/Discord incoming webhook if you want watchdog alerts delivered somewhere; leave blank to just have it log locally.
4. **`deploy/qoe-pilot.service`** — adjust `WorkingDirectory` if you deploy the project somewhere other than where this checkout actually lives.
5. **Off-host backup copy** — `pg-backup` writes to a local Docker volume on the same VM. That protects against database corruption but not against losing the whole VM — worth adding a cron job or object-storage sync (e.g. `rclone` to S3/Backblaze) to copy the `pg_backups` volume off-host periodically. Not included here since it depends on what storage you already have access to.
6. **SFTP firewall allowlist** — the `sftp` service's port (`SFTP_PORT` in `.env`, default 2222) is otherwise open to the whole internet, and *will* get found and brute-forced within days (confirmed in practice, not hypothetical). Set `SFTP_ALLOWED_CIDRS` in `.env` to your network's real IP ranges, then run:
   ```
   sudo bash deploy/setup-sftp-firewall.sh
   ```
   Re-run this any time `SFTP_ALLOWED_CIDRS` changes (e.g. onboarding a router outside your existing ranges). UFW alone does not work for this — see the script's comments for why (Docker's own port-publishing rules bypass UFW's normal filtering).

## What's intentionally still open

- IP-allowlisting instead of basic auth for the internal tools, if/once you have a fixed admin network range or VPN — noted as an option in the Caddyfile comments, not configured since it depends on your actual network.
- Point-in-time recovery (WAL archiving) instead of just nightly dumps — worth it once this is handling real customer data at full scale; nightly `pg_dump` is a reasonable starting point, not the ceiling.
