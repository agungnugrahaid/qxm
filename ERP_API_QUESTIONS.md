# QXM → ERP: questions on `/api/v1/customer/report`

Context: QXM generates the customer-facing Network Performance Report (SLA +
ticket pages). Those numbers are entered by hand today; we want to fill them
from the ERP instead. We surveyed the endpoint on 2026-08-12 against
`year=2026` (8,052 records) and the unfiltered response (73,617 records,
2018-08 → 2026-08). The questions below are ordered by how much they block us.

Everything here is about **one endpoint**, since `/api/v1/customer`,
`/api/v1/customers`, `/api/v1/service` and `/api/v1/sla` all return 404 —
please confirm that `/api/v1/customer/report` is the entire API surface.

---

## Already settled on the QXM side (no answer needed)

- **SLA basis.** Confirmed: availability is derived from `report_type =
  gangguan` windows only. QXM will compute it and store it as an **editable**
  value, so an operator can correct records that are mis-dated (see C1) without
  waiting on an ERP fix. A manual edit is **sticky** — it survives later syncs
  and is flagged as overridden — which is exactly why B4 (do records change
  after we pull them?) matters: we need to show an operator when the ERP has
  since disagreed with their correction.
- **Ticket log scope.** All three `report_type` values — `gangguan`,
  `informasi` and `permintaan` — are printed in the report's ticket log, not
  just faults. Only `gangguan` affects the SLA figure.
- **`month` parameter and ticket number** are being raised separately; see B1
  and A2, kept here for completeness.

## A. Blocking — we cannot generate SLA pages without these

**A1. Is there an immutable internal ID for a customer and for a service?**

We understand `cid_number` / `sid_number` are manually entered and have never
been format-validated. That makes them unsafe as a permanent join key, and we
can see the damage in the data:

- `sid_number = 05.0169.3` appears under **two** different customer codes,
  `05.0169.0223` and `05.0169.0323` — one service billed to two customers.
  Which is correct?
- 835 raw `cid_number` values collapse to 832 once whitespace is stripped
  (one of them ends in a literal tab character).
- 761 of 832 follow `##.####.####`; the rest are `.MAX` (24), 11-digit (20),
  `.GF` (16), `##.C##/####` (5) and `###-##-##` (3).

**What we need:** your database's own primary key — an integer or UUID that
never changes and was never typed by hand — exposed as e.g. `customer_id` and
`service_id` alongside the existing display codes. We would store that as the
permanent link between an ERP customer and a QXM customer.

Related: **can one real customer appear under more than one `cid_number`?**
If so we need to know which codes are aliases, or their history is split.

**A2. Can the payload include the ticket number?**

There is no ticket identifier in the response. Our ticket table needs a stable
key so a re-sync updates rows instead of duplicating them, and the report
prints the number the customer sees. Manual entries currently carry real ERP
numbers such as `332693` — please expose that field.

**A3. How do we enumerate a customer's services?**

A service only appears when it has an incident, so we cannot list what a
customer actually subscribes to. A customer with a perfect month produces zero
rows, which is indistinguishable from a customer we have no data for. UNISA has
at least 5 services (`01.0211`, `-02`, `-03`, `-05`) but only the ones with
incidents show up in a given month.

We also need three fields that are not in the payload at all:

- `customer_name` — there is no name anywhere in the response, so we cannot
  verify that a code maps to the customer we think it does. Given A1, this is
  our only sanity check on the mapping. Cheap for you, high value for us.
- `service_name` — e.g. "IDEA ONE 1000 Mbps", printed in the SLA table
- `node_count` — number of nodes per service, used to weight the SLA total

**Ideal outcome:** a service-list endpoint (`GET /api/v1/customer/{cid}/services`)
returning service id, name, node count and status per customer.

---

## B. Load and correctness of the query

**B1. `month` is ignored — is that intended?**

`?year=2026&month=01` returns a byte-identical response (3,756,865 bytes,
records spanning 2026-01 → 2026-08) to `?year=2026`. We would like `month` to
actually filter, which reduces a daily pull from ~3.6 MB to ~300 KB.

