"""detector.py -- derives service incidents into the `incidents` table.

  router_unreachable  the router stopped pushing metrics          (outage)
  internet_down       router up, but ALL ping targets failing      (outage)
  uplink_down         a WAN interface not running                  (degraded)
  aps_offline         APs offline above the site's own baseline    (degraded)
  dhcp_full           a pool at its ceiling                        (degraded)
  conntrack_full      connection table at its ceiling              (degraded)

Only `outage` counts against availability: a failed-over uplink or a full DHCP
pool is real and worth showing, but the service was still up.

All of it is deliberately conservative. A customer told they were down when they
were not will never trust the panel again, so every threshold errs toward
missing a short blip rather than inventing one.

Two correctness rules earn their keep here:

1. `internet_down` requires EVERY target to fail, not any. Measured over 7
   days, customer 8 had 320 five-minute buckets at >=99% loss to facebook.com
   and cloudflare-dns while google.com and google-dns stayed at 0%. That is a
   per-destination routing problem, not an outage -- an "any target" rule would
   have manufactured 320 outages for one customer in a week.

2. The monitoring-gap guard runs first. Our own collector has frozen for ~2h
   before, and from the data that is indistinguishable from every customer
   going down simultaneously. When a large share of the fleet goes quiet in the
   same bucket we mark the window as OUR gap, so it never counts against a
   customer.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]

# Routers push every ~5 minutes. Two consecutive misses before we call it
# unreachable -- one miss is routine.
STALE_AFTER = timedelta(seconds=int(os.environ.get("INCIDENT_STALE_SECONDS", "780")))  # 13 min
# How far back each pass reconsiders. Long enough to close incidents that
# recovered between passes, short enough to stay cheap.
LOOKBACK = timedelta(hours=int(os.environ.get("INCIDENT_LOOKBACK_HOURS", "6")))
# All-target loss must persist across this many consecutive 5-minute buckets.
MIN_LOSS_BUCKETS = int(os.environ.get("INCIDENT_MIN_LOSS_BUCKETS", "2"))
# A bucket is "our" gap when fewer than this share of the normally-reporting
# routers checked in. Erring high is the safe direction: flagging only marks an
# incident as ours (suppressing it from a customer's availability), it never
# creates one. At 0.5 a real bucket with 9 of ~19 routers reporting fell just
# the wrong side of the boundary.
GAP_RATIO = float(os.environ.get("INCIDENT_GAP_RATIO", "0.6"))
INTERVAL = int(os.environ.get("INCIDENT_INTERVAL_SECONDS", "300"))
# APs offline ABOVE the site's own 24h median. A fixed threshold would open a
# permanent incident on an estate that runs ~98 APs offline as its normal state.
APS_OFFLINE_DELTA = int(os.environ.get("INCIDENT_APS_OFFLINE_DELTA", "3"))
DHCP_FULL_PCT = float(os.environ.get("INCIDENT_DHCP_FULL_PCT", "99"))
CONNTRACK_FULL_PCT = float(os.environ.get("INCIDENT_CONNTRACK_FULL_PCT", "95"))


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def q(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur.fetchall()


# --- monitoring-gap guard ----------------------------------------------------

def monitoring_gap_buckets(conn, since):
    """5-minute buckets where most of the fleet went quiet at once -> ours.

    Baseline is the median per-bucket reporting count over the window, which is
    robust to a couple of routers being legitimately down.
    """
    rows = q(conn, """
        SELECT time_bucket('5 minutes', time) AS b, count(DISTINCT router_id) AS n
        FROM router_metrics WHERE time >= %s GROUP BY 1 ORDER BY 1
    """, (since,))
    if len(rows) < 4:
        return set()
    counts = sorted(r["n"] for r in rows)
    median = counts[len(counts) // 2]
    if median <= 0:
        return set()
    return {r["b"] for r in rows if r["n"] < median * GAP_RATIO}


def in_gap(gaps, start, end):
    """True if any monitoring-gap bucket overlaps [start, end]."""
    return any(start <= b <= end for b in gaps)


# --- detectors ---------------------------------------------------------------

def detect_router_unreachable(conn, now, since, gaps):
    """Gaps in the metrics push, plus a still-open gap at the tail."""
    found = []
    routers = q(conn, """
        SELECT r.id, r.customer_id, r.identity_name
        FROM routers r WHERE r.customer_id IS NOT NULL
    """)
    for r in routers:
        times = [x["time"] for x in q(conn, """
            SELECT time FROM router_metrics
            WHERE router_id = %s AND time >= %s ORDER BY time
        """, (r["id"], since))]
        if not times:
            continue      # never reported in the window; not evidence of a new outage
        # closed gaps between consecutive pushes
        for prev, nxt in zip(times, times[1:]):
            if nxt - prev > STALE_AFTER:
                found.append({
                    "customer_id": r["customer_id"], "router_id": r["id"],
                    "kind": "router_unreachable", "severity": "outage",
                    "started_at": prev, "ended_at": nxt,
                    "detail": {"router": r["identity_name"],
                               "gap_seconds": int((nxt - prev).total_seconds())},
                    "monitoring_gap": in_gap(gaps, prev, nxt),
                })
        # still open?
        if now - times[-1] > STALE_AFTER:
            found.append({
                "customer_id": r["customer_id"], "router_id": r["id"],
                "kind": "router_unreachable", "severity": "outage",
                "started_at": times[-1], "ended_at": None,
                "detail": {"router": r["identity_name"],
                           "gap_seconds": int((now - times[-1]).total_seconds())},
                "monitoring_gap": in_gap(gaps, times[-1], now),
            })
    return found


def detect_internet_down(conn, now, since, gaps):
    """Every ping target failing at once, for MIN_LOSS_BUCKETS in a row.

    Placeholder rows (rtt 0 / loss 100) come from deploy-time manual script
    runs -- the documented scheduler-only ping quirk -- and are excluded, or
    every deploy would look like an outage.
    """
    found = []
    rows = q(conn, """
        SELECT r.id AS router_id, r.customer_id, r.identity_name,
               time_bucket('5 minutes', pm.time) AS b,
               count(*) FILTER (WHERE pm.packet_loss_pct >= 99) AS lost,
               count(*) AS total,
               array_agg(DISTINCT pm.target_name) AS targets
        FROM path_metrics pm JOIN routers r ON r.id = pm.router_id
        WHERE pm.time >= %s AND r.customer_id IS NOT NULL
          AND NOT (pm.rtt_avg_ms = 0 AND pm.packet_loss_pct = 100)
        GROUP BY 1, 2, 3, 4 ORDER BY 1, 4
    """, (since,))

    by_router = {}
    for row in rows:
        by_router.setdefault(row["router_id"], []).append(row)

    for router_id, buckets in by_router.items():
        run = []
        for b in buckets:
            # need at least 2 targets to call it "all targets"
            all_down = b["total"] >= 2 and b["lost"] == b["total"]
            if all_down:
                run.append(b)
                continue
            if len(run) >= MIN_LOSS_BUCKETS:
                found.append(_loss_incident(run, ended=b["b"], now=now, gaps=gaps))
            run = []
        if len(run) >= MIN_LOSS_BUCKETS:
            # still failing at the end of the window -> open
            found.append(_loss_incident(run, ended=None, now=now, gaps=gaps))
    return found


def _loss_incident(run, ended, now, gaps):
    first, last = run[0], run[-1]
    return {
        "customer_id": first["customer_id"], "router_id": first["router_id"],
        "kind": "internet_down", "severity": "outage",
        "started_at": first["b"], "ended_at": ended,
        "detail": {"router": first["identity_name"],
                   "targets": list(last["targets"]),
                   "buckets": len(run)},
        "monitoring_gap": in_gap(gaps, first["b"], ended or now),
    }


def _runs(buckets, key, min_len, now, gaps, build):
    """Collapse consecutive flagged buckets into incidents.

    `buckets` is ordered; `key(b)` says whether the condition holds. A run of at
    least `min_len` becomes one incident via `build(run, ended)`.
    """
    found, run = [], []
    for b in buckets:
        if key(b):
            run.append(b)
            continue
        if len(run) >= min_len:
            found.append(build(run, b["b"]))
        run = []
    if len(run) >= min_len:
        found.append(build(run, None))
    return found


def detect_uplink_down(conn, now, since, gaps):
    """A WAN interface reporting not-running. On a multi-WAN customer this is
    degraded rather than an outage -- the point is that the customer can SEE the
    backup carried them, which is invisible today. If the last WAN also fails,
    internet_down covers it."""
    uplinks = {(r["router_id"], r["interface_name"]) for r in q(conn, """
        SELECT DISTINCT r.id AS router_id, u.interface_name
          FROM uplink_metrics u JOIN routers r ON r.id = u.router_id
         WHERE u.time >= %s AND u.interface_name IS NOT NULL
        UNION SELECT id, wan_interface FROM routers WHERE wan_interface IS NOT NULL
        UNION SELECT id, wan_interface_backup FROM routers WHERE wan_interface_backup IS NOT NULL
    """, (since,))}
    if not uplinks:
        return []

    rows = q(conn, """
        SELECT r.id AS router_id, r.customer_id, r.identity_name,
               im.interface_name, time_bucket('5 minutes', im.time) AS b,
               bool_and(NOT im.running AND NOT im.disabled) AS down
        FROM interface_metrics im JOIN routers r ON r.id = im.router_id
        WHERE im.time >= %s AND r.customer_id IS NOT NULL
        GROUP BY 1,2,3,4,5 ORDER BY 1,4,5
    """, (since,))

    found = []
    by_iface = {}
    for r in rows:
        if (r["router_id"], r["interface_name"]) in uplinks:
            by_iface.setdefault((r["router_id"], r["interface_name"]), []).append(r)
    for (_, iface), buckets in by_iface.items():
        def build(run, ended, iface=iface):
            f = run[0]
            return {"customer_id": f["customer_id"], "router_id": f["router_id"],
                    "kind": "uplink_down", "severity": "degraded",
                    "started_at": f["b"], "ended_at": ended,
                    "detail": {"router": f["identity_name"], "interface": iface},
                    "monitoring_gap": in_gap(gaps, f["b"], ended or now)}
        found += _runs(buckets, lambda b: b["down"], MIN_LOSS_BUCKETS, now, gaps, build)
    return found


def detect_aps_offline(conn, now, since, gaps):
    """APs newly offline at a site, measured against that site's own 24h median.

    A fixed threshold is wrong here: one estate runs ~98 APs permanently
    offline, which would open an incident that never closes and drown the real
    ones. Comparing to the site's own baseline detects a NEW failure instead of
    a chronic state.
    """
    rows = q(conn, """
        WITH per_bucket AS (
          SELECT s.customer_id, s.id AS site_id,
                 COALESCE(NULLIF(s.site_desc,''), s.unifi_site_name) AS site,
                 time_bucket('5 minutes', a.time) AS b,
                 count(*) FILTER (WHERE a.state IS DISTINCT FROM 1) AS offline,
                 count(*) AS total
          FROM ap_inventory a JOIN sites s ON s.id = a.site_id
          WHERE a.time >= %s AND s.customer_id IS NOT NULL
          GROUP BY 1,2,3,4
        ),
        -- percentile_cont is an ordered-set aggregate and cannot be used as a
        -- window function, so the per-site baseline is its own CTE.
        base AS (
          SELECT site_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY offline) AS baseline
          FROM per_bucket GROUP BY site_id
        )
        SELECT p.*, b.baseline FROM per_bucket p
        JOIN base b ON b.site_id = p.site_id
        ORDER BY p.site_id, p.b
    """, (since,))
    by_site = {}
    for r in rows:
        by_site.setdefault(r["site_id"], []).append(r)

    found = []
    for site_id, buckets in by_site.items():
        def build(run, ended):
            f = run[0]
            peak = max(b["offline"] for b in run)
            return {"customer_id": f["customer_id"], "router_id": None,
                    "kind": "aps_offline", "severity": "degraded",
                    "started_at": f["b"], "ended_at": ended,
                    "detail": {"site": f["site"], "ap_count": int(peak), "site_total": int(f["total"] or 0),
                               "baseline": int(f["baseline"] or 0)},
                    "monitoring_gap": in_gap(gaps, f["b"], ended or now)}
        found += _runs(
            buckets,
            # Scale the trigger with estate size: +3 offline is a crisis on a
            # 6-AP site and noise on a 322-AP one.
            lambda b: b["offline"] >= (b["baseline"] or 0) + max(
                APS_OFFLINE_DELTA, round(0.05 * (b["total"] or 0))),
            MIN_LOSS_BUCKETS, now, gaps, build)
    return found


def detect_capacity(conn, now, since, gaps):
    """DHCP pools and the connection table hitting their ceiling. Neither takes
    the link down, but both stop NEW connections -- which reaches the customer
    as "the wifi stopped working" for anyone arriving."""
    found = []

    dhcp = q(conn, """
        SELECT r.customer_id, r.id AS router_id, r.identity_name, d.pool_name,
               time_bucket('5 minutes', d.time) AS b, max(d.utilization_pct) AS pct
        FROM dhcp_pool_metrics d JOIN routers r ON r.id = d.router_id
        WHERE d.time >= %s AND r.customer_id IS NOT NULL
        GROUP BY 1,2,3,4,5 ORDER BY 2,4,5
    """, (since,))
    by_pool = {}
    for r in dhcp:
        by_pool.setdefault((r["router_id"], r["pool_name"]), []).append(r)
    for (_, pool), buckets in by_pool.items():
        def build(run, ended, pool=pool):
            f = run[0]
            return {"customer_id": f["customer_id"], "router_id": f["router_id"],
                    "kind": "dhcp_full", "severity": "degraded",
                    "started_at": f["b"], "ended_at": ended,
                    "detail": {"router": f["identity_name"], "pool": pool,
                               "peak_pct": float(max(b["pct"] or 0 for b in run))},
                    "monitoring_gap": in_gap(gaps, f["b"], ended or now)}
        found += _runs(buckets, lambda b: (b["pct"] or 0) >= DHCP_FULL_PCT,
                       MIN_LOSS_BUCKETS, now, gaps, build)

    ct = q(conn, """
        SELECT r.customer_id, r.id AS router_id, r.identity_name,
               time_bucket('5 minutes', m.time) AS b,
               max(100.0 * m.conntrack_count / NULLIF(m.conntrack_max, 0)) AS pct
        FROM router_metrics m JOIN routers r ON r.id = m.router_id
        WHERE m.time >= %s AND r.customer_id IS NOT NULL AND m.conntrack_max > 0
        GROUP BY 1,2,3,4 ORDER BY 2,4
    """, (since,))
    by_router = {}
    for r in ct:
        by_router.setdefault(r["router_id"], []).append(r)
    for buckets in by_router.values():
        def build(run, ended):
            f = run[0]
            return {"customer_id": f["customer_id"], "router_id": f["router_id"],
                    "kind": "conntrack_full", "severity": "degraded",
                    "started_at": f["b"], "ended_at": ended,
                    "detail": {"router": f["identity_name"],
                               "peak_pct": round(float(max(b["pct"] or 0 for b in run)), 1)},
                    "monitoring_gap": in_gap(gaps, f["b"], ended or now)}
        found += _runs(buckets, lambda b: (b["pct"] or 0) >= CONNTRACK_FULL_PCT,
                       MIN_LOSS_BUCKETS, now, gaps, build)
    return found


# --- persistence -------------------------------------------------------------

def upsert(conn, inc):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO incidents (customer_id, router_id, kind, severity,
                               started_at, ended_at, detail, monitoring_gap)
        VALUES (%(customer_id)s, %(router_id)s, %(kind)s, %(severity)s,
                %(started_at)s, %(ended_at)s, %(detail)s, %(monitoring_gap)s)
        ON CONFLICT (customer_id, kind, COALESCE(router_id, -1), started_at)
        DO UPDATE SET ended_at = EXCLUDED.ended_at,
                      detail = EXCLUDED.detail,
                      monitoring_gap = EXCLUDED.monitoring_gap,
                      updated_at = now()
        RETURNING (xmax = 0) AS inserted
    """, {**inc, "detail": json.dumps(inc["detail"])})
    return cur.fetchone()["inserted"]


