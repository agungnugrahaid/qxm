"""
collector.py — async poller for UniFi controllers.

Reads controllers.yaml (one or more controllers, each with a list of sites),
logs into each controller, pulls the client list (stat/sta) for every site
concurrently, and writes rows into TimescaleDB. Runs forever, sleeping
`poll_interval_seconds` between cycles.

This is the pilot-scale version: sequential across controllers, concurrent
across sites within a controller. For 5 controllers x 50 sites you'd add
concurrency across controllers too and a semaphore to cap in-flight
requests — see isp_scale_addendum.md for that discussion.
"""

import asyncio
import os
import ssl
from datetime import datetime, timezone

import aiohttp
import psycopg2
import yaml

DATABASE_URL = os.environ["DATABASE_URL"]
CONFIG_PATH = "/app/controllers.yaml"

# Self-signed certs are normal for self-hosted controllers on a LAN.
# Do not reuse this relaxed SSL context for anything internet-facing.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def ensure_controller(conn, ctrl):
    cur = conn.cursor()
    cur.execute("SELECT id FROM controllers WHERE name = %s", (ctrl["name"],))
    row = cur.fetchone()
    if row:
        controller_id = row[0]
    else:
        cur.execute(
            "INSERT INTO controllers (name, base_url, api_user, api_password, is_unifi_os) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (ctrl["name"], ctrl["base_url"], ctrl["api_user"], ctrl["api_password"], ctrl["is_unifi_os"]),
        )
        controller_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return controller_id


def ensure_site(conn, controller_id, site_name):
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM sites WHERE controller_id = %s AND unifi_site_name = %s",
        (controller_id, site_name),
    )
    row = cur.fetchone()
    if row:
        site_id = row[0]
    else:
        cur.execute(
            "INSERT INTO sites (controller_id, unifi_site_name) VALUES (%s, %s) RETURNING id",
            (controller_id, site_name),
        )
        site_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return site_id


async def poll_site(session, ctrl, site_name, site_id, conn):
    api_prefix = f"{ctrl['base_url']}/proxy/network" if ctrl["is_unifi_os"] else ctrl["base_url"]
    url = f"{api_prefix}/api/s/{site_name}/stat/sta"

    async with session.get(url, ssl=SSL_CTX) as resp:
        resp.raise_for_status()
        data = await resp.json()

    now = datetime.now(timezone.utc)
    rows = [
        (now, site_id, c.get("mac"), c.get("ap_mac"), c.get("signal"), c.get("satisfaction"), c.get("radio"))
        for c in data.get("data", [])
    ]

    if rows:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO client_metrics (time, site_id, client_mac, ap_mac, signal, satisfaction, radio) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        conn.commit()
        cur.close()

    print(f"[{ctrl['name']}/{site_name}] saved {len(rows)} client rows at {now.isoformat()}")


async def poll_site_devices(session, ctrl, site_name, site_id, conn):
    """
    Pulls stat/device (APs) for a site — this is where AP firmware version
    and channel utilization live, riding along with data you'd want for
    AP health anyway. Field names below match a standard software
    Controller/UDM response; double check against your controller version
    if a field comes back empty.
    """
    api_prefix = f"{ctrl['base_url']}/proxy/network" if ctrl["is_unifi_os"] else ctrl["base_url"]
    url = f"{api_prefix}/api/s/{site_name}/stat/device"

    async with session.get(url, ssl=SSL_CTX) as resp:
        resp.raise_for_status()
        data = await resp.json()

    now = datetime.now(timezone.utc)
    rows = []
    for dev in data.get("data", []):
        stats = dev.get("system-stats", {})
        cu_2g = None
        cu_5g = None
        for radio in dev.get("radio_table_stats", []):
            if radio.get("radio") == "ng":
                cu_2g = radio.get("cu_total")
            elif radio.get("radio") == "na":
                cu_5g = radio.get("cu_total")

        rows.append((
            now,
            site_id,
            dev.get("mac"),
            dev.get("name"),
            dev.get("model"),
            dev.get("version"),
            stats.get("cpu"),
            stats.get("mem"),
            cu_2g,
            cu_5g,
        ))

    if rows:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO ap_inventory "
            "(time, site_id, ap_mac, ap_name, model, version, cpu_pct, mem_pct, channel_util_2g, channel_util_5g) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        conn.commit()
        cur.close()

    print(f"[{ctrl['name']}/{site_name}] saved {len(rows)} AP inventory rows at {now.isoformat()}")


async def poll_controller(ctrl, conn):
    login_url = f"{ctrl['base_url']}/api/auth/login" if ctrl["is_unifi_os"] else f"{ctrl['base_url']}/api/login"

    async with aiohttp.ClientSession() as session:
        await session.post(
            login_url,
            json={"username": ctrl["api_user"], "password": ctrl["api_password"]},
            ssl=SSL_CTX,
        )

        controller_id = ensure_controller(conn, ctrl)
        tasks = []
        site_labels = []
        for site_name in ctrl["sites"]:
            site_id = ensure_site(conn, controller_id, site_name)
            tasks.append(poll_site(session, ctrl, site_name, site_id, conn))
            site_labels.append(f"{site_name} (clients)")
            tasks.append(poll_site_devices(session, ctrl, site_name, site_id, conn))
            site_labels.append(f"{site_name} (devices)")

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, result in zip(site_labels, results):
            if isinstance(result, Exception):
                print(f"[{ctrl['name']}/{label}] error — {result}")


async def poll_all(config, conn):
    for ctrl in config["controllers"]:
        try:
            await poll_controller(ctrl, conn)
        except Exception as e:
            print(f"[{ctrl['name']}] controller-level error — {e}")


async def main_loop():
    config = load_config()
    conn = get_conn()
    interval = config.get("poll_interval_seconds", 300)

    while True:
        await poll_all(config, conn)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main_loop())