Note also that `?year=2026?month=01` (with a second `?`) returns
**422 "The year does not match the format Y."** — that was our mistake, but a
clearer error would help.

**B2. Can we filter by customer?**

The response contains all ~835 customers and ~1,087 services. QXM monitors
about 21 of them, so we are pulling — and storing — incident text for hundreds
of customers we have no business holding. A `cid_number` filter would fix both
the privacy exposure and the payload size.

**B3. Is there an incremental / "changed since" option?**

Something like `?updated_since=2026-08-11T00:00:00` would let us sync daily
without re-fetching the year each time. If not, we will pull
`?year=<current year>` once a day and reconcile our side — please confirm that
cadence is acceptable for your server.

**B4. Are historical records ever edited after the fact?** *(now important)*

QXM will store SLA as an operator-editable value, so this decides our conflict
rule: if a record can change in the ERP after we have pulled it — a reopened
ticket, a corrected outage window, a back-dated entry — we need to know, or a
re-sync will either silently overwrite a human correction or silently ignore a
genuine ERP fix. If records are edited, a `last_updated` timestamp per record
would let us resolve it properly.

---

## C. Data quality

Counts below are from `year=2026` (8,052 records).

**C1. `report_end` earlier than `report_start` — 65 records.** Plus 11 records
whose timestamps don't parse, and windows as long as 260 days (max observed
6,252 hours). Are these data-entry errors, or do they encode something (e.g.
still-open tickets)? QXM will let an operator correct these locally, but since
they feed a customer-facing SLA figure they are worth fixing at source too —
a validation rule rejecting `report_end < report_start` on entry would stop
new ones appearing.

**C2. How do we tell an open ticket from a closed one?** There is no status
field. Every record has both `report_start` and `report_end`, so we currently
assume all are closed — please confirm, or add a status.

**C3. HTML entity encoding — 177 records.** `report_content` / `report_actions`
contain `&lt;`, `&gt;` etc. (no real HTML tags). We unescape on our side; it
would be cleaner as plain UTF-8.

**C4. Stray whitespace in the keys — 89 records.** Values such as
`" 01.1093.0819 "` are padded, and 182 `report_subject` values contain
non-breaking spaces (U+00A0). We trim defensively, but since `cid_number` is
our join key, padding is risky. Can it be normalised at source?

**C5.** *(merged into A1 — identifier stability is the blocking question.)*

**C6. Please confirm the meaning of `report_type`.** We read them as:
`gangguan` = fault/outage (4,311), `informasi` = information request (2,325),
`permintaan` = change/service request (1,416). We currently count **only
`gangguan`** toward downtime, because the other two also carry start/end
windows but look like handling time, not outage time (median 1.9 min and
9.0 min respectively). Please confirm that is correct.

---

## D. Security and access

**D1. Can this be served over HTTPS?** The endpoint is plain HTTP, so the Basic
Auth credentials cross the network in clear text on every call.

**D2. Can QXM get its own scoped, non-shared credential?** We were given
`admin01@example.com`, which looks like a shared administrative account. We
would prefer a dedicated read-only API user — ideally one restricted to the
customers QXM actually monitors (see B2).

**D3. Is there a rate limit or maintenance window we should respect?** Our
planned pattern is one request per day plus occasional on-demand report
generation.

---

## Summary of what we need to fill the report

| Report field | ERP source today | Status |
|---|---|---|
| Ticket date | `report_date` | ✅ |
| Ticket description | `report_subject` + `report_content` | ✅ |
| Ticket action | `report_actions` | ✅ |
| Ticket MTTR | `report_end − report_start` | ⚠️ 65 negative, 11 unparseable (C1) |
| Ticket type in log | `report_type` | ✅ all three printed |
| Ticket number | — | ❌ A2 |
| Ticket status | — | ❌ C2 |
| Service id | `sid_number` | ⚠️ unvalidated, see A1 |
| Customer identity | `cid_number` | ⚠️ unvalidated, no name — A1 / A3 |
| Service name | — | ❌ A3 |
| Node count | — | ❌ A3 |
| SLA % | derived from `gangguan`, editable in QXM | ✅ settled |
| Full service list | — | ❌ A3 |
