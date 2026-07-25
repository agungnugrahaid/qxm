"""Sync ClickHouse flow.exporter_map from Postgres (the source of truth).

Reads router_flow_exporters JOIN routers JOIN customers, expands each CIDR to
concrete exporter IPs, and rewrites flow.exporter_map so flow attribution always
reflects what's in the Console -- no hand-editing ClickHouse. Also surfaces
exporter IPs seen in flows_raw that aren't attributed to any customer
("learn-and-flag": catches new/backup uplink IPs that only appear on failover).
Loops on SYNC_INTERVAL.
"""
import os
import json
import time
import ipaddress
import statistics
import urllib.request

import psycopg2
import psycopg2.extras
import clickhouse_connect

DATABASE_URL = os.environ["DATABASE_URL"]
INTERVAL = int(os.environ.get("SYNC_INTERVAL", "300"))
MAX_EXPAND = int(os.environ.get("MAX_EXPAND", "1024"))  # skip absurdly large CIDRs

# --- per-client abuse detection (scan_abuse) ---------------------------------
# A client is flagged when its estimated (sampling-scaled) connection rate clears
# an absolute FLOOR *and* is a large multiple (K) of its router's median client
# -- adaptive so a busy-but-normal NAT doesn't false-positive and a quiet router
# still trips on a real flood. See FLOW_COLLECTION_PLAN.md.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
ABUSE_WINDOW_MIN = int(os.environ.get("ABUSE_WINDOW_MIN", "10"))       # look-back
ABUSE_FLOOR_CONN_RATE = float(os.environ.get("ABUSE_FLOOR_CONN_RATE", "200"))  # est conn/s
ABUSE_MULTIPLE_K = float(os.environ.get("ABUSE_MULTIPLE_K", "10"))     # x router median


def ch_client():
    return clickhouse_connect.get_client(
        host=os.environ.get("CH_HOST", "clickhouse"),
        username=os.environ.get("CH_USER", "flow"),
        password=os.environ.get("CH_PASS", "flowpass"),
        database=os.environ.get("CH_DB", "flow"),
    )


def sync_once():
    pg = psycopg2.connect(DATABASE_URL)
    pg.cursor_factory = psycopg2.extras.RealDictCursor
    cur = pg.cursor()
    cur.execute(
        """
        SELECT rfe.cidr, r.customer_id, c.name AS customer_name
        FROM router_flow_exporters rfe
        JOIN routers r   ON r.id = rfe.router_id
        JOIN customers c ON c.id = r.customer_id
        """
    )
    rows = cur.fetchall()
    pg.close()

    mapping = {}   # exporter_ip -> (customer_id, customer_name)
    skipped = []
    for row in rows:
        try:
            net = ipaddress.ip_network(row["cidr"].strip(), strict=False)
        except ValueError:
            skipped.append(row["cidr"])
            continue
        if net.num_addresses > MAX_EXPAND:
            skipped.append(f"{row['cidr']} (>{MAX_EXPAND} addrs)")
            continue
        for ip in net:
            mapping[str(ip)] = (row["customer_id"], row["customer_name"])

    ch = ch_client()
    # Prepare data first, then truncate+insert so the empty window is minimal.
    data = [[ip, cid, name] for ip, (cid, name) in mapping.items()]
    ch.command("TRUNCATE TABLE flow.exporter_map")
    if data:
        ch.insert("exporter_map", data,
                  column_names=["exporter_ip", "customer_id", "customer_name"])

    # learn-and-flag: exporter IPs seen in the last 24h not covered by the map.
    unattributed = ch.query(
        "SELECT exporter_ip, count() AS n, min(ts) AS first_seen, max(ts) AS last_seen "
        "FROM flows_raw WHERE ts > now()-86400 "
        "AND exporter_ip NOT IN (SELECT exporter_ip FROM exporter_map) "
        "GROUP BY exporter_ip ORDER BY n DESC"
    ).result_rows

    msg = f"[sync] exporter_map: {len(mapping)} IP(s) from {len(rows)} PG row(s)"
    if skipped:
        msg += f"; skipped {len(skipped)}: {skipped}"
    print(msg, flush=True)
    if unattributed:
        print(f"[sync] UNATTRIBUTED exporter IPs seen in last 24h "
              f"({len(unattributed)}) -- assign each to a router in Postgres:", flush=True)
        for ip, n, first_seen, last_seen in unattributed:
            print(f"[sync]   {ip}  ({n} flows, {first_seen} .. {last_seen})", flush=True)
    else:
        print("[sync] no unattributed exporter IPs", flush=True)


