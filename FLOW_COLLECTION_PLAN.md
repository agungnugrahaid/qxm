# Traffic-Flow Collection — Phased Plan

Status: **plan only, nothing built.** This is the agreed design for adding
per-customer traffic-flow (NetFlow/IPFIX) collection to QXM, to be executed
phase by phase with a hard gate after Phase 0.

## Goal & scope

Add a **per-customer internet-usage breakdown** — **top content providers**
(by destination network/ASN, e.g. Google, Netflix, Meta) and **top internal
users** (heaviest devices/clients) — to enrich the existing Monthly Report and
the per-customer Grafana dashboards. Provider = ASN/organization, NOT specific
websites/URLs (encrypted + CDN-fronted; not wanted anyway).

**Critical framing:** the flow stack does **not** owe us accurate totals. The
Monthly Report already gets byte-exact uplink volume from the interface
counters in the metrics push ("Internet Traffic" panel + uplink graphs). Flow's
only job is **composition**, which is why sampled data is acceptable (see
Levers). If we ever needed billing-grade per-flow accounting this design would
change; we don't.

Not in scope: DDoS/forensics; specific hostnames/URLs (that's DNS logging, a
separate stream we don't want); the domestic/international transit split (a
gmedia peering/cost metric, not customer-facing — see Report integration);
sFlow (MikroTik doesn't speak it — RouterOS exports NetFlow v5/v9 and IPFIX via
`/ip traffic-flow`). Note ASN is used here only to *name destinations*, not for
peering analysis.

## Architecture

```
MikroTik CPE ──IPFIX (UDP 4739), WAN-only, sampled──▶ goflow2 (decoder)
                                                           │ JSON
                                                           ▼
                                               flow-inserter (async_insert)
                                                           ▼
                                         ClickHouse ── raw flows (short TTL)
                                             │            └▶ hourly top-N MVs (13-mo TTL)
                                             │  (query-time: dictGet against
                                             │   GeoLite2-ASN dictionary → provider)
                                             ▼
                         Grafana 13.1 (ClickHouse datasource, SHARED with QXM)
                                             ▼
                         report_lib.py renders the panel into the PDF
```

Three new containers (`clickhouse`, `goflow2`, `flow-inserter`) in a **sibling
`docker-compose.flow.yml`** on their own network, joined to Grafana's network.
Kept separate on purpose: different storage engine/lifecycle from TimescaleDB,
and it makes the eventual move to a dedicated VM a host change, not a redesign.
**No Kafka at current scale** — ClickHouse `async_insert` batches server-side;
Kafka/Redpanda is the documented scale-out (see Scaling to 500).

## Core design decisions (the four efficiency levers + attribution)

These are all parameters of one new `deploy_lib` traffic-flow step, sized per
router.

1. **WAN-only observation** — `interfaces=` built per-router from
   `routers.wan_interface` **+** `routers.wan_interface_backup` (must include
   the failover uplink or we go blind during failover). Captures 100% of
   internet traffic (every internet flow crosses the uplink), drops intra-LAN
   noise, cuts volume and CPE CPU. Not a compromise — better-targeted than
   `interfaces=all` for a usage report.

