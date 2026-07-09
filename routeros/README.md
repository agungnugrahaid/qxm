# RouterOS Push Scripts

Two scripts, two schedules — deliberately kept separate so the rarely-changing firmware check doesn't ride on the same 5-minute cadence as everything else.

## Install

There are two metrics script variants — `qoe-push-metrics-v7.rsc` (RouterOS v7+, uses `/ping ... as-value` for real latency/loss numbers) and `qoe-push-metrics-v6.rsc` (RouterOS v6, no ping block — see "Two metrics script variants" below for why). `deploy_lib.py` (used by both admin-ui and `bulk_deploy.py`) auto-detects each router's actual RouterOS major version after connecting and pushes the matching one — you don't need to pick manually when deploying through those tools.

For a fully manual install instead:

1. Copy the variant matching the router's RouterOS major version, plus `qoe-push-firmware.rsc`, into RouterOS (`/import file=...` after uploading via Files, or paste directly into `/system script add`).
2. Edit the `url` and `token` placeholders in both — the token must match a row you've created in the `routers` table.
3. Schedule them:

```
/system scheduler add name=qoe-push-metrics interval=5m on-event=qoe-push-metrics
/system scheduler add name=qoe-push-firmware interval=1d on-event=qoe-push-firmware
```

(`on-event` refers to the script name if added via `/system script add name=qoe-push-metrics source=[contents of the .rsc file]` — adjust to however you're managing scripts.)

## Two metrics script variants (RouterOS v6 vs v7)

`/ping ... as-value` (used to capture structured per-packet latency/loss) is confirmed working on RouterOS 7.20.4, but on at least one RouterOS 6.49.8 (long-term) box it isn't recognized by the script parser **at all** — not a runtime error, a hard parse failure, which kills the *entire* script (uplink/CPU/RAM/DHCP data included, not just the ping block). `qoe-push-metrics-v6.rsc` exists specifically to avoid that: it drops the ping block entirely rather than risk everything else failing to push too. Routers deployed with it will show no data in the "Ping Latency & Loss" panel — that's expected, not a bug, for that RouterOS branch.

If you hit the same parse failure on a different v6 build, or find the working syntax for it, that's worth feeding back into `qoe-push-metrics-v6.rsc` rather than treating it as router-specific.

## Test on one router first

RouterOS scripting syntax for ping statistics (`as-value`, time-value arithmetic) has been finicky across versions in the past. Before rolling this to more than one pilot CPE:

- Run the metrics script manually (`/system script run qoe-push-metrics`) and check `/log print` for errors.
- Confirm the payload actually lands — check `path_metrics` and `dhcp_pool_metrics` in Adminer/Grafana after a manual run.
- **Ping numbers specifically won't be trustworthy from a manual run.** Confirmed on 7.20.4: `/ping ... as-value` only returns real per-packet data when the script is fired by `/system scheduler`; a script run triggered directly via `/system script run` (including over the API, which is how admin-ui's manual-trigger tooling works) silently gets an empty result -- no error, just 100% loss / 0ms across the board. That's not a connectivity problem -- wait for an actual scheduled cycle (or check `path_metrics` a few minutes later) before concluding ping is broken. Every other field (uptime, CPU/RAM/disk, uplink traffic, DHCP) updates correctly on a manual run; this quirk is specific to ping's `as-value` capture.
- If `rtt_avg_ms`/`packet_loss_pct` come back as 0 or blank on an actual *scheduled* run (not a manual one) — that's the real signal something's off. Check `/ping address=8.8.8.8 count=5 as-value` output shape directly in the terminal to see the actual field names/types your version returns. If it fails to parse at all (rather than returning bad values), you've likely hit the same v6 wall described above.

## Using api-ssl instead of plaintext api

The management connection (used by admin-ui/bulk_deploy.py to push these scripts, not by the scripts themselves) defaults to RouterOS's plaintext `api` service on port 8728. That port is a real brute-force target if it's reachable from the internet — switch a router to `api-ssl` (port 8729, TLS-wrapped) once it's been hit.

On the router, issue it a self-signed cert and assign it to the api-ssl service:

```
/certificate add name=api-ssl-cert common-name=<router-identity> key-usage=tls-server,key-cert-sign,crl-sign
/certificate sign api-ssl-cert
/ip service set api-ssl certificate=api-ssl-cert
```

