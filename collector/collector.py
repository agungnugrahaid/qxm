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
table) has a `vendor` field, defaulting to "unifi". poll_controller
dispatches on it via CONTROLLER_POLLERS below. Two vendors today:
  - "unifi": self-hosted controllers, rich per-client wireless metrics.
  - "ruijie": Ruijie/Reyee Cloud (cloud-as.ruijienetworks.com), a single
    cloud "controller" whose BUILDING groups map to our sites. Exposes
    per-client signal/band/channel/ssid/ip and per-AP status/channel-util,
    but NOT satisfaction, noise (so no SNR), or per-client retry counters
    -- those columns are left NULL and the corresponding dashboard cards
    read n/a for Ruijie sites. Rate-limited (20/sec, 5000 calls/day); it
    polls every 600s (10-min cadence, comfortably inside the dashboard's
    15-min freshness window) and fetches devices+clients per paired
    building (not a bulk account sweep), so the daily call budget is
    ~1 + 2*paired_buildings per cycle -- proportional to what's collected,
    not the ~300-building account. Raise the interval toward 900s if the
    paired-building count ever gets large enough to approach the budget.

Adding a further vendor is "write one poll_<vendor>_controller and
register it" -- no change to poll_all/main_loop.
"""

import asyncio
import base64
import os
import ssl
import time
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
            (ctrl["name"], ctrl["base_url"], ctrl["api_user"], ctrl["api_password"], ctrl.get("is_unifi_os", False), ctrl.get("vendor", "unifi")),
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
            c.get("ip"),
        )
        for c in data.get("data", [])
    ]

    if rows:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO client_metrics (time, site_id, client_mac, ap_mac, signal, satisfaction, radio, "
            "tx_retries, wifi_tx_attempts, tx_rate, rx_rate, noise, channel, essid, is_wired, hostname, ip) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            dev.get("ip"),
        ))

    if rows:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO ap_inventory "
            "(time, site_id, ap_mac, ap_name, model, version, cpu_pct, mem_pct, channel_util_2g, channel_util_5g, "
            "state, satisfaction, num_sta, uptime, ip) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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

        # --- Discovery: register EVERY site the controller reports, so
        # they're all visible/assignable on admin-ui's Sites page. This is
        # deliberately decoupled from collection (below): discovery is
        # automatic, collection is opt-in via customer assignment.
        # Best-effort -- a failure here shouldn't block metric collection
        # for the already-known sites.
        try:
            site_descs = await fetch_site_descs(session, ctrl)
            for site_name, desc in site_descs.items():
                ensure_site(conn, controller_id, site_name, desc)
        except Exception as e:
            print(f"[{ctrl['name']}] site discovery failed — {e}")
            site_descs = {}

        # --- Collection: poll ONLY sites paired with a customer. The
        # Sites page's assign form is the single on/off switch -- no
        # per-site YAML edits (controllers.yaml's old `sites:` lists are
        # ignored). Unassigning stops collection on the next cycle;
        # history is kept.
        cur = conn.cursor()
        cur.execute(
            "SELECT id, unifi_site_name FROM sites "
            "WHERE controller_id = %s AND customer_id IS NOT NULL",
            (controller_id,),
        )
        collect_sites = cur.fetchall()
        cur.close()
        print(f"[{ctrl['name']}] {len(site_descs)} sites discovered, {len(collect_sites)} collecting")

        tasks = []
        site_labels = []
        for site_id, site_name in collect_sites:
            tasks.append(poll_site(session, ctrl, site_name, site_id, conn))
            site_labels.append(f"{site_name} (clients)")
            tasks.append(poll_site_devices(session, ctrl, site_name, site_id, conn))
            site_labels.append(f"{site_name} (devices)")

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, result in zip(site_labels, results):
            if isinstance(result, Exception):
                print(f"[{ctrl['name']}/{label}] error — {result}")


# ------------------------------------------------------------------------
# Ruijie / Reyee Cloud (cloud-as.ruijienetworks.com)
# ------------------------------------------------------------------------
# One cloud "controller" (the API account); its BUILDING groups map to our
# sites. Auth is a 3-part credential (app_id + secret + api_token) that
# mints a 60-minute accessToken. Rate-limited (20/sec, 5000 calls/day), so
# this runs on a slower per-controller interval and caches the token.
# See the module docstring for the metric-coverage limitation.

_RUIJIE_TOKENS = {}  # controller name -> (accessToken, expiry monotonic secs)
# accessToken TTL is 60 min per docs; refresh a little early to avoid using
# one that expires mid-cycle.
_RUIJIE_TOKEN_TTL = 55 * 60


def _ruijie_mac(dotted):
    """Ruijie reports MACs dotted ("5416.5184.acef"); normalize to
    colon-lowercase ("54:16:51:84:ac:ef") to match the UniFi rows so
    per-AP joins and dedup behave uniformly."""
    if not dotted:
        return None
    hexonly = dotted.replace(".", "").replace(":", "").lower()
    if len(hexonly) != 12:
        return dotted
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))


async def _ruijie_token(session, ctrl):
    name = ctrl["name"]
    cached = _RUIJIE_TOKENS.get(name)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    url = f"{ctrl['base_url']}/service/api/oauth20/client/access_token"
    async with session.post(
        url,
        params={"token": ctrl["api_token"]},
        json={"appid": ctrl["api_user"], "secret": ctrl["api_password"]},
        ssl=SSL_CTX,
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if data.get("code") != 0 or "accessToken" not in data:
        raise RuntimeError(f"Ruijie auth failed: {data.get('code')} {data.get('msg')}")
    token = data["accessToken"]
    _RUIJIE_TOKENS[name] = (token, time.monotonic() + _RUIJIE_TOKEN_TTL)
    return token


async def _ruijie_get(session, ctrl, path, params, call_counter):
    """GET a Ruijie API path with the access token, pacing to stay under
    the 20/sec limit and counting calls for the daily-budget log line."""
    call_counter[0] += 1
    await asyncio.sleep(0.1)  # <=10 req/s, comfortably under the 20/s cap
    url = f"{ctrl['base_url']}{path}"
    async with session.get(url, params=params, ssl=SSL_CTX) as resp:
        resp.raise_for_status()
        return await resp.json()


def _ruijie_walk_buildings(node, acc):
    """Collect every BUILDING-type group from the group tree as
    (groupId, name). BUILDING is Ruijie's site level (a customer venue);
    ROOT/DEVICE levels are skipped."""
    if isinstance(node, dict):
        if node.get("type") == "BUILDING" and node.get("groupId"):
            acc.append((node["groupId"], node.get("name") or str(node["groupId"])))
        for v in node.values():
            _ruijie_walk_buildings(v, acc)
    elif isinstance(node, list):
        for v in node:
            _ruijie_walk_buildings(v, acc)


async def poll_ruijie_controller(ctrl, conn):
    call_counter = [0]
    # Ruijie's self-signed-free cloud uses real TLS, but SSL_CTX (verify
    # off) is fine and consistent with the rest of the collector.
    async with aiohttp.ClientSession() as session:
        token = await _ruijie_token(session, ctrl)
        controller_id = ensure_controller(conn, ctrl)

        # --- Discovery: every BUILDING group becomes a site.
        tree = await _ruijie_get(
            session, ctrl, "/service/api/group/single/tree",
            {"depth": "DEVICE", "access_token": token}, call_counter,
        )
        tree_root = tree.get("groups", tree)
        buildings = []
        _ruijie_walk_buildings(tree_root, buildings)
        for gid, name in buildings:
            ensure_site(conn, controller_id, str(gid), name)

        # --- Which buildings are customer-paired (collect only those).
        cur = conn.cursor()
        cur.execute(
            "SELECT id, unifi_site_name FROM sites "
            "WHERE controller_id = %s AND customer_id IS NOT NULL",
            (controller_id,),
        )
        paired = cur.fetchall()  # (site_id, groupId-as-str)
        cur.close()

        if not paired:
            print(f"[{ctrl['name']}] {len(buildings)} buildings discovered, 0 collecting "
                  f"({call_counter[0]} API calls)")
            return

        now = datetime.now(timezone.utc)

        # --- Per-paired-building: fetch that building's devices then its
        # clients. Deliberately NOT a bulk ROOT device sweep -- that pulls
        # every device in the whole account (~26 calls) regardless of how
        # few buildings we actually collect, which dominates the daily
        # budget. Per-building keeps cost proportional to paired count
        # (confirmed: group_id=<building> returns exactly that building's
        # devices), which is what lets Ruijie run at the tighter 600s
        # interval its 15-min dashboard freshness needs.
        ap_written = 0
        client_written = 0
        for site_id, gid in paired:
            # Devices -> AP rows + a serial->mac map for this building, so
            # clients (which reference their AP by serial) get an ap_mac.
            serial_to_mac = {}
            ap_rows = []
            page = 1
            while True:
                devs = await _ruijie_get(
                    session, ctrl, "/service/api/maint/devices",
                    {"group_id": gid, "page": page, "per_page": 100, "access_token": token},
                    call_counter,
                )
                device_list = devs.get("deviceList", [])
                for d in device_list:
                    mac = _ruijie_mac(d.get("mac"))
                    if d.get("serialNumber"):
                        serial_to_mac[d["serialNumber"]] = mac
                    if d.get("commonType") != "AP":
                        continue
                    ap_rows.append((
                        now, site_id, mac, d.get("aliasName") or d.get("name"),
                        d.get("productClass"), d.get("softwareVersion"),
                        None, None,  # cpu_pct, mem_pct -- not exposed
                        d.get("radio1ChannelUtil"), d.get("radio2ChannelUtil"),
                        1 if d.get("onlineStatus") == "ON" else 0,
                        None,  # satisfaction -- not exposed
                        d.get("staNums"),
                        None,  # uptime -- not exposed
                        d.get("localIp"),
                    ))
                if len(device_list) < 100:
                    break
                page += 1
            if ap_rows:
                cur = conn.cursor()
                cur.executemany(
                    "INSERT INTO ap_inventory "
                    "(time, site_id, ap_mac, ap_name, model, version, cpu_pct, mem_pct, "
                    "channel_util_2g, channel_util_5g, state, satisfaction, num_sta, uptime, ip) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    ap_rows,
                )
                conn.commit()
                cur.close()
                ap_written += len(ap_rows)

            # Clients. satisfaction/noise/tx_retries/wifi_tx_attempts/
            # tx_rate/rx_rate are not exposed by Ruijie -> NULL (those
            # dashboard cards read n/a for Ruijie sites).
            client_rows = []
            page_index = 1
            while True:
                data = await _ruijie_get(
                    session, ctrl, "/service/api/open/v1/dev/user/current-user",
                    {"group_id": gid, "page_index": page_index, "page_size": 200,
                     "access_token": token},
                    call_counter,
                )
                client_list = data.get("list", [])
                for c in client_list:
                    if c.get("connectType") != "wireless":
                        continue
                    band = c.get("band")
                    radio = "ng" if band == "2.4G" else ("na" if band == "5G" else None)
                    client_rows.append((
                        now, site_id, _ruijie_mac(c.get("mac")),
                        serial_to_mac.get(c.get("linkedDevice")),  # ap_mac
                        c.get("rssi"),  # signal
                        None,  # satisfaction
                        radio,
                        None, None,  # tx_retries, wifi_tx_attempts
                        None, None,  # tx_rate, rx_rate
                        None,  # noise
                        c.get("channel"), c.get("ssid"),
                        False,  # is_wired
                        c.get("userName"), c.get("ip"),
                    ))
                if len(client_list) < 200:
                    break
                page_index += 1
            if client_rows:
                cur = conn.cursor()
                cur.executemany(
                    "INSERT INTO client_metrics (time, site_id, client_mac, ap_mac, signal, "
                    "satisfaction, radio, tx_retries, wifi_tx_attempts, tx_rate, rx_rate, "
                    "noise, channel, essid, is_wired, hostname, ip) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    client_rows,
                )
                conn.commit()
                cur.close()
                client_written += len(client_rows)

        print(f"[{ctrl['name']}] {len(buildings)} buildings discovered, {len(paired)} collecting "
              f"-- {ap_written} APs, {client_written} clients ({call_counter[0]} API calls)")


# Dispatch table so a new vendor is "write one poll_<vendor>_controller
# function and add it here," not a restructure of poll_all/main_loop. Every
# entry must accept (ctrl, conn) and follow the same contract the pollers
# do: raise on a controller-level failure (auth, unreachable, etc.) so
# poll_all's own try/except can log and move on to the next controller
# without killing the whole cycle.
CONTROLLER_POLLERS = {
    "unifi": poll_unifi_controller,
    "ruijie": poll_ruijie_controller,
}


async def poll_controller(ctrl, conn):
    vendor = ctrl.get("vendor", "unifi")
    poller = CONTROLLER_POLLERS.get(vendor)
    if poller is None:
        raise ValueError(f"no poller registered for vendor {vendor!r} (controller {ctrl['name']!r})")
    await poller(ctrl, conn)


# Last-poll monotonic timestamp per controller name, for per-controller
# interval gating (a controller with its own poll_interval_seconds -- e.g.
# a rate-limited Ruijie cloud on 900s -- shouldn't be hit every global
# tick just because a UniFi controller wants 300s).
_LAST_POLL = {}


async def poll_all(config, conn):
    global_interval = config.get("poll_interval_seconds", 300)
    now = time.monotonic()
    for ctrl in config["controllers"]:
        name = ctrl["name"]
        interval = ctrl.get("poll_interval_seconds", global_interval)
        last = _LAST_POLL.get(name)
        if last is not None and (now - last) < interval:
            continue  # not due yet on this controller's own cadence
        _LAST_POLL[name] = now
        try:
            await poll_controller(ctrl, conn)
        except Exception as e:
            print(f"[{name}] controller-level error — {e}")


async def main_loop():
    config = load_config()
    conn = get_conn()
    # The loop ticks at the global (minimum) interval; each controller is
    # then gated to its own cadence inside poll_all.
    interval = config.get("poll_interval_seconds", 300)

    while True:
        await poll_all(config, conn)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main_loop())
