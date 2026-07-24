"""Sync ClickHouse flow.exporter_map from Postgres (the source of truth).

Reads router_flow_exporters JOIN routers JOIN customers, expands each CIDR to
concrete exporter IPs, and rewrites flow.exporter_map so flow attribution always
reflects what's in the Console -- no hand-editing ClickHouse. Also surfaces
exporter IPs seen in flows_raw that aren't attributed to any customer
("learn-and-flag": catches new/backup uplink IPs that only appear on failover).
Loops on SYNC_INTERVAL.
"""
import os
import time
import ipaddress

import psycopg2
import psycopg2.extras
import clickhouse_connect

DATABASE_URL = os.environ["DATABASE_URL"]
INTERVAL = int(os.environ.get("SYNC_INTERVAL", "300"))
MAX_EXPAND = int(os.environ.get("MAX_EXPAND", "1024"))  # skip absurdly large CIDRs


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


def main():
    while True:
        try:
            sync_once()
        except Exception as e:  # never let a transient DB/CH blip kill the loop
            print(f"[sync] ERROR: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