2. **Sampling** (`packet-sampling=yes`) — **tested on the canary 2026-07-24, and
   it's weaker than hoped.** Availability: MikroTik docs say v7-only, but the
   canary is **RouterOS 6.48.6 and sampling demonstrably works** — a sustained
   ~3× flow-rate drop for 15 min after enabling it — so treat it as available on
   late-v6 too (verify per version rather than trusting the doc). Semantics:
   sample `sampling-interval` consecutive packets, skip `sampling-space`, repeat
   → packet fraction = `interval/(interval+space)`. BUT it samples *packets*
   while recording a flow if *any* of its packets is sampled, with ~full
   conntrack counts — so a "1/100" config (`interval=1 space=99`) dropped
   flows/bytes only **~3×**, not 100×, and per-flow byte counts barely changed.
   Consequences:
   - **Storage saving is modest** (row count ~3×, not the sampling factor) and
     **byte totals can't be recovered by ×rate** — the true-byte factor depends
     on the flow-size distribution and must be calibrated empirically vs the
     interface counters. Also the rate is **not advertised** to goflow2
     (`sampling_rate` stayed 0), confirming manual handling either way.
   - **So sampling is NOT the primary storage lever** (see Scaling: short raw
     TTL is). It's an optional **CPE-CPU / ingest / network reducer** for busy
     routers — worth it because totals come from counters, so approximate
     composition is fine; skip it on light routers.

   **v7 update — 2026-07-24, Grand Ambarrukmo (RouterOS 7.16.1), 1:99.** The
   reduction is **far stronger than v6**: ~26× average (~50× off the peak),
   3,850→~140 flows/s, holding steady — this hot router dropped to canary scale.
   The mechanism explains both: a flow is kept if **any** of its packets is
   sampled, so effectiveness is **flow-size-distribution-dependent**. Long
   multi-packet flows almost always have a sampled packet (v6's weak ~3× was on
   normal traffic); **single-packet flows** (this router's volume is dominated
   by one host's P2P/SYN churn) have only a ~1% catch chance → dropped wholesale.
   Net: sampling is a **strong** lever exactly where it's needed (busy routers
   drowning in tiny-flow churn), weak on clean multi-packet traffic — so decide
   per-router (routers.flow_sampling_*), not fleet-wide. `sampling_rate` still 0
   (not advertised). **Composition preserved** (big multi-packet download flows
   survive — top providers unchanged); **flow-derived byte VOLUME undercounts**
   on a sampled router (we drop ~96% of flows, survivors keep full counts) — fine
   by design, totals come from interface counters, but don't read the flow
   dashboard's GiB as absolute for a sampled router.

   **Config changes emit a one-time garbage burst.** Any `/ip traffic-flow`
   config change regenerates the IPFIX template (and every deploy_lib push does),
   and goflow2 decodes a short burst against the in-flight template → malformed
   records (Grand Ambarrukmo: 21 rows at the sampling-enable instant — reserved
   addresses, near-UInt64-max byte/packet counts). They're filtered out of the
   rollup MVs (both-public → neither up nor down) but land in `flows_raw`.
   `inserter.py` now drops records over sane caps (MAX_BYTES 10 TB / MAX_PACKETS
   10 B) so this never reaches storage; a `dropped=` counter surfaces it in the
   collector log.

3. **Trimmed IPFIX field template** — the default exports ~36 fields incl.
   `tcp-seq-num`/`tcp-ack-num`/`tcp-window-size`/`src+dst-mac-address`/`ttl`/
   `tos`, none of which feed a usage report. Keep only: `first-forwarded`,
   `last-forwarded`, `src-address`, `dst-address`, `src-port`, `dst-port`,
   `protocol`, `bytes`, `packets`, `in-interface`, `out-interface`,
   `nat-src-address`, `nat-dst-address`. Shrinks every record → less CPE export
   bandwidth and less storage.

4. **`cache-entries` set explicitly per router class** — do NOT inherit the
   default (v6 = 64k, v7 = 1M; see v6/v7 note). Size to device RAM: generous on
   big sites (Eastparc-class, 322 APs — 64k could truncate), modest on low-RAM
   hAP CPE (1M entries is ~100 MB+ RAM, too much for the smallest). Sampling
   reduces the needed value.

**Per-customer attribution** (the piece that makes this QXM-native, not a
generic flow tool). Flow export is **IP-identified, not token-authenticated** —
unlike the metrics push, which uses a token *precisely because* CPE sit behind
NAT. So flow attribution inherits the NAT problem the metrics path was built to
dodge, and the design must handle it head-on:

- **Range-set per router, not a single IP.** A router with main/backup/LTE
  uplinks masquerades each uplink to its *own* public IP, and export packets
  take whichever uplink routing/failover picks — so one router presents a *set*
  of exporter IPs over time. Model attribution as a **range-lookup dictionary**
  (same pattern as `asn`/`cdn_override`): `(ip_start, ip_end) → customer`, a
  `/32` per known uplink IP or a CIDR per block. Source of truth is Postgres: a
  per-router **`router_flow_exporters(router_id, ip_start, ip_end)`** child
  table, synced into the ClickHouse dictionary. (Supersedes the earlier
  single-`flow_exporter_ip`-column idea.)
- **Learn-and-flag** for IPs you can't know up front (a backup uplink's public
  IP only appears on failover): a periodic check surfaces any exporter IP in
  `flows_raw` not covered by the map as "unattributed — assign me"; the operator
  maps it (→ Postgres). Catches new/backup IPs the first time they're used.
- **CGNAT is the hard limit — tier routers by attribution feasibility.** If
  several customers share a NAT public IP (CGNAT / uncontrolled upstream NAT),
  their flows carry the *same* exporter IP and cannot be told apart from the flow
  alone (ranges don't help — one IP maps to many customers). Options, best to
  worst: (a) a unique in-packet tag that survives NAT — `observation_domain_id` /
  engine-id — **but MikroTik emits 0 and exposes no setting; confirm on current
  ROS whether it's settable, as it'd be the clean fix**; (b) ride flow export over
  the router's **management tunnel** (WireGuard/IPsec) so the source is the
  unique, stable inner IP — the flow analogue of the metrics token, robust but
  heavier; (c) per-router destination port (doesn't scale); (d) **exclude** pure-
  CGNAT sites from per-customer flow (site-level aggregate only). Tag each router
  `public-distinct` / `multi-uplink` / `cgnat`; fleet rollout enables flow only
  where it's attributable.
- **Per-client breakdown works even WAN-only**, because the IPFIX template
  exports both `src-address` (pre-NAT internal client) and `nat-src-address`
  (post-NAT public) in the same record. (Confirmed in both v6 and v7 templates.)

**Content-provider enrichment** (turns destination IPs into names):
- The flow record carries only the raw destination IP — no ASN, no provider,
  no hostname; the CPE has no BGP table and can't supply one. Producing "top
  content providers" **is** this enrichment; there's no shortcut that yields
  provider names without an IP→provider map.
- Mechanism: the free **MaxMind GeoLite2-ASN** dataset (IP-range → AS number +
  org) loaded as a **ClickHouse dictionary**, resolved at **query time**
  (`dictGetString(asn_dict, 'org', dst_ip)`). A data file + dictionary
  definition + monthly refresh — **not a new container, no BGP feed, no DPI**,
  and not a reason to reconsider Akvorado.
- Granularity ceiling: for content owners with their own ASN (Google, Netflix,
  Meta, TikTok, …) the ASN ≈ the service. Traffic behind a shared CDN
  (Cloudflare/AWS/Fastly) resolves to the CDN, not the site — inherent, and
  fine for this use case. Free tier is adequate; the big content ASNs are the
  well-covered ones.
- **ASN dataset is required; GeoLite2-Country is optional** and currently NOT
  planned — it was only needed for a domestic/international split, which is
  deliberately excluded (see Report integration).

## Data model & retention

| Table | Contents | TTL | Queried by |
|---|---|---|---|
| `flows_raw` (MergeTree) | decoded flow records, sampling-scaled | **2–7 days** (rolling buffer) | ad-hoc drill-down only |
| `flows_topn_hourly` (MV → AggregatingMergeTree) | hourly top-N by internal talker and by destination IP/ASN per (customer, exporter) | **~13 months** (covers report YTD) | reports + dashboards |
| `asn_dict` (dictionary, not a table) | GeoLite2-ASN IP-range → AS number + org name | refreshed monthly | query-time provider resolution |

Reports and dashboards run **entirely off the aggregate MVs**, which stay tiny
at any scale. Raw is a short forensic buffer only. Byte figures in the
aggregates are the sampling-scaled estimates. Provider names are resolved at
query time from `asn_dict`, so raw/aggregate rows store only IPs/ASNs and a
dataset refresh re-labels history automatically.

## Report integration

Reuse the existing Grafana-panel-into-PDF path (same mechanism as the per-router
ping graphs, panel 105 rendered via `d-solo`): build two ClickHouse-backed
panels on the customer dashboard — **Top Content Providers (by network)** (ASN
org name via `asn_dict`, sampling-scaled bytes) and **Top Internal Users** (by
`src-address`, the heaviest devices/clients) — then repeat-render them into
`report_template.html`. Minimal new code in `report_lib`, no new query engine in
the reporter container. (Alternative if a native paginating HTML table is
preferred, like the AP table: query ClickHouse directly from `report_lib` with
`clickhouse-connect`. Start with the Grafana-render path.)

**Deliberately excluded from the customer report:** specific hostnames/URLs
(flow can't see them — encrypted, CDN-fronted, no SNI; and not wanted) and the
domestic/international transit split (a gmedia peering/cost metric, not
customer-facing). The ASN data would make that split near-free in a separate
*internal* ops view later if ever wanted — but it stays out of the customer
deliverable.

---

## Phases

### Phase 0 — De-risk on ONE router  ⟵ hard gate, nothing proceeds until green

Stand up `clickhouse` + `goflow2` + `flow-inserter` (sibling compose). Enable
`/ip traffic-flow` on **one pilot router** with all four levers + the
obs-domain tag, targeting the VM. Prove:

- [ ] Flows decode and land in `flows_raw`.
- [ ] **Attribution:** exporter→customer sync resolves the pilot router to the
      correct `customer_id` (and the obs-domain tag is carried through).
- [ ] **Sampling math — the new make-or-break gate:** confirm MikroTik
      advertises the sampling rate to goflow2 (IPFIX options template). If it
      doesn't, configure the rate per-exporter. Verify byte estimates scale
      correctly (sampled ×rate ≈ interface-counter total for the same window).
- [ ] Confirm the `sampling-interval`/`sampling-space` ratio semantics for the
      ROS version (don't trust a formula).
- [ ] **Volume:** measure actual flows/sec on the pilot, and ideally on one
      big site, to extrapolate toward 500 (drives all sizing).
- [ ] Eyeball that both `src-address` (internal) and `nat-src-address` (public)
      are populated → per-client breakdown is real.
- [ ] Verify `/ip traffic-flow` isn't device-mode-gated on a 7.17+ CPE (core
      routing feature, shouldn't be, but check — cf. scheduler/fetch locks).

Gate: attribution correct **and** sampling math correct **and** volume measured.

**Phase 0 results — canary Ro-Agregat-1 (Reseller Wahana), 2026-07-23, unsampled:**
- **Rate: 207 flows/s** on this single aggregation router (the heavy end of the
  fleet) — concretely confirms sampling is the right scale lever.
- **Exporter IP is the NAT egress IP** (`111.68.29.39`), distinct from the
  router's `mgmt_host` (`58.145.168.153`) — expected, not a bug. **Attribution
  keys on the flow exporter (NAT) IP**, captured per router; do NOT reuse
  mgmt_host, and don't assume the ingestion-push source IP matches it either.
- **`observation_domain_id` = 0** — MikroTik doesn't tag it, so the planned
  CGNAT disambiguator isn't available out of the box; exporter IP is the key,
  fine for distinct-IP customers.
- **WAN-only validated decisively:** 99.9% of flows cross the uplink (34k down +
  21k up), 0.1% intra-LAN, zero transit.
- **Per-client visible directly** in `src_addr`/`dst_addr` (MikroTik exports the
  conntrack pre-NAT addresses). goflow2 does NOT surface MikroTik's `nat-*`
  fields — and we don't need them.
- **`src_as`/`dst_as` = 0** (CPE has no BGP table) → ASN enrichment required.
- **On-net CDN nuance:** the reseller's top destinations are gmedia's own
  on-net Google/Meta caches (`43.245.187.x`, `112.78.36.x`), which resolve to
  **AS55666 GMEDIA** — correct but hides that it's Google/Meta content served
  from local cache. "Top content providers" needs a **curated override**
  (`cdn_override` table) for known cache ranges; off-net resolves correctly
  (Meta AS32934, Akamai AS20940, …).
- Sampling-rate advertisement gate — **CLOSED (2026-07-24): not advertised**
  (`sampling_rate` stayed 0), and byte scaling is empirical-not-×rate anyway
  (works on the v6.48.6 canary despite the doc's "v7-only"; see lever 2). Short
  raw TTL, not sampling, is the storage lever.

Enrichment uses the free **iptoasn.com** dataset (no license key), not MaxMind.

### Phase 1 — Storage shape + one-customer dashboard

- Load the **GeoLite2-ASN dictionary** (`asn_dict`) into ClickHouse + a monthly
  refresh; sanity-check that top destination IPs resolve to sensible providers.
- Add the `flows_topn_hourly` materialized views (top-N by internal talker and
  by destination ASN per customer/exporter) and the TTLs.
- Wire the Grafana ClickHouse datasource (`grafana-clickhouse-datasource`).
- Build the **Top Content Providers** + **Top Internal Users** panels for the
  pilot customer; validate aggregates read sensibly and match rough expectation.

### Phase 2 — Report integration

- Add the panel to the customer dashboard; repeat-render it into
  `report_template.html` (same path as panel 105).
- Generate the pilot customer's Monthly Report with the new section; review
  output (WeasyPrint, Montserrat, pagination, WIB windows).
- Gate: report section looks right and numbers are sane.

### Phase 3 — Fleet rollout

- Add the traffic-flow step to `deploy_lib`: WAN-list from schema, `cache-entries`
  per router class (v6 64k / v7 1M default), trimmed IPFIX template. (No
  per-router obs-domain — MikroTik emits 0, not settable.) Optional
  `packet-sampling` only on the busiest routers as a CPU/ingest reducer (NOT for
  storage; byte figures become approximate — see lever 2).
- **Attribution before enabling**: register each router's exporter IP range-set
  in Postgres (`router_flow_exporters`) and tier it `public-distinct` /
  `multi-uplink` / `cgnat`; enable flow only where attributable, and stand up the
  exporter_map sync + "unattributed IP" learn-and-flag (see Per-customer
  attribution).
- **Shorten `flows_raw` TTL to 2–3 days** (`ALTER TABLE flows_raw MODIFY TTL
  ts + INTERVAL 3 DAY`) — the primary storage lever at scale. Currently 7 days
  (kept longer while the MV pipeline is young, for re-backfill room + bug
  margin); size the final value to the per-flow lookback the NOC actually wants.
- Ensure off-site backups include the `clickhouse-data` volume.
- Roll out selectively: a few routers first, watch CPE CPU and cache eviction,
  then expand. Skip/soften on the busiest or lowest-RAM CPE as needed.

---

## Scaling to 500 (turn-on-at-N playbook, not build-now)

The design is additive to 500 routers; ClickHouse is more justified at scale,
not less. Build these two now because they're painful to retrofit; treat the
rest as documented triggers:

**Build now (Phase 0):** the range-set attribution model above (painful to
retrofit once you've collected mislabeled data); instrument the pilot to measure
flows/sec.

**Measured on the canary** (one aggregation router = the heavy end, unsampled):
~240 flows/s → **389 MiB compressed / 25 h** in `flows_raw` (~18.5 B/row) ≈
**~2.5 GB per heavy router for the 7-day raw buffer**. The 13-month rollup MVs
for that same router are **~125 KiB** — because they're bounded by *aggregation
cardinality* (routers × providers/users × hours), **not** by flow rate. That
split is the whole storage story:
- **Long-term (13-month MVs):** ~10–15 GB at 500 routers *regardless of
  sampling* — trivial. Sampling only turns the byte counts into scaled estimates.
- **Short-term (7-day raw buffer) + ingest CPU + CPE CPU + collector network:**
  scales with raw flow rate × 500. Unsampled, an all-heavy fleet ≈ **1.25 TB raw
  + ~120k flows/s** (over the VM); a lighter mixed fleet (~50 flows/s avg) ≈
  **150–260 GB raw + ~25k flows/s** (a large fraction of the 428 GB free).

The primary lever is a **short raw TTL**: raw is only a drill-down buffer, the
MVs hold what reports need, and dropping raw from 7 days to **2 days** cuts the
raw buffer ~3.5× — predictably, at full accuracy, on v6 *and* v7. That alone
likely brings a mixed fleet's raw buffer to ~45–75 GB. **Sampling is a secondary
lever** (works on late-v6 too, not just v7 — see lever 2) that reduces CPE CPU +
ingest + network on the busiest routers,
but — per lever 2, tested 2026-07-24 — its storage saving is modest (~3×, not the
sampling factor) and its byte figures are approximate, so it's not what makes
500 routers fit. Net: **short raw TTL for storage; sampling only to spare CPU on
heavy v7 routers.** Neither is needed at canary scale.

**Turn on when the measured rate crosses the threshold:**
- **Buffer (Kafka/Redpanda/NATS)** between collectors and ClickHouse — the
  "no Kafka" call flips here; UDP has no backpressure, so without it a CH
  merge/restart silently drops flows and you can't scale collectors.
- **Horizontal goflow2 workers + UDP drop monitoring** (`netstat -su` receive
  overruns, alarmed alongside the existing data-freshness alert). Silent UDP
  loss is the failure mode that makes flow data quietly lie.
- **Dedicated VM** for the flow stack. The sibling-compose/own-network boundary
  makes this a host move (repoint Grafana at a remote ClickHouse), not a
  redesign.

Note 500 routers stresses all of QXM (TimescaleDB, the single-VM premise, the
Ruijie 5000/day quota that's already the fleet cap) — plan flow capacity as part
of a holistic 500-router pass, not in isolation.

## Resource budget (current fleet)

~2–3 GB RAM (ClickHouse ~1–2, goflow2 + inserter tens of MB each), a few GB/mo
disk with sampling + short raw TTL. Trivial against current headroom (~27 GB
RAM free, 432 GB disk free, 8 vCPU near-idle).

## v6 vs v7 note

The main v6/v7 difference in the traffic-flow step is the `cache-entries`
default (v6 64k / v7 1M — set explicitly per router class). `packet-sampling`
is NOT v6/v7-split as the doc implies: MikroTik docs call it v7-only, but the
v6.48.6 canary samples fine (verify per version). The IPFIX field template is
identical across versions, so the rest (dual NAT-address export, trimmable
fields) applies to both. Milder than the RouterOS *scripts*, where v7 syntax
hard-fails the v6 parser and each file needs a real
split — but the step still branches on version for cache-entries and sampling.

## NAT / masquerade download attribution (2026-07-24 finding + plan)

**Finding (Grand Ambarrukmo, router 7, exporter 119.2.52.140).** The canary
(Ro-Agregat-1) routes without masquerade at the observation point, so both flow
directions carry the pre-NAT private client and "download = dst is a private
client" just works. **Most CPE masquerade their WAN**, and there the download
(reply) direction is recorded with `dst = the router's own public IP` (the
exporter_ip), not the pre-NAT client. Measured: 17.85 GiB / 20 min of download
landed as `dst = exporter_ip`, **0** as `dst = private` → the dashboard showed
upload only. The canary was a misleadingly easy case; the WAN-only design and
the earlier "we don't need the nat-\* fields" note both assumed no NAT.

**Done — #3, totals + providers (shipped 2026-07-24).** `traffic_hourly` and
`provider_hourly` MVs widened "internal side" to *private client **OR** the
exporter's own IP* (`sp2`/`dp2` in `flow/materialized_views.sql`). This recovers
download **volume** (traffic_hourly) and download **providers** (provider_hourly
reads the provider off the external `src`, which is present) for masquerading
routers, with no effect on non-NAT routers. `user_hourly` deliberately kept
strict (`sp != dp`) so a NAT-download is never mis-attributed to the router's own
IP as a "top user". Applied via drop+recreate the two MVs + truncate/backfill
(procedure in the MV file). Verified: Grand Ambarrukmo now shows ~44 GiB/hr
download and real top providers (Meta Cache, Google, Google Cache, Apple, …).

**Plan — #2, per-client download attribution (only remaining gap).** After #3,
the one thing still missing on NATing routers is **which internal client** did
the download — Top Internal Users is upload-only there (and download is ~9× upload,
so that ranking is badly skewed). The client is only in MikroTik's
`nat-dst-address` field, which the trimmed template exports but goflow2 (v2.2.2,
default `-format json`) drops. Steps, gated on the first:

1. **Verify the IPFIX encoding (gating unknown).** Confirm which IE MikroTik uses
   for `nat-dst-address`/`nat-src-address` — standard `postNAT{Source,Destination}
   IPv4Address` (225/226) vs a MikroTik enterprise IE (PEN 14988) — and that the
   field actually carries the un-NAT'd client on **reply** flows. Capture via
   `tcpdump -w` on 4739/udp + decode the template, or run goflow2 with a debug/
   custom mapping and eyeball. If it's an enterprise IE or unpopulated on reply,
   #2 isn't viable as-is and CGNAT-style fallbacks apply.
2. **Surface it in goflow2** via a custom mapping file (`-mapping mapping.yaml`,
   supported in v2.2.2) mapping the NAT IEs to JSON fields.
3. **Extend the pipeline:** `inserter.py` extracts `nat_src_addr`/`nat_dst_addr`;
   add the two columns to `flows_raw` (small; a migration-equivalent CREATE change
   + note the storage bump against the short raw TTL).
4. **Use it in `user_hourly`:** for a NAT-download flow (`dst = exporter_ip`), the
   internal client = `nat_dst_addr`; keep `src_addr` for normal/non-NAT rows.
   Re-backfill user_hourly.
5. Watch storage — two more String columns per raw row; reconfirm the raw-TTL math.

**Is #2 needed?** Yes for accurate per-client Top Users on masquerading routers
(the majority). It's a bigger change than #3 and blocked on step 1's live
verification, so it's staged separately, not a blocker for rollout — totals and
providers are already correct fleet-wide.

## Open questions

1. ~~Does MikroTik advertise the sampling rate?~~ **Answered 2026-07-24: no**
   (`sampling_rate` = 0), and byte estimates can't be recovered by ×rate anyway
   (samples packets but records full conntrack counts) — calibrate empirically
   if ever used. (Sampling works on the v6.48.6 canary despite the doc's
   "v7-only".) See lever 2 / Scaling.
2. ~~`sampling-interval`/`sampling-space` semantics?~~ **Answered:** sample
   `interval` consecutive packets, skip `space`, repeat → packet fraction
   `interval/(interval+space)`; but flow-count/byte reduction is far milder
   (~3× for a 1/100 packet config) and distribution-dependent.
3. Per-router flows/sec across representative site sizes — canary (aggregation
   router) = ~240/s; still need the fleet distribution — **drives sizing.**
4. Can MikroTik set a unique `observation_domain_id`/engine-id? (would rescue
   CGNAT attribution — see Per-customer attribution.) Emits 0 by default.
