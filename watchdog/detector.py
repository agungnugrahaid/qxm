"""detector.py -- derives service incidents into the `incidents` table.

Phase A: the two kinds that mean "down".

  router_unreachable  the router stopped pushing metrics
  internet_down       the router is up but ALL ping targets are failing

Both are deliberately conservative. A customer told they were down when they
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
             + detect_internet_down(conn, now, since, gaps))
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
