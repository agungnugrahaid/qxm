# ERP → QXM: SLA & Ticket API contract (handover draft)

QXM's Monthly Report PDF includes SLA/availability and ticket sections.
Today those numbers are **entered by hand** in the QXM Console (customer
detail page → "SLA & Tickets" card, stored in `customer_sla_services` /
`customer_tickets`). This document is the contract for the ERP endpoint
that will replace that manual entry — the field names below intentionally
match the manual tables, so switching QXM's report collectors to the ERP
is a drop-in change (the manual tables then remain as a fallback/cache).

## Endpoint

```
GET /api/v1/sla/{customer_external_id}?month=YYYY-MM
Authorization: Bearer <key issued to QXM>
```

- `customer_external_id` — the ERP's own customer code. This is the same
  key QXM stores in `customers.external_id` for the traffic-series
  integration API (see the integration-api plan), so both directions of
  ERP↔QXM traffic use one mapping.
- `month` — calendar month the SLA figures describe (WIB).

## Response — 200

```json
{
  "customer_external_id": "ARKON-0169",
  "month": "2026-06",
  "services": [
    {
      "service_id": "05.0169.3",
      "service_name": "IDEA ONE 30 Mbps",
      "node_count": 1,
      "sla_pct": 100.0
    }
  ],
  "tickets": [
    {
      "ticket_no": "TCK-2026-0612",
      "date": "2026-06-12",
      "description": "Link down - fiber cut",
      "action": "Rerouted + splice repair",
      "mttr_seconds": 5400,
      "status": "closed"
    }
  ]
}
```

Field notes:

- `sla_pct` — monthly uptime percentage for that service, 0–100, up to 3
  decimals. QXM renders per-service rows plus a node-weighted Total.
- `tickets` — every ticket **opened in that month** for the customer;
  empty array when none. `mttr_seconds` may be null while a ticket is
  open; `status` is `open` or `closed`.
- Unknown `customer_external_id` → **404**. Month with no SLA data yet →
  200 with empty `services` (QXM then omits the SLA pages for that month).

## Auth / transport

- Static bearer key issued to QXM, checked on every request; requests
  without or with a wrong key → **401**.
- HTTPS only. QXM calls this at report-generation time (monthly scheduler
  on the 1st + on-demand exports), so expected volume is trivial —
  a few requests per customer per month.
