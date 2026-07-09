"""
collector.py — async poller for wireless controllers (UniFi today).

Reads controllers.yaml (one or more controllers, each with a list of sites),
logs into each controller, pulls the client list (stat/sta) for every site
concurrently, and writes rows into TimescaleDB. Runs forever, sleeping
`poll_interval_seconds` between cycles.

This is the pilot-scale version: sequential across controllers, concurrent
across sites within a controller. For 5 controllers x 50 sites you'd add
concurrency across controllers too and a semaphore to cap in-flight
requests. Confirmed live this session: a full cycle across 2 controllers /
5 sites (including a 300+ AP site) takes ~1.1-1.2s against a 300s poll
interval, so this holds comfortably through single digits of controllers --
revisit concurrency-across-controllers only if this fleet approaches
~15-20 controller instances.

Multi-vendor: each controller row in controllers.yaml (and the `controllers`
table) has a `vendor` field, defaulting to "unifi" for backward
compatibility with existing config. poll_controller dispatches on it via
CONTROLLER_POLLERS below -- adding a second vendor (e.g. Ruijie, once its
API is actually available) means writing one new poll_<vendor>_controller
function and registering it there, not restructuring poll_all/main_loop.
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
            "INSERT INTO controllers (name, base_url, api_user, api_password, is_unifi_os, vendor) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (ctrl["name"], ctrl["base_url"], ctrl["api_user"], ctrl["api_password"], ctrl["is_unifi_os"], ctrl.get("vendor", "unifi")),
        )
        controller_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return controller_id


def ensure_site(conn, controller_id, site_name, site_desc=None):
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM sites WHERE controller_id = %s AND unifi_site_name = %s",
        (controller_id, site_name),
    )
    row = cur.fetchone()
    if row:
        site_id = row[0]
        # Keep the readable name current -- someone can rename a site in
        # UniFi later (e.g. a rebrand), and this is cheap to just always
        # re-assert rather than only setting it once at discovery.
        if site_desc is not None:
            cur.execute("UPDATE sites SET site_desc = %s WHERE id = %s", (site_desc, site_id))
    else:
        cur.execute(
            "INSERT INTO sites (controller_id, unifi_site_name, site_desc) VALUES (%s, %s, %s) RETURNING id",
            (controller_id, site_name, site_desc),
        )
        site_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return site_id


async def fetch_site_descs(session, ctrl):
    """
    GET /api/self/sites returns every site this account can see on the
    controller, each with both the internal `name` (the random code used
    in API URLs, e.g. "gk7em92p") and `desc` (the human label set in the
    UniFi UI, e.g. "01.0757-01.GRAND-AMBARRUKMO"). One call per
    controller gets every configured site's readable name in one shot,
    rather than a separate lookup per site.
    """
    api_prefix = f"{ctrl['base_url']}/proxy/network" if ctrl["is_unifi_os"] else ctrl["base_url"]
    async with session.get(f"{api_prefix}/api/self/sites", ssl=SSL_CTX) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return {s["name"]: s.get("desc") for s in data.get("data", [])}


async def poll_site(session, ctrl, site_name, site_id, conn):
    api_prefix = f"{ctrl['base_url']}/proxy/network" if ctrl["is_unifi_os"] else ctrl["base_url"]
    url = f"{api_prefix}/api/s/{site_name}/stat/sta"

    async with session.get(url, ssl=SSL_CTX) as resp:
        resp.raise_for_status()
        data = await resp.json()

    now = datetime.now(timezone.utc)
    # tx_retries/wifi_tx_attempts give a retry rate -- the wireless
    # equivalent of the wired-side interface error/collision metrics.
    # Signal strength alone doesn't show RF congestion the way retries
    # do (a client can have great signal and still retry constantly on a
    # crowded channel). tx_rate/rx_rate/noise/channel/essid round out the
    # picture: noise turns raw signal into a true SNR reading, and
    # tx_rate catches a client stuck at a degraded PHY rate even when
    # signal looks fine.
    rows = [
        (
            now, site_id, c.get("mac"), c.get("ap_mac"), c.get("signal"), c.get("satisfaction"), c.get("radio"),
            c.get("tx_retries"), c.get("wifi_tx_attempts"), c.get("tx_rate"), c.get("rx_rate"),
            c.get("noise"), c.get("channel"), c.get("essid"), c.get("is_wired"), c.get("hostname"),
        )
        for c in data.get("data", [])
    ]

    if rows:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO client_metrics (time, site_id, client_mac, ap_mac, signal, satisfaction, radio, "
            "tx_retries, wifi_tx_attempts, tx_rate, rx_rate, noise, channel, essid, is_wired, hostname) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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

    `state` gives direct AP up/down detection (1 = connected, confirmed
    live) instead of inferring an outage from an AP silently vanishing
    from client data. `satisfaction`/`num_sta` are UniFi's own per-AP
    scores, riding along the same request.
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
            dev.get("state"),
            dev.get("satisfaction"),
            dev.get("num_sta"),
            dev.get("uptime"),
        ))

    if rows:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO ap_inventory "
            "(time, site_id, ap_mac, ap_name, model, version, cpu_pct, mem_pct, channel_util_2g, channel_util_5g, "
            "state, satisfaction, num_sta, uptime) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        conn.commit()
        cur.close()

    print(f"[{ctrl['name']}/{site_name}] saved {len(rows)} AP inventory rows at {now.isoformat()}")


async def poll_unifi_controller(ctrl, conn):
    login_url = f"{ctrl['base_url']}/api/auth/login" if ctrl["is_unifi_os"] else f"{ctrl['base_url']}/api/login"

    # aiohttp's default cookie jar silently drops cookies for IP-address
    # hosts (only accepts them for real domain names) unless created with
    # unsafe=True -- confirmed live: login succeeded and returned a valid
    # session cookie, but every subsequent request came back 401
    # LoginRequired because the cookie was never actually stored, since
    # controllers are commonly reached by bare IP rather than a hostname.
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        login_resp = await session.post(
            login_url,
            json={"username": ctrl["api_user"], "password": ctrl["api_password"]},
            ssl=SSL_CTX,
        )
        login_resp.raise_for_status()

        controller_id = ensure_controller(conn, ctrl)

        # Best-effort -- readable site names are a nice-to-have, not
        # something that should block client/AP metric collection (the
        # actual point of this poll cycle) if this call fails for any
        # reason.
        try:
            site_descs = await fetch_site_descs(session, ctrl)
        except Exception as e:
            print(f"[{ctrl['name']}] couldn't fetch site names — {e}")
            site_descs = {}

        tasks = []
        site_labels = []
        for site_name in ctrl["sites"]:
            site_id = ensure_site(conn, controller_id, site_name, site_descs.get(site_name))
            tasks.append(poll_site(session, ctrl, site_name, site_id, conn))
            site_labels.append(f"{site_name} (clients)")
            tasks.append(poll_site_devices(session, ctrl, site_name, site_id, conn))
            site_labels.append(f"{site_name} (devices)")

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, result in zip(site_labels, results):
            if isinstance(result, Exception):
                print(f"[{ctrl['name']}/{label}] error — {result}")


# Dispatch table so a new vendor (e.g. Ruijie, once its API is actually in
# hand) is "write one poll_<vendor>_controller function and add it here,"
# not a restructure of poll_all/main_loop. Every entry must accept
# (ctrl, conn) and follow the same contract poll_unifi_controller does:
# raise on a controller-level failure (login, unreachable, etc.) so
# poll_all's own try/except can log and move on to the next controller
# without killing the whole cycle.
CONTROLLER_POLLERS = {
    "unifi": poll_unifi_controller,
}


async def poll_controller(ctrl, conn):
    vendor = ctrl.get("vendor", "unifi")
    poller = CONTROLLER_POLLERS.get(vendor)
    if poller is None:
        raise ValueError(f"no poller registered for vendor {vendor!r} (controller {ctrl['name']!r})")
    await poller(ctrl, conn)


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