(`key-cert-sign,crl-sign` lets the cert act as its own CA so `/certificate sign` can self-sign it — a plain `tls-server`-only cert will fail signing with "CA not found".)

Then in admin-ui, edit the router and check **"Use api-ssl"**, and set the management port to `8729`. `deploy_lib.py` will connect over TLS (with certificate verification disabled, since there's no shared CA to check against — it just trusts whatever cert that specific router presents).

Once api-ssl is confirmed working, disable the plaintext `api` service on that router (`/ip service disable api`) so the brute-forced port is actually closed, not just supplemented.

## DNS reachability check

Alongside the two IP-based ping targets (8.8.8.8, 1.1.1.1 -- which never touch DNS since they're pinged directly by address), the v7 script also pings `google.com` as a third target (`dns-google`). Since that requires the router to resolve it first, it's the only target that actually proves DNS resolution is working, not just general internet reachability. Wrapped in `:do/on-error` so a genuine DNS failure reports as 100% loss for that one target instead of taking down the rest of the push -- confirmed RouterOS throws a real exception ("name does not exist") for an unresolvable domain, unlike an unreachable IP which just times out normally.

## DHCP pool size calculation

`total_addresses` is computed from RouterOS's `ranges` field, which can hold multiple comma-separated entries (`:toarray` splits on the comma), each either a dash range (`10.0.0.10-10.0.0.200` -- subtract the two IPs via `:toip` and add 1) or CIDR (`100.64.0.0/16` -- `2^(32-prefix)`), summed per pool. Confirmed correct against real multi-pool configs on both a RouterOS 7.20.4 and a 6.49.8 router (dash ranges, a /16 CIDR pool, and multiple pools per router all computed correctly).

## Daily config snapshot (over SFTP, not HTTP)

`qoe-push-firmware.rsc` also runs `/export compact` daily and uploads the result via SFTP to the `sftp` service (see `docker-compose.yml` and `config-snapshot-watcher/`), which lands in `router_config_snapshots` and is browsable/diffable from admin-ui (`/config-snapshots/{router_id}`).

Two things confirmed the hard way, worth knowing if this ever needs touching again:

- **Why SFTP and not the HTTP `/tool fetch` pattern used everywhere else**: reading a large file's contents into a script variable (`/file get [find name=...] contents`) silently returns `nil` -- not truncated, nothing -- above some size threshold. Confirmed working at ~33KB, confirmed failing at ~81-220KB on real fleet routers (most routers' configs are in that failing range; this wasn't an edge case). SFTP uploads the file directly from flash without ever materializing it into a variable, so it isn't subject to that cap. `/tool fetch upload=yes` only supports (S)FTP anyway -- HTTP upload of arbitrary files isn't a thing RouterOS's fetch tool does at all.
- **SFTP syntax differs between RouterOS versions**: the `mode=sftp address=... port=... user=... password=...` parameter style (the one in RouterOS's own docs) fails to parse on RouterOS 6.49.8 (`syntax error`) even though it works on 7.20.4. The `url="sftp://user:pass@host:port/path"` form works identically on both -- use that, not the parameter style.

All routers share one SFTP account (`configupload`) rather than getting individual accounts -- the SFTP image used (`atmoz/sftp`) only reads its user list at container startup, so per-router accounts would mean restarting the SFTP service (and therefore giving admin-ui Docker control) every time a router is added. Instead, each router uploads its export as `<its own auth_token>.rsc`; the watcher validates that filename against `routers.auth_token` before storing it, so the existing per-router credential is still what gates whose data gets attributed to whom -- the shared account just controls who can *attempt* an upload, not whose data gets accepted. Config exports don't contain secrets (RouterOS masks/omits them by default, and this script's policy structurally can't request `show-sensitive`), so the main exposure from the shared account is other routers' filenames (tokens) being briefly listable before the watcher polls and deletes (every 30s) -- an accepted trade at this fleet size, not something to scale up without revisiting.

## Baseline hardening script (`qoe-baseline-hardening-v7.rsc` / `-v6.rsc`)

Automates what used to be a "default new client" config copy-pasted into the terminal by hand for every new router: SNMP, a non-default management-port set, timezone/NTP, an RFC1918 + gmedia-office management-access allowlist, DNS-recursion/spam/port-scanner firewall filters, RADIUS AAA, and a DNS-cache-flush scheduler. Pushed and scheduled the same way as the other two scripts (`deploy_lib.push_to_router`, once a day).

**Deliberately excluded, left fully alone by this script:**
- **api / api-ssl service config.** The legacy baseline disables api-ssl and uses plaintext api -- the opposite of this repo's own guidance above (prefer api-ssl, disable plaintext api once confirmed) -- and would break any router already on `use_ssl=true`. Stays owned by the api-ssl onboarding flow (admin-ui's router-edit form).
- **The legacy `backup-rsc-client` scheduler.** Does the same thing the config-snapshot block above already does, but via one shared, plaintext-embedded legacy FTP password across the old fleet. Superseded.
- **NAT (global src-nat + DNS redirect).** Many routers in this fleet NAT users from a loopback IP living on an interface-less bridge, not a normal WAN-bound address -- there's no safe generic way to resolve "the WAN IP" across that topology, so this is left out entirely and stays manual.

**Idempotent, not "first-run only".** Rather than tracking "have we ever pushed this to this router" (which can't handle intentional future changes like a secret rotation, and can't heal drift if someone manually reverts a setting), every `add`-type command in the script is preceded by an existence check -- so re-running the whole script against an already-configured router just skips what's already correct and adds whatever's missing. This is what makes it safe to point at the large number of existing customer routers that already have some or all of this hand-applied (several already had the exact NTP servers, RADIUS entries, etc. from the legacy config in place when this was built), not just brand-new ones.

**Why this is two files, not one with a version check inside it:** confirmed live that RouterOS v7's NTP client replaced `primary-ntp`/`secondary-ntp` with a separate `/system ntp client servers` sub-menu that doesn't exist on v6 at all -- and, same as the `/ping ... as-value` situation above, confirmed that RouterOS v6's script engine hard-fails at *run* time on unrecognized v7-only command paths even inside an `:if` branch that's never taken (the script *parses* fine at add-time, then throws `expected command name` the moment you try to run it). Splitting into matching v7/v6 files, selected the same way the metrics scripts already are, sidesteps that entirely.

**The "allow-access" list problem:** the legacy port-scanner detection rules reference an `allow-access` address-list that's never actually defined anywhere in the source config it came from. Left as-is, `!allow-access` would match against an empty/nonexistent list -- i.e. match *everything*, including this pilot's own management traffic. Mapped to `gmedia-all-ip` instead (the same CIDR set used for the management-port allowlist, itself sourced from `.env`'s `SFTP_ALLOWED_CIDRS` -- confirmed to be a superset of the legacy list, including this pilot's own NOC egress range) so there's one allowlist to keep in sync, not a second undefined one.

**Rollout safety:** this script touches firewall DROP rules and non-default management ports on the same channel used to push it -- a real remote-lockout risk if the allowlist doesn't actually cover wherever `push_to_router` is being called from. `push_to_router` deliberately does *not* auto-run this script immediately after pushing it (unlike the other two) -- it waits for its own daily scheduler on first deploy, so a bad push doesn't take effect immediately across the fleet. Before rolling past a single pilot router: deploy once, confirm mgmt access still works, deploy again and confirm no duplicate address-list/filter entries got created (proves the idempotency guards actually work, not just first-run correctness), then re-run against an already hand-configured router specifically to confirm it detects existing state rather than duplicating it.

RADIUS secrets and the gmedia CIDR list are threaded through as env vars (`RADIUS_SERVER_1_SECRET`/`RADIUS_SERVER_2_SECRET`/`SFTP_ALLOWED_CIDRS`, see `.env.example`) rather than hardcoded in the script -- same pattern as every other credential in this repo. If a secret contains a literal `$`, it must be escaped as `$$` in `.env` -- confirmed the hard way that Compose's own `.env` parsing interpolates an unescaped `$word` as a variable reference and silently truncates the value otherwise.

**v7's RADIUS entries need `require-message-auth=no`** -- this property doesn't exist on v6 radius entries at all (confirmed live), so it's only set in `-v7.rsc`. Without it, v7's default (`yes-for-request-resp`) rejects login against these RADIUS servers. Both the RADIUS block and the DNS-cache-flush scheduler use a check-then-set-or-add pattern rather than plain add-if-missing, specifically so a router that already had these from an earlier run (before this property/interval existed, or from the legacy hand-applied config) gets corrected in place too, not just left alone because "something's already there."