def run_once(conn):
    now = datetime.now(timezone.utc)
    since = now - LOOKBACK
    gaps = monitoring_gap_buckets(conn, since)
    found = (detect_router_unreachable(conn, now, since, gaps)
             + detect_internet_down(conn, now, since, gaps)
             + detect_uplink_down(conn, now, since, gaps)
             + detect_aps_offline(conn, now, since, gaps)
             + detect_capacity(conn, now, since, gaps))
    new = sum(1 for inc in found if upsert(conn, inc))
    conn.commit()
    open_now = q(conn, "SELECT count(*) AS n FROM incidents WHERE ended_at IS NULL")[0]["n"]
    flagged = sum(1 for inc in found if inc["monitoring_gap"])
    print(f"[incidents] window={LOOKBACK} seen={len(found)} new={new} "
          f"open={open_now} monitoring_gap={flagged} gap_buckets={len(gaps)}", flush=True)
    return found


def main():
    print(f"[incidents] detector starting, every {INTERVAL}s "
          f"(stale_after={STALE_AFTER}, min_loss_buckets={MIN_LOSS_BUCKETS})", flush=True)
    while True:
        try:
            conn = get_conn()
            run_once(conn)
            conn.close()
        except Exception as exc:
            print(f"[incidents] pass failed: {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