def post_webhook(text, fields):
    """Best-effort JSON POST to WEBHOOK_URL. `text` is a Slack-compatible summary
    line; `fields` carries the structured incident. Never raises."""
    if not WEBHOOK_URL:
        return
    payload = {"text": text, **fields}
    try:
        req = urllib.request.Request(
            WEBHOOK_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:
        print(f"[abuse] webhook POST failed: {e}", flush=True)


def _exporter_router_map(cur):
    """exporter_ip -> router info, built from Postgres (source of truth), for
    flow-enabled routers only. Carries the configured sampling ratio so the
    detector can scale observed rates back up on sampled routers."""
    cur.execute(
        """
        SELECT rfe.cidr, r.id AS router_id, r.customer_id, r.identity_name,
               r.flow_sampling_interval AS si, r.flow_sampling_space AS sp
        FROM router_flow_exporters rfe
        JOIN routers r ON r.id = rfe.router_id
        WHERE r.flow_enabled = true
        """
    )
    out = {}
    for row in cur.fetchall():
        try:
            net = ipaddress.ip_network(row["cidr"].strip(), strict=False)
        except ValueError:
            continue
        if net.num_addresses > MAX_EXPAND:
            continue
        si = row["si"] or 0
        factor = (si + (row["sp"] or 0)) / si if si and si > 0 else 1.0
        info = {"router_id": row["router_id"], "customer_id": row["customer_id"],
                "identity": row["identity_name"], "factor": float(factor)}
        for ip in net:
            out[str(ip)] = info
    return out


def scan_abuse():
    """Flag internal clients whose connection/packet rate is abnormal for their
    router -- the conntrack-exhaustion pattern (Grand Ambarrukmo 10.100.99.147,
    ~5,300 SYN/s). Reads the per-minute client_minute rollup, applies an adaptive
    +floor threshold (sampling-aware), and records/upserts incidents into
    Postgres flow_abuse_events, alerting once per (client, hour) via webhook."""
    pg = psycopg2.connect(DATABASE_URL)
    pg.cursor_factory = psycopg2.extras.RealDictCursor
    cur = pg.cursor()
    router_map = _exporter_router_map(cur)
    if not router_map:
        pg.close()
        return

    ch = ch_client()
    rows = ch.query(
        "SELECT exporter_ip, client_ip, max(flows) AS peak_flows, "
        "max(packets) AS peak_pkts, sum(flows) AS sum_flows, sum(syn_like) AS sum_syn "
        "FROM client_minute WHERE minute > now() - toIntervalMinute(%d) "
        "GROUP BY exporter_ip, client_ip" % ABUSE_WINDOW_MIN
    ).result_rows

    # group per exporter so the adaptive baseline is per-router
    per_exporter = {}
    for exporter_ip, client_ip, peak_flows, peak_pkts, sum_flows, sum_syn in rows:
        if exporter_ip in router_map:
            per_exporter.setdefault(exporter_ip, []).append(
                (client_ip, int(peak_flows), int(peak_pkts), int(sum_flows), int(sum_syn)))

    flagged = 0
    for exporter_ip, clients in per_exporter.items():
        info = router_map[exporter_ip]
        factor = info["factor"]
        median_peak = statistics.median([c[1] for c in clients]) or 0
        for client_ip, peak_flows, peak_pkts, sum_flows, sum_syn in clients:
            est_conn_rate = (peak_flows / 60.0) * factor
            est_pps = (peak_pkts / 60.0) * factor
            # adaptive test uses RAW peaks (ranking preserved under sampling);
            # floor uses the sampling-scaled estimate.
            if est_conn_rate < ABUSE_FLOOR_CONN_RATE:
                continue
            if peak_flows < ABUSE_MULTIPLE_K * median_peak:
                continue
            syn_ratio = sum_syn / sum_flows if sum_flows else 0.0
            _record_incident(cur, info, exporter_ip, client_ip,
                             est_conn_rate, est_pps, syn_ratio, factor)
            flagged += 1
    pg.commit()
    pg.close()
    print(f"[abuse] scanned {len(per_exporter)} exporter(s), {flagged} client(s) over threshold",
          flush=True)


def _record_incident(cur, info, exporter_ip, client_ip,
                    est_conn_rate, est_pps, syn_ratio, factor):
    """Upsert one rolling incident per (router, client, hour); alert on the first
    sighting in that hour (or a retry if a prior notify failed)."""
    cur.execute(
        """
        INSERT INTO flow_abuse_events
          (router_id, customer_id, internal_ip, incident_hour,
           peak_conn_rate, peak_pps, syn_ratio, sampling_factor)
        VALUES (%s, %s, %s, date_trunc('hour', now()), %s, %s, %s, %s)
        ON CONFLICT (router_id, internal_ip, incident_hour) DO UPDATE SET
          last_seen       = now(),
          peak_conn_rate  = greatest(flow_abuse_events.peak_conn_rate, EXCLUDED.peak_conn_rate),
          peak_pps        = greatest(flow_abuse_events.peak_pps, EXCLUDED.peak_pps),
          syn_ratio       = EXCLUDED.syn_ratio,
          sampling_factor = EXCLUDED.sampling_factor
        RETURNING id, (xmax = 0) AS inserted, notified
        """,
        (info["router_id"], info["customer_id"], client_ip,
         est_conn_rate, est_pps, syn_ratio, factor),
    )
    row = cur.fetchone()
    sampled = factor > 1.0
    line = (f"[abuse] FLAG {client_ip} on {info['identity']} (exporter {exporter_ip}): "
            f"~{est_conn_rate:.0f} conn/s, ~{est_pps:.0f} pkt/s, "
            f"SYN-like {syn_ratio*100:.0f}%"
            + (f" [sampled x{factor:.0f}, est. scaled]" if sampled else ""))
    if row["inserted"] or not row["notified"]:
        print(line, flush=True)
        post_webhook(
            f":rotating_light: High connection rate: {client_ip} on "
            f"{info['identity']} — ~{est_conn_rate:.0f} conn/s, ~{est_pps:.0f} pkt/s, "
            f"SYN-like {syn_ratio*100:.0f}%"
            + (" (sampled router — estimate scaled, small floods may hide)" if sampled else ""),
            {"router": info["identity"], "customer_id": info["customer_id"],
             "exporter_ip": exporter_ip, "client_ip": client_ip,
             "est_conn_per_s": round(est_conn_rate), "est_pkt_per_s": round(est_pps),
             "syn_like_ratio": round(syn_ratio, 3), "sampling_factor": factor,
             "sampled": sampled})
        cur.execute("UPDATE flow_abuse_events SET notified = true WHERE id = %s",
                    (row["id"],))


def main():
    while True:
        try:
            sync_once()
        except Exception as e:  # never let a transient DB/CH blip kill the loop
            print(f"[sync] ERROR: {e}", flush=True)
        try:
            scan_abuse()
        except Exception as e:
            print(f"[abuse] ERROR: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
