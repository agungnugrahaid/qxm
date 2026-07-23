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

2. **Sampling** (`packet-sampling=yes`) — start conservative (e.g. 1/100), tune
   from the pilot's measured flows/sec. Acceptable because totals come from
   counters and the elephants that dominate a top-talkers report survive
   sampling; the mouse flows we lose wouldn't be listed anyway. This is the
   lever that dissolves the 500-router walls (flows/sec, CPE CPU, storage all
   drop by the sampling factor, and cache pressure with it).

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
generic flow tool):
- Map `exporter_ip → routers → customer_id`. Kept fresh **automatically** from
  the public source IP the ingestion-api already sees on every 5-min metrics
  push — self-heals when a customer's dynamic NAT IP changes.
- Disambiguator for shared/CGNAT public IPs: provision a **unique NetFlow
  observation-domain / engine-id per router** at deploy time and attribute on
  that. Cheap to bake in now, painful to retrofit — so it's in the schema from
  Phase 0 even though 19 routers don't strictly need it.
- Per-**client** breakdown works even WAN-only, because the IPFIX template
  exports both `src-address` (pre-NAT internal client) and `nat-src-address`
  (post-NAT public) in the same record. (Confirmed present in both v6 and v7
  default templates; still a Phase 0 eyeball check.)

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
- Still open: sampling-rate advertisement gate (needs sampling enabled to test).

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

- Add the traffic-flow step to `deploy_lib` (v6/v7 `cache-entries` split;
  WAN-list from schema; sampling params; trimmed template; per-router
  obs-domain). Needs the usual v6/v7 handling — but here it's trivial (one
  number differs), not a syntax split.
- Roll out selectively: a few routers first, watch CPE CPU and cache eviction,
  then expand. Skip/soften on the busiest or lowest-RAM CPE as needed.

---

## Scaling to 500 (turn-on-at-N playbook, not build-now)

The design is additive to 500 routers; ClickHouse is more justified at scale,
not less. Build these two now because they're painful to retrofit; treat the
rest as documented triggers:

**Build now (Phase 0):** obs-domain attribution key in the schema; instrument
the pilot to measure flows/sec.

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

Volume projection is an order-of-magnitude uncertain until the pilot measures —
at 500 routers, raw flows could be anywhere from ~26 to ~100+ GB/day *before*
sampling, which is exactly why sampling + short raw TTL + aggregate-only reports
are baked in. And note 500 routers stresses all of QXM (TimescaleDB, the
single-VM premise, the Ruijie 5000/day quota that's already the fleet cap) —
plan flow capacity as part of a holistic 500-router pass, not in isolation.

## Resource budget (current fleet)

~2–3 GB RAM (ClickHouse ~1–2, goflow2 + inserter tens of MB each), a few GB/mo
disk with sampling + short raw TTL. Trivial against current headroom (~27 GB
RAM free, 432 GB disk free, 8 vCPU near-idle).

## v6 vs v7 note

The ONLY traffic-flow config difference between v6 and v7 defaults is
`cache-entries` (v6 64k / v7 1M). The entire IPFIX field template is identical,
so every finding here (dual NAT-address export, trimmable fields, sampling)
applies equally to both. Unlike the RouterOS *scripts* — where v7 syntax
hard-fails the v6 parser and each file needs a real split — the traffic-flow
step's v6/v7 handling is just "pick the right `cache-entries`."

## Open questions carried into Phase 0

1. Does MikroTik advertise the sampling rate in an IPFIX options template so
   goflow2 auto-scales? (else per-exporter manual rate) — **decides byte-estimate accuracy.**
2. Exact `sampling-interval`/`sampling-space` ratio semantics on the target ROS versions.
3. Per-router flows/sec across representative site sizes — **drives all sizing and the scale triggers.**
