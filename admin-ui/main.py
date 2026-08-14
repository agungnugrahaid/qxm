"""
admin-ui — a small web front end for managing the router fleet instead of
editing router_inventory.csv by hand.

Pages:
  GET  /                    list routers, online/offline status, Edit/Deploy buttons
  GET  /routers/new         add-router form
  POST /routers/new         create row + auto-generate its auth token
  GET  /routers/{id}/edit   edit-router form
  POST /routers/{id}/edit   update row
  POST /routers/{id}/deploy push the qoe-push scripts to that one router now
  POST /deploy-all          kick off a background push to every router (or, with
                            priority="critical", only routers marked critical) --
                            returns immediately; watch progress via each router's
                            last_deploy_status/at/detail on GET /
  GET  /customers           list + quick-add customers (routers need one)
  POST /customers/new       create a customer
  GET  /sites               list UniFi sites (auto-discovered by the collector),
                            assign each one to a customer
  POST /sites/{id}/assign   set/change a site's customer_id
  GET  /config-snapshots/{router_id}                    list daily config snapshots for a router
  GET  /config-snapshots/{router_id}/{timestamp}        download one snapshot as .rsc
  GET  /config-snapshots/{router_id}/{timestamp}/view   view one snapshot's full text inline
  GET  /config-snapshots/{router_id}/{timestamp}/diff   git-like diff vs the previous snapshot

Deploy logic itself lives in deploy_lib.py (shared with bulk_deploy.py) so
this UI and the CLI tool can never drift apart on how a push actually happens.

Sites themselves are auto-created by collector.py the first time it sees a
new site on a controller — customer_id starts out NULL until assigned here.
This page is what closes that gap.
"""

import base64
import difflib
import hashlib
import hmac
import html
import ipaddress
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import decimal
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard_share import share_dashboard_for_customer, slugify
from portal_auth import find_customer_user, hash_password, touch_last_login, verify_password
from deploy_lib import load_templates, push_to_router
from report_lib import (
    WIB,
    ap_model_name,
    collect_ap_rows,
    collect_clients,
    collect_flow_providers,
    collect_flow_users,
    collect_kpis,
    collect_path,
    collect_resource,
    collect_sla,
    collect_traffic,
    collect_wifi_quality,
    flow_enabled,
    generate_report,
)

DATABASE_URL = os.environ["DATABASE_URL"]
INGEST_BASE_URL = os.environ.get("INGEST_BASE_URL", "https://monitor.yourisp.com")
SFTP_CONFIG = {
    "host": os.environ.get("SFTP_HOST", "monitor.yourisp.com"),
    "port": os.environ.get("SFTP_PORT", "2222"),
    "user": os.environ.get("SFTP_USER", "configupload"),
    "password": os.environ.get("SFTP_PASSWORD", "changeme"),
}
SYSLOG_CONFIG = {
    "host": os.environ.get("SYSLOG_HOST", "monitor.yourisp.com"),
    "port": os.environ.get("SYSLOG_PORT", "1514"),
}
RADIUS_CONFIG = {
    "secret1": os.environ.get("RADIUS_SERVER_1_SECRET", "changeme"),
    "secret2": os.environ.get("RADIUS_SERVER_2_SECRET", "changeme"),
}
# Traffic-flow (IPFIX) export target -- the QXM flow collector, reachable at
# the monitor host's public IP on 4739/udp (published by qxm-flow-collector).
# Defaults to the syslog host since it's the same VM. cache_entries sizes the
# per-router flow table to device RAM (v6.48.6 canary runs 512k); tune down for
# low-RAM CPE, up for very large sites. Passed to push_to_router, which applies
# it only to routers with flow_enabled set (and skips CGNAT). Sampling stays
# off by default -- byte totals come from interface counters, so it's only a
# CPU lever for busy routers.
FLOW_CONFIG = {
    # `or`-chain, not .get defaults: compose passes FLOW_COLLECTOR_HOST as an
    # empty string when unset, which would otherwise blank the target instead
    # of falling back to the (same-VM) syslog host.
    "target": (os.environ.get("FLOW_COLLECTOR_HOST") or os.environ.get("SYSLOG_HOST") or "monitor.yourisp.com"),
    "port": os.environ.get("FLOW_COLLECTOR_PORT") or "4739",
    "cache_entries": os.environ.get("FLOW_CACHE_ENTRIES") or "512k",
    # Sampling is per-router (routers.flow_sampling_*), read off the row in
    # deploy_lib -- not a fleet-wide default here.
}
# Pre-filled into the Add Router form (not Edit) as the fleet-standard API login.
ROUTER_API_DEFAULT_USER = os.environ.get("ROUTER_API_DEFAULT_USER", "")
ROUTER_API_DEFAULT_PASSWORD = os.environ.get("ROUTER_API_DEFAULT_PASSWORD", "")
# Same CIDR set as SFTP's own allowlist -- see
# routeros/qoe-baseline-hardening-v7.rsc's header comment for why this is
# reused as the router-side management-access allowlist too, rather than
# tracked as a second separate list.
GMEDIA_CIDRS = [c.strip() for c in os.environ.get("SFTP_ALLOWED_CIDRS", "").split(",") if c.strip()]

# ClickHouse (flow stack, sibling compose) -- read-only, for the flow
# attribution UI's "unattributed exporters" discovery. Same best-effort HTTP
# pattern as report_lib.flow_enabled: ClickHouse is a bonus, never a page
# dependency, so any error just yields no rows.
CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
CH_USER = os.environ.get("CH_USER", "flow")
CH_PASS = os.environ.get("CH_PASS", "flowpass")
# Mirror flow/sync_exporters.py MAX_EXPAND: reject a CIDR here if the sync
# would silently skip it (otherwise the row is accepted but never attributed).
FLOW_EXPAND_MAX = 1024
# Router egress attributability classes (see FLOW_COLLECTION_PLAN.md); cgnat
# means flows can't be pinned to one customer -> leave flow off.
VALID_FLOW_TIERS = ("public-distinct", "multi-uplink", "cgnat")

app = FastAPI(title="QXM Console")
# Vendored assets (simple-datatables) -- served locally rather than from a
# CDN so the admin UI has no external dependency at page load.
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
# Drop a company logo at admin-ui/static/logo.png (checked at startup) to
# replace the generic activity icon in the sidebar/mobile headers.
templates.env.globals["logo_exists"] = os.path.exists("static/logo.png")

# --- App-level login ---------------------------------------------------------
# Sessions are stateless HMAC-signed cookies (no server-side store, no extra
# deps): base64("user:expiry_ts") + "." + HMAC-SHA256 over that payload.
# ADMIN_UI_PASSWORD empty means logins are refused (fail closed) so a missing
# env var can't silently expose the UI. Without ADMIN_UI_SESSION_SECRET a
# random per-process secret is used, which logs everyone out on restart.
ADMIN_UI_USER = os.environ.get("ADMIN_UI_USER", "admin")
ADMIN_UI_PASSWORD = os.environ.get("ADMIN_UI_PASSWORD", "")
SESSION_SECRET = (os.environ.get("ADMIN_UI_SESSION_SECRET") or secrets.token_hex(32)).encode()
SESSION_HOURS = int(os.environ.get("ADMIN_UI_SESSION_HOURS", "8"))
# secure=True drops the cookie on plain-http access; disable only if the UI
# is ever served without Caddy's TLS in front.
COOKIE_SECURE = os.environ.get("ADMIN_UI_COOKIE_SECURE", "1") == "1"
SESSION_COOKIE = "qxm_session"


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()


# Session roles. "admin" is the NOC operator (full Console); "customer" is a
# portal login from customer_users (migration 028) restricted to /portal.
ROLE_ADMIN = "admin"
ROLE_CUSTOMER = "customer"


def make_session_cookie(username: str, role: str = ROLE_ADMIN, customer_id=None) -> str:
    """payload = username:role:customer_id:expiry  (customer_id "-" for admins).

    The customer_id lives INSIDE the signed payload, never in the URL or a
    form field -- that is the whole point of the portal: a customer cannot
    ask for another customer's data because they never get to name it.
    """
    expires = int(time.time()) + SESSION_HOURS * 3600
    payload = f"{username}:{role}:{customer_id if customer_id is not None else '-'}:{expires}"
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + _sign(payload)


def verify_session_cookie(value: str):
    """Returns (username, role, customer_id|None), or None if the cookie is
    missing/tampered/expired. Cookies issued before migration 028 have the old
    two-field payload and simply fail to parse -- those users re-login, which
    is the safe direction."""
    try:
        encoded, sig = value.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        username, role, customer_id, expires = payload.rsplit(":", 3)
        if time.time() > int(expires):
            return None
        if role not in (ROLE_ADMIN, ROLE_CUSTOMER):
            return None
        # A customer session without a customer_id would fall through to
        # "no filter" downstream; refuse it outright.
        if role == ROLE_CUSTOMER and not customer_id.isdigit():
            return None
        return username, role, (int(customer_id) if customer_id.isdigit() else None)
    except Exception:
        return None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # /health stays open for the watchdog; /static for the login page's CSS.
    if path in ("/login", "/health") or path.startswith("/static/"):
        return await call_next(request)
    session = verify_session_cookie(request.cookies.get(SESSION_COOKIE, ""))
    if session is None:
        if request.method == "GET":
            target = path + ("?" + str(request.url.query) if request.url.query else "")
            return RedirectResponse("/login?next=" + urllib.parse.quote(target, safe=""), status_code=303)
        return RedirectResponse("/login", status_code=303)
    user, role, customer_id = session
    # Deny-by-default for customers: they reach ONLY the portal and logout.
    # Written as an allowlist so a new admin route is never accidentally
    # customer-reachable -- adding a route must not widen this.
    if role == ROLE_CUSTOMER and not (path == "/portal" or path.startswith("/portal/") or path == "/logout"):
        return RedirectResponse("/portal", status_code=303)
    request.state.session_user = user
    request.state.session_role = role
    request.state.session_customer_id = customer_id
    return await call_next(request)


def portal_customer_id(request: Request, override=None) -> int:
    """The customer this request may see.

    For a customer session: always the id in the signed cookie -- `override`
    is ignored, so ?as= / ?customer_id= cannot widen what they see.
    For an admin session: `override` is honoured, which is how the NOC previews
    a customer's portal ("view as") without needing that customer's password.
    """
    if getattr(request.state, "session_role", None) == ROLE_ADMIN and override is not None:
        return int(override)
    cid = getattr(request.state, "session_customer_id", None)
    if cid is None:
        raise PermissionError("no customer bound to this session")
    return cid


def _safe_next(next_url: str) -> str:
    # Same-site paths only, so ?next= can't be turned into an open redirect.
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if verify_session_cookie(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None, "next": _safe_next(next)}
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    # compare_digest on both fields keeps the check constant-time; the sleep
    # blunts online brute-forcing without needing a lockout table.
    user_ok = secrets.compare_digest(username, ADMIN_UI_USER)
    pass_ok = ADMIN_UI_PASSWORD and secrets.compare_digest(password, ADMIN_UI_PASSWORD)

    role, customer_id, target = ROLE_ADMIN, None, _safe_next(next)
    if not (user_ok and pass_ok):
        # Not the NOC operator -- try a customer portal login (migration 028).
        # Same form, same cookie; the role in the session decides what they see.
        cust = None
        try:
            conn = get_conn()
            cust = find_customer_user(conn, username)
            ok = cust is not None and verify_password(password, cust["password_hash"])
            if ok:
                touch_last_login(conn, cust["id"])
            conn.close()
        except Exception:
            ok = False
        if not ok:
            time.sleep(1)
            error = "Invalid username or password." if ADMIN_UI_PASSWORD else "Login disabled: ADMIN_UI_PASSWORD is not set."
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": error, "next": _safe_next(next)}, status_code=401
            )
        role, customer_id = ROLE_CUSTOMER, cust["customer_id"]
        # Customers always land on the portal, never on a ?next= they were
        # handed -- otherwise a crafted link bounces them into an admin path
        # (which the middleware would reject anyway, but this is cleaner).
        target = "/portal"

    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(username, role, customer_id),
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return resp


# --- Customer portal ---------------------------------------------------------
# Everything here reads the customer_id from the signed session via
# portal_customer_id(request) -- never from the path, query or form. The
# middleware already restricts a customer session to /portal*; this is the
# second half of that guarantee.

# Grafana-style relative ranges. The portal is meant to feel like the customer
# dashboard we cannot share, so the range set matches the dashboard's, not the
# report's fixed 30-day window.
PORTAL_RANGES = {
    "6h": timedelta(hours=6), "24h": timedelta(hours=24),
    "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90),
}
PORTAL_RANGE_DEFAULT = "24h"

# Sections for the right-hand rail. Each carries its OWN default range rather
# than sharing one global picker: monthly SLA under a 24h window renders an
# empty card, and 90 days of per-core CPU is an expensive accident. The picker
# overrides only within the section the reader is in.
PORTAL_SECTIONS = [
    {"id": "overview", "title_en": "Overview",          "title_id": "Ringkasan",              "default_range": "24h"},
    {"id": "internet", "title_en": "Internet",          "title_id": "Internet",               "default_range": "7d"},
    {"id": "wireless", "title_en": "Wireless",          "title_id": "Nirkabel",               "default_range": "24h"},
    {"id": "traffic",  "title_en": "Traffic Insights",  "title_id": "Analisis Trafik",        "default_range": "7d"},
    {"id": "health",   "title_en": "Health & Capacity", "title_id": "Kesehatan & Kapasitas",  "default_range": "24h"},
    {"id": "inventory","title_en": "Inventory",         "title_id": "Inventaris",             "default_range": "24h"},
    {"id": "service",  "title_en": "Service",           "title_id": "Layanan",                "default_range": "30d"},
]


def _portal_window(rng: str):
    rng = rng if rng in PORTAL_RANGES else PORTAL_RANGE_DEFAULT
    end = datetime.now(WIB)
    return rng, end - PORTAL_RANGES[rng], end


def _epochs(times):
    """uPlot wants x as seconds. gapfill can emit None-free rows, but guard
    anyway so one bad bucket can't 500 the panel."""
    return [int(t.timestamp()) if t is not None else None for t in times]


def _num(v):
    """Decimal/None -> float/None so json can serialise it."""
    return None if v is None else float(v)


def _pg_rows(sql, params):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def _reshape(rows, key_fn, value_cols):
    """rows (ordered by bucket) -> (times, {series_name: [values]}).

    Same shape the report collectors return. `key_fn(row, col_label)` names the
    series. NOTE the `if name not in series` rather than setdefault: setdefault
    rebuilds the [None]*len default on every row even when the key exists,
    which is a real hot-path cost on 90-day windows.
    """
    times, seen = [], set()
    for r in rows:
        if r["t"] not in seen:
            seen.add(r["t"])
            times.append(r["t"])
    idx = {t: i for i, t in enumerate(times)}
    series = {}
    for r in rows:
        for label, col in value_cols:
            name = key_fn(r, label)
            if name not in series:
                series[name] = [None] * len(times)
            series[name][idx[r["t"]]] = r[col]
    return times, series


# --- Phase B portal collectors ------------------------------------------------
# Portal-only, deliberately NOT in report_lib: that file is the PDF's contract
# and the PDF stays as-is, so a panel added here can never change a
# customer-facing document.

def portal_uplinks(customer_id, start, end):
    """Per-WAN throughput. The Internet Traffic panel sums every uplink; this
    splits them, which is what tells a customer their backup link is carrying
    traffic (or that a primary has gone quiet)."""
    sql = """
        WITH deltas AS (
          SELECT um.router_id, r.identity_name AS router, um.uplink_label, um.time,
            um.rx_bytes - LAG(um.rx_bytes) OVER w AS rx_d,
            um.tx_bytes - LAG(um.tx_bytes) OVER w AS tx_d,
            EXTRACT(EPOCH FROM (um.time - LAG(um.time) OVER w)) AS secs
          FROM uplink_metrics um JOIN routers r ON r.id = um.router_id
          WHERE r.customer_id = %(cid)s AND um.time BETWEEN %(start)s AND %(end)s
          WINDOW w AS (PARTITION BY um.router_id, um.uplink_label ORDER BY um.time)
        )
        SELECT time_bucket_gapfill('5 minutes', time, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               router, uplink_label,
               avg(CASE WHEN rx_d IS NULL THEN NULL ELSE GREATEST(rx_d, 0) * 8 / NULLIF(secs, 0) END) AS down,
               avg(CASE WHEN tx_d IS NULL THEN NULL ELSE GREATEST(tx_d, 0) * 8 / NULLIF(secs, 0) END) AS up
        FROM deltas GROUP BY 1, 2, 3 ORDER BY 1
    """
    rows = _pg_rows(sql, {"cid": customer_id, "start": start, "end": end})
    multi = len({r["router"] for r in rows}) > 1
    def name(r, label):
        base = r["uplink_label"] or "uplink"
        return f"{r['router']} {base} {label}" if multi else f"{base} {label}"
    return _reshape(rows, name, [("↓", "down"), ("↑", "up")])


def portal_conntrack(customer_id, start, end):
    """Connection-table usage. At 100% the router silently drops new sessions
    and the customer experiences it as 'the internet stopped', with every
    latency graph still looking healthy."""
    sql = """
        SELECT time_bucket_gapfill('5 minutes', rm.time, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               r.identity_name AS router,
               avg(CASE WHEN rm.conntrack_max > 0
                        THEN 100.0 * rm.conntrack_count / rm.conntrack_max END) AS pct
        FROM router_metrics rm JOIN routers r ON r.id = rm.router_id
        WHERE r.customer_id = %(cid)s AND rm.time BETWEEN %(start)s AND %(end)s
        GROUP BY 1, 2 ORDER BY 1
    """
    rows = _pg_rows(sql, {"cid": customer_id, "start": start, "end": end})
    return _reshape(rows, lambda r, _: r["router"], [("", "pct")])


def portal_iferrors(customer_id):
    """Latest interface counters, problems only. A rising rx_fcs_error is how a
    failing cable shows up -- exactly how the Kesatuan Bangsa fault was found."""
    rows = _pg_rows(
        """
        SELECT DISTINCT ON (im.router_id, im.interface_name)
               im.router_id, r.identity_name AS router, im.interface_name,
               im.running, im.disabled,
               im.rx_fcs_error, im.rx_too_short, im.rx_too_long, im.rx_overflow,
               im.tx_collision, im.tx_late_collision
        FROM interface_metrics im JOIN routers r ON r.id = im.router_id
        WHERE r.customer_id = %s AND im.time > now() - interval '2 hours'
        ORDER BY im.router_id, im.interface_name, im.time DESC
        """,
        (customer_id,),
    )
    # An unused switch port is "not running and not disabled" -- i.e. it looks
    # exactly like a failed link. Reporting those as problems buries the real
    # ones (Oakwood alone had 9 empty ports), so a DOWN interface is only a
    # problem when it is a known uplink. Errors are always worth showing.
    uplinks = {
        (r["router_id"], r["interface_name"])
        for r in _pg_rows(
            """
            SELECT DISTINCT r.id AS router_id, u.interface_name
              FROM uplink_metrics u JOIN routers r ON r.id = u.router_id
             WHERE r.customer_id = %(cid)s AND u.time > now() - interval '24 hours'
                   AND u.interface_name IS NOT NULL
            UNION
            SELECT id, wan_interface FROM routers
             WHERE customer_id = %(cid)s AND wan_interface IS NOT NULL
            UNION
            SELECT id, wan_interface_backup FROM routers
             WHERE customer_id = %(cid)s AND wan_interface_backup IS NOT NULL
            """,
            {"cid": customer_id},
        )
    }
    cols = ("rx_fcs_error", "rx_too_short", "rx_too_long", "rx_overflow",
            "tx_collision", "tx_late_collision")
    out = []
    for r in rows:
        errs = sum(int(r[c] or 0) for c in cols)
        down = (not r["running"]) and (not r["disabled"])
        is_uplink = (r["router_id"], r["interface_name"]) in uplinks
        if not errs and not (down and is_uplink):
            continue
        out.append({"router": r["router"], "interface": r["interface_name"],
                    "state": "down" if down else ("disabled" if r["disabled"] else "up"),
                    "uplink": is_uplink,
                    "errors": errs,
                    "detail": ", ".join(f"{c.replace('_', ' ')}: {int(r[c])}"
                                        for c in cols if int(r[c] or 0))})
    out.sort(key=lambda x: (-x["errors"], x["router"]))
    return out


def portal_ap_detail(customer_id):
    """Per-AP load and radio conditions. Channel utilisation is the number that
    explains 'wifi is slow in a full function room' when every AP is online and
    signal looks fine."""
    rows = _pg_rows(
        """
        SELECT DISTINCT ON (a.ap_mac)
               a.ap_name, a.model, a.cpu_pct, a.mem_pct,
               a.channel_util_2g, a.channel_util_5g, a.num_sta, a.state
        FROM ap_inventory a JOIN sites s ON s.id = a.site_id
        WHERE s.customer_id = %s AND a.time > now() - interval '15 minutes'
        ORDER BY a.ap_mac, a.time DESC
        """,
        (customer_id,),
    )
    out = [{"ap_name": r["ap_name"], "model": ap_model_name(r["model"]), "code": r["model"],
            "clients": r["num_sta"],
            "cpu": r["cpu_pct"], "mem": r["mem_pct"],
            "util_2g": r["channel_util_2g"], "util_5g": r["channel_util_5g"]}
           for r in rows if r["state"] == 1]
    out.sort(key=lambda x: -(float(x["util_5g"] or 0) + float(x["util_2g"] or 0)))
    return out[:40]


def portal_abuse(customer_id, start, end):
    """Clients opening sessions far faster than their peers -- typically a
    compromised device or heavy P2P. Worded as something to look at, not an
    accusation: the detector flags rate, it does not know intent."""
    rows = _pg_rows(
        """
        SELECT r.identity_name AS router, e.internal_ip, e.first_seen, e.last_seen,
               e.peak_conn_rate, e.peak_pps, e.syn_ratio
        FROM flow_abuse_events e JOIN routers r ON r.id = e.router_id
        WHERE e.customer_id = %s AND e.last_seen >= %s AND e.last_seen < %s
        ORDER BY e.peak_conn_rate DESC LIMIT 50
        """,
        (customer_id, start, end),
    )
    return [{"router": r["router"], "ip": r["internal_ip"],
             "first_seen": r["first_seen"], "last_seen": r["last_seen"],
             "conn_rate": r["peak_conn_rate"], "pps": r["peak_pps"],
             "syn_ratio": r["syn_ratio"]} for r in rows]


INCIDENT_LABELS = {
    "router_unreachable": ("Router unreachable", "Router tidak terjangkau"),
    "internet_down": ("Internet connection down", "Koneksi internet terputus"),
    "degraded": ("Degraded performance", "Kinerja menurun"),
    "uplink_down": ("Uplink down", "Uplink terputus"),
    "aps_offline": ("Access points offline", "Access point mati"),
    "dhcp_full": ("DHCP pool full", "Alokasi DHCP penuh"),
    "conntrack_full": ("Connection table full", "Tabel koneksi penuh"),
    "abuse": ("Unusually active device", "Perangkat dengan aktivitas tidak wajar"),
}


def portal_incidents(customer_id, start, end):
    """Measured outages for the period, plus availability derived from them.

    monitoring_gap rows are excluded from BOTH the list and the availability
    figure: those are windows where we stopped receiving data from most of the
    fleet at once, i.e. our own outage, and charging a customer's availability
    for our collector freezing would be plainly wrong. The panel says so rather
    than silently dropping the time.
    """
    rows = _pg_rows(
        """
        SELECT i.id, i.kind, i.severity, i.started_at, i.ended_at, i.detail,
               i.monitoring_gap, r.identity_name AS router
        FROM incidents i LEFT JOIN routers r ON r.id = i.router_id
        WHERE i.customer_id = %s
          AND i.started_at < %s
          AND (i.ended_at IS NULL OR i.ended_at > %s)
        ORDER BY i.started_at DESC
        """,
        (customer_id, end, start),
    )

    window = max((end - start).total_seconds(), 1)
    counted, excluded, out = 0.0, 0.0, []
    for r in rows:
        # clip to the selected window so a long incident spanning the edge
        # contributes only the part inside it
        s = max(r["started_at"], start)
        e = min(r["ended_at"] or end, end)
        secs = max((e - s).total_seconds(), 0)
        if r["monitoring_gap"]:
            excluded += secs
            continue
        # Only a genuine outage counts against availability. A failed-over
        # uplink, a full DHCP pool or a noisy device is degraded/advisory --
        # real, worth showing, but the service was still up.
        if r["severity"] == "outage":
            counted += secs
        en, idn = INCIDENT_LABELS.get(r["kind"], (r["kind"], r["kind"]))
        out.append({
            "kind": r["kind"], "label_en": en, "label_id": idn,
            "severity": r["severity"], "router": r["router"],
            "started_at": r["started_at"], "ended_at": r["ended_at"],
            "ongoing": r["ended_at"] is None,
            "minutes": round(secs / 60, 1),
            "detail": r["detail"] or {},
        })

    # Abuse events are merged at READ time rather than copied into `incidents`:
    # flow-sync already maintains them with first_seen/last_seen, and duplicating
    # would mean keeping two tables in step. They are advisory -- a device
    # misbehaving is not the service being down -- so they never affect
    # availability.
    for a in _pg_rows(
        """
        SELECT e.internal_ip, e.first_seen, e.last_seen, e.peak_conn_rate,
               r.identity_name AS router
        FROM flow_abuse_events e LEFT JOIN routers r ON r.id = e.router_id
        WHERE e.customer_id = %s AND e.first_seen < %s AND e.last_seen > %s
        -- Only the worst few. These are advisory and there can be dozens; the
        -- timeline exists to surface outages, and 20 advisory rows bury them.
        -- The full list lives in the Traffic Insights panel.
        ORDER BY e.peak_conn_rate DESC LIMIT 5
        """,
        (customer_id, end, start),
    ):
        out.append({
            "kind": "abuse", "label_en": "Unusually active device",
            "label_id": "Perangkat dengan aktivitas tidak wajar",
            "severity": "advisory", "router": a["router"],
            "started_at": a["first_seen"], "ended_at": a["last_seen"],
            "ongoing": False,
            "minutes": round(max((min(a["last_seen"], end) - max(a["first_seen"], start))
                                 .total_seconds(), 0) / 60, 1),
            "detail": {"ip": a["internal_ip"],
                       "peak_conn_rate": round(a["peak_conn_rate"] or 0)},
        })
    out.sort(key=lambda i: i["started_at"], reverse=True)

    availability = 100.0 * (1 - min(counted, window) / window)
    return {
        "incidents": out,
        "open_count": sum(1 for i in out if i["ongoing"]),
        "availability": round(availability, 3),
        "downtime_minutes": round(counted / 60, 1),
        "excluded_minutes": round(excluded / 60, 1),
    }


def portal_environment(customer_id):
    """Latest hardware gauges per router: temperature, fans, PSU state, voltage,
    power draw. health_metrics stores value as TEXT because the gauges are a
    mixed bag (psu1-state is 'ok', temperature is a number)."""
    rows = _pg_rows(
        """
        SELECT DISTINCT ON (h.router_id, h.gauge_name)
               r.identity_name AS router, h.gauge_name, h.value, h.unit
        FROM health_metrics h JOIN routers r ON r.id = h.router_id
        WHERE r.customer_id = %s AND h.time > now() - interval '2 hours'
        ORDER BY h.router_id, h.gauge_name, h.time DESC
        """,
        (customer_id,),
    )
    pretty = {
        "cpu-temperature": "CPU temperature", "board-temperature1": "Board temperature",
        "sfp-temperature": "SFP temperature", "temperature": "Temperature",
        "fan1-speed": "Fan 1", "fan2-speed": "Fan 2", "fan-state": "Fan state",
        "psu1-state": "PSU 1", "psu2-state": "PSU 2", "voltage": "Voltage",
        "current": "Current", "power-consumption": "Power draw",
    }
    return [{"router": r["router"],
             "gauge": pretty.get(r["gauge_name"], r["gauge_name"].replace("-", " ")),
             "value": r["value"], "unit": r["unit"] or ""} for r in rows]


def portal_cores(customer_id, start, end):
    """Per-core CPU load. A router can look fine on average CPU while one core
    is pinned -- that is what a single-threaded task (often IPsec or firewall
    hashing) looks like."""
    sql = """
        SELECT time_bucket_gapfill('5 minutes', c.time, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               r.identity_name AS router, c.core_name, avg(c.load_pct) AS load
        FROM cpu_core_metrics c JOIN routers r ON r.id = c.router_id
        WHERE r.customer_id = %(cid)s AND c.time BETWEEN %(start)s AND %(end)s
        GROUP BY 1, 2, 3 ORDER BY 1
    """
    rows = _pg_rows(sql, {"cid": customer_id, "start": start, "end": end})
    multi = len({r["router"] for r in rows}) > 1
    def name(r, _):
        return f"{r['router']} {r['core_name']}" if multi else r["core_name"]
    times, series = _reshape(rows, name, [("", "load")])
    # A 16-core router across several sites is an unreadable spaghetti chart;
    # keep the busiest cores, which are the ones worth looking at anyway.
    if len(series) > 12:
        busiest = sorted(series, key=lambda k: -max((v or 0) for v in series[k]))[:12]
        series = {k: series[k] for k in busiest}
    return times, series


def portal_bands(customer_id):
    """Client split across radio bands. A hotel with most clients on 2.4GHz is
    usually one where 5GHz coverage does not reach the rooms."""
    rows = _pg_rows(
        """
        SELECT c.radio, count(DISTINCT c.client_mac) AS n
        FROM client_metrics c JOIN sites s ON s.id = c.site_id
        WHERE s.customer_id = %s AND c.time > now() - interval '15 minutes'
        GROUP BY 1
        """,
        (customer_id,),
    )
    # UniFi radio codes; Ruijie reports an empty string.
    label = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz", "": "Unknown", None: "Unknown"}
    out = [{"band": label.get(r["radio"], r["radio"]), "clients": int(r["n"] or 0)} for r in rows]
    total = sum(b["clients"] for b in out) or 1
    for b in out:
        b["share"] = round(100.0 * b["clients"] / total, 1)
    out.sort(key=lambda b: -b["clients"])
    return out


def portal_inventory(customer_id):
    """What the customer has. Deliberately WITHOUT mgmt_host and without
    RouterOS version / update status: those are an attacker's shopping list and
    the customer cannot act on them anyway (we patch the CPE)."""
    sites = _pg_rows(
        "SELECT COALESCE(NULLIF(site_desc, ''), unifi_site_name) AS name "
        "FROM sites WHERE customer_id = %s ORDER BY 1", (customer_id,))
    routers = _pg_rows(
        """
        SELECT r.identity_name AS name, f.board_name, f.architecture, m.uptime,
               r.last_seen_at
        FROM routers r
        LEFT JOIN LATERAL (SELECT board_name, architecture FROM router_firmware
                            WHERE router_id = r.id ORDER BY time DESC LIMIT 1) f ON true
        LEFT JOIN LATERAL (SELECT uptime FROM router_metrics
                            WHERE router_id = r.id ORDER BY time DESC LIMIT 1) m ON true
        WHERE r.customer_id = %s ORDER BY r.identity_name
        """, (customer_id,))
    ap_models = _pg_rows(
        """
        SELECT model, count(*) AS n FROM (
          SELECT DISTINCT ON (a.ap_mac) a.model
          FROM ap_inventory a JOIN sites s ON s.id = a.site_id
          WHERE s.customer_id = %s AND a.time > now() - interval '15 minutes'
          ORDER BY a.ap_mac, a.time DESC
        ) x GROUP BY 1 ORDER BY 2 DESC
        """, (customer_id,))
    files = _pg_rows(
        "SELECT id, label, filename, size_bytes, uploaded_at "
        "FROM customer_topology_files WHERE customer_id = %s ORDER BY uploaded_at DESC",
        (customer_id,))
    return {
        "sites": [r["name"] for r in sites],
        "routers": [{"name": r["name"], "board": r["board_name"],
                     "arch": r["architecture"], "uptime": r["uptime"],
                     "last_seen": r["last_seen_at"]} for r in routers],
        # Friendly name first, raw code kept alongside so an operator on a
        # support call can still match it to what the controller shows.
        "ap_models": [{"model": ap_model_name(r["model"]), "code": r["model"],
                       "count": int(r["n"])} for r in ap_models],
        "files": [{"id": r["id"], "label": r["label"], "filename": r["filename"],
                   "size": r["size_bytes"], "uploaded_at": r["uploaded_at"]} for r in files],
    }


def _jsonable(v):
    """Recursively make a payload JSON-safe. psycopg2 hands back Decimal for
    numeric columns and date/datetime for dates; JSONResponse is constructed
    AFTER the per-panel try/except, so one stray Decimal 500s the endpoint
    instead of degrading that single panel. (Bit us on ap_inventory.cpu_pct.)"""
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def portal_panel_list(customer_id, start, end):
    """Panel descriptors for the portal, mirroring the customer dashboard's
    layout. Cheap -- no series data, just what exists for this customer."""
    panels = [
        {"id": "incidents", "section": "overview", "kind": "incidents",
         "title_en": "Service Incidents",
         "title_id": "Gangguan Layanan",
         "note_en": "Measured from our monitoring -- this is not the contractual SLA, "
                    "which is on the Service page. Periods where our own monitoring was "
                    "unavailable are excluded rather than counted against you.",
         "note_id": "Diukur dari pemantauan kami -- ini bukan SLA kontraktual, yang ada di "
                    "halaman Layanan. Periode saat pemantauan kami tidak tersedia "
                    "dikecualikan, bukan dihitung sebagai gangguan."},
        {"id": "traffic", "section": "internet", "kind": "area",
         "title_en": "Internet Traffic", "title_id": "Trafik Internet", "unit": "bps"},
    ]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.id, r.identity_name FROM routers r
        WHERE r.customer_id = %s
          AND EXISTS (SELECT 1 FROM path_metrics pm
                      WHERE pm.router_id = r.id AND pm.time >= %s AND pm.time < %s)
        ORDER BY r.identity_name
        """,
        (customer_id, start, end),
    )
    for r in cur.fetchall():
        panels.append({"id": f"path:{r['id']}", "section": "internet", "kind": "line", "unit": "ms / %",
                       "title_en": f"Path Latency, Jitter & Loss — {r['identity_name']}",
                       "title_id": f"Latensi, Jitter & Kehilangan Paket — {r['identity_name']}"})
    conn.close()

    panels += [
        {"id": "resource", "section": "health", "kind": "line", "unit": "%",
         "title_en": "Router Resource Usage (CPU / RAM / Disk)",
         "title_id": "Penggunaan Sumber Daya Router (CPU / RAM / Disk)"},
        {"id": "clients", "section": "wireless", "kind": "line", "unit": "clients",
         "title_en": "Wi-Fi Clients Over Time",
         "title_id": "Jumlah Perangkat Wi-Fi dari Waktu ke Waktu"},
        {"id": "wifi", "section": "wireless", "kind": "line", "unit": "",
         "title_en": "Wi-Fi Quality (Signal / Satisfaction / Retry)",
         "title_id": "Kualitas Wi-Fi (Sinyal / Kepuasan / Pengulangan)"},
    ]

    # Only offered when the customer actually has the data, so nobody gets an
    # empty card. Each check is a cheap EXISTS.
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT EXISTS (SELECT 1 FROM sites WHERE customer_id = %s) AS x", (customer_id,))
    has_wireless = cur.fetchone()["x"]
    cur.execute(
        """SELECT EXISTS (
             SELECT 1 FROM uplink_metrics u JOIN routers r ON r.id = u.router_id
             WHERE r.customer_id = %s AND u.time > now() - interval '24 hours') AS x""",
        (customer_id,),
    )
    has_uplinks = cur.fetchone()["x"]
    cur.execute(
        """SELECT EXISTS (
             SELECT 1 FROM router_metrics m JOIN routers r ON r.id = m.router_id
             WHERE r.customer_id = %s AND m.time > now() - interval '24 hours'
               AND m.conntrack_max > 0) AS x""",
        (customer_id,),
    )
    has_conntrack = cur.fetchone()["x"]
    cur.execute(
        """SELECT EXISTS (
             SELECT 1 FROM interface_metrics i JOIN routers r ON r.id = i.router_id
             WHERE r.customer_id = %s AND i.time > now() - interval '2 hours') AS x""",
        (customer_id,),
    )
    has_ifaces = cur.fetchone()["x"]
    cur.execute("SELECT EXISTS (SELECT 1 FROM flow_abuse_events WHERE customer_id = %s) AS x",
                (customer_id,))
    has_abuse = cur.fetchone()["x"]
    cur.execute(
        """SELECT EXISTS (
             SELECT 1 FROM health_metrics h JOIN routers r ON r.id = h.router_id
             WHERE r.customer_id = %s AND h.time > now() - interval '2 hours') AS x""",
        (customer_id,),
    )
    has_env = cur.fetchone()["x"]
    cur.execute(
        """SELECT EXISTS (
             SELECT 1 FROM cpu_core_metrics c JOIN routers r ON r.id = c.router_id
             WHERE r.customer_id = %s AND c.time > now() - interval '24 hours') AS x""",
        (customer_id,),
    )
    has_cores = cur.fetchone()["x"]
    cur.execute("SELECT EXISTS (SELECT 1 FROM routers WHERE customer_id = %s) AS x", (customer_id,))
    has_inventory = cur.fetchone()["x"] or has_wireless
    cur.execute(
        """SELECT EXISTS (
             SELECT 1 FROM dhcp_pool_metrics d JOIN routers r ON r.id = d.router_id
             WHERE r.customer_id = %s AND d.time > now() - interval '24 hours') AS x""",
        (customer_id,),
    )
    has_dhcp = cur.fetchone()["x"]
    cur.execute(
        """SELECT EXISTS (SELECT 1 FROM customer_sla_services WHERE customer_id = %s)
                OR EXISTS (SELECT 1 FROM customer_tickets WHERE customer_id = %s) AS x""",
        (customer_id, customer_id),
    )
    has_sla = cur.fetchone()["x"]
    conn.close()

    if has_uplinks:
        panels.append({"id": "uplinks", "section": "internet", "kind": "line", "unit": "bps",
                       "title_en": "Traffic per Uplink",
                       "title_id": "Trafik per Uplink"})
    if has_wireless:
        panels.append({"id": "aps", "section": "wireless", "kind": "aptable",
                       "title_en": "Access Point Status",
                       "title_id": "Status Access Point"})
        panels.append({"id": "apdetail", "section": "wireless", "kind": "apdetail",
                       "title_en": "Access Point Load & Radio",
                       "title_id": "Beban & Radio Access Point",
                       "note_en": "Channel utilisation above ~60% means the airtime is congested, "
                                  "even when signal strength looks healthy.",
                       "note_id": "Utilisasi kanal di atas ~60% menandakan airtime padat, "
                                  "walaupun kekuatan sinyal terlihat baik."})
    if has_conntrack:
        panels.append({"id": "conntrack", "section": "health", "kind": "line", "unit": "%",
                       "title_en": "Connection Table Usage",
                       "title_id": "Penggunaan Tabel Koneksi",
                       "note_en": "At 100% the router cannot open new sessions, which is felt "
                                  "as the internet stopping even while latency looks normal.",
                       "note_id": "Pada 100% router tidak dapat membuka sesi baru, terasa seperti "
                                  "internet berhenti walaupun latensi terlihat normal."})
    if has_ifaces:
        panels.append({"id": "iferrors", "section": "health", "kind": "iferrors",
                       "title_en": "Interface Problems",
                       "title_id": "Masalah Antarmuka",
                       "note_en": "Only interfaces that are down or reporting errors are listed. "
                                  "Rising error counts usually mean a cable or optic fault.",
                       "note_id": "Hanya antarmuka yang mati atau bermasalah yang ditampilkan. "
                                  "Jumlah error yang naik biasanya berarti gangguan kabel atau optik."})
    if has_abuse:
        panels.append({"id": "abuse", "section": "traffic", "kind": "abuse",
                       "title_en": "Unusually Active Devices",
                       "title_id": "Perangkat dengan Aktivitas Tidak Wajar",
                       "note_en": "Devices opening connections far faster than others on the same "
                                  "network. Often a compromised device or peer-to-peer software — "
                                  "worth checking, not proof of a problem.",
                       "note_id": "Perangkat yang membuka koneksi jauh lebih cepat dari perangkat "
                                  "lain di jaringan yang sama. Sering karena perangkat terinfeksi "
                                  "atau aplikasi peer-to-peer — perlu diperiksa, bukan bukti masalah."})
    if has_wireless:
        panels.append({"id": "bands", "section": "wireless", "kind": "bands",
                       "title_en": "Clients by Radio Band",
                       "title_id": "Perangkat per Pita Radio",
                       "note_en": "A high share on 2.4 GHz usually means 5 GHz coverage is not "
                                  "reaching those areas.",
                       "note_id": "Porsi 2.4 GHz yang tinggi biasanya berarti jangkauan 5 GHz "
                                  "belum menjangkau area tersebut."})
    if has_cores:
        panels.append({"id": "cores", "section": "health", "kind": "line", "unit": "%",
                       "title_en": "CPU Load per Core",
                       "title_id": "Beban CPU per Inti",
                       "note_en": "A single pinned core can slow traffic while the average CPU "
                                  "still looks low.",
                       "note_id": "Satu inti yang penuh dapat memperlambat trafik walaupun "
                                  "rata-rata CPU terlihat rendah."})
    if has_env:
        panels.append({"id": "environment", "section": "health", "kind": "environment",
                       "title_en": "Hardware Health",
                       "title_id": "Kesehatan Perangkat Keras"})
    if has_inventory:
        panels.append({"id": "inventory", "section": "inventory", "kind": "inventory",
                       "title_en": "Equipment & Sites",
                       "title_id": "Perangkat & Lokasi"})
    if has_dhcp:
        panels.append({"id": "dhcp", "section": "health", "kind": "dhcp",
                       "title_en": "DHCP Pool Utilisation",
                       "title_id": "Penggunaan Alamat DHCP"})
    if has_sla:
        panels.append({"id": "sla", "section": "service", "kind": "sla",
                       "title_en": "SLA & Support Tickets",
                       "title_id": "SLA & Tiket Dukungan",
                       "note_en": "Recent months — not affected by the range selector above.",
                       "note_id": "Beberapa bulan terakhir — tidak mengikuti pilihan rentang di atas."})
    if flow_enabled(customer_id):
        panels += [
            {"id": "flow_providers", "section": "traffic", "kind": "table",
             "title_en": "Top Content Providers", "title_id": "Konten / Layanan Teratas",
             "note_en": ("Indicative figures based on sampled traffic data. Provider "
                         "ranking is representative; absolute volumes are lower than "
                         "actual usage."),
             "note_id": ("Angka indikatif berdasarkan sampel data trafik. Peringkat "
                         "layanan bersifat representatif; volume absolut lebih rendah "
                         "dari pemakaian sebenarnya.")},
            {"id": "flow_users", "section": "traffic", "kind": "bar",
             "title_en": "Top Internal Users", "title_id": "Pengguna Internal Teratas",
             "note_en": ("Indicative figures based on sampled traffic data. User "
                         "ranking is representative; absolute volumes are lower than "
                         "actual usage."),
             "note_id": ("Angka indikatif berdasarkan sampel data trafik. Peringkat "
                         "pengguna bersifat representatif; volume absolut lebih rendah "
                         "dari pemakaian sebenarnya.")},
        ]
    return panels


@app.get("/portal")
def portal_home(request: Request, range: str = PORTAL_RANGE_DEFAULT,
                as_: int = Query(None, alias="as")):
    is_admin = getattr(request.state, "session_role", None) == ROLE_ADMIN
    if is_admin and as_ is None:
        # Admins have no single customer -- send them to the Console.
        return RedirectResponse("/customers", status_code=303)
    customer_id = portal_customer_id(request, as_)
    rng, start, end = _portal_window(range)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM customers WHERE id = %s", (customer_id,))
    row = cur.fetchone()
    conn.close()

    # The shell renders immediately; panels stream in over /portal/api/series
    # so a slow 90-day scan never blocks the page.
    panels = portal_panel_list(customer_id, start, end)
    # Only offer a section that has something in it. Overview is always shown
    # (it carries the KPI tiles, which every customer has).
    used = {p["section"] for p in panels} | {"overview"}
    sections = [s for s in PORTAL_SECTIONS if s["id"] in used]

    return templates.TemplateResponse(
        "portal.html",
        {"request": request,
         "customer_name": row["name"] if row else "",
         "panels": panels,
         "sections": sections,
         "ranges": list(PORTAL_RANGES.keys()),
         "range": rng,
         # Only ever set for an admin preview; a customer session gets "" and
         # the value is ignored server-side even if they add it by hand.
         "preview_as": as_ if is_admin else None,
         "report_start_default": (end - timedelta(days=30)).strftime("%Y-%m-%d"),
         "report_end_default": end.strftime("%Y-%m-%d"),
         "user": request.state.session_user},
    )


@app.get("/portal/api/kpis")
def portal_api_kpis(request: Request, range: str = PORTAL_RANGE_DEFAULT,
                    as_: int = Query(None, alias="as")):
    customer_id = portal_customer_id(request, as_)
    _, start, end = _portal_window(range)
    try:
        kpis = collect_kpis(customer_id, start, end)
    except Exception:
        kpis = []
    return JSONResponse(_jsonable({"kpis": [{"en": en, "id": idn, "value": v} for en, idn, v in kpis]}))


@app.get("/portal/api/series")
def portal_api_series(request: Request, panel: str, range: str = PORTAL_RANGE_DEFAULT,
                      as_: int = Query(None, alias="as")):
    """One panel's data as JSON. customer_id comes from the session (or, for an
    admin preview only, ?as=), so `panel` is the only client input -- and a
    path:<id> router is re-checked against that customer before it is queried."""
    customer_id = portal_customer_id(request, as_)
    _, start, end = _portal_window(range)
    out = {"panel": panel, "times": [], "series": {}}

    try:
        if panel == "traffic":
            times, down, up = collect_traffic(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {"Download": [_num(v) for v in down],
                             "Upload": [_num(v) for v in up]}
        elif panel.startswith("path:"):
            router_id = int(panel.split(":", 1)[1])
            # Ownership check -- without it a customer could read another
            # customer's router by guessing an id.
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM routers WHERE id = %s AND customer_id = %s",
                        (router_id, customer_id))
            owned = cur.fetchone() is not None
            conn.close()
            if not owned:
                return JSONResponse({"error": "not found"}, status_code=404)
            times, series = collect_path(router_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {k: [_num(x) for x in v] for k, v in series.items()}
        elif panel == "resource":
            times, series = collect_resource(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {k: [_num(x) for x in v] for k, v in series.items()}
        elif panel == "clients":
            times, series = collect_clients(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {k: [_num(x) for x in v] for k, v in series.items()}
        elif panel == "wifi":
            times, sig, sat, ret = collect_wifi_quality(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {"Signal (dBm)": [_num(v) for v in sig],
                             "Satisfaction %": [_num(v) for v in sat],
                             "Retry %": [_num(v) for v in ret]}
        elif panel == "flow_providers":
            from_ms, to_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            out["rows"] = collect_flow_providers(customer_id, from_ms, to_ms)
        elif panel == "flow_users":
            from_ms, to_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            labels, values, human = collect_flow_users(customer_id, from_ms, to_ms)
            out["rows"] = [{"label": l, "value": v, "human": h}
                           for l, v, h in zip(labels, values, human)]
        elif panel == "aps":
            rows, summary_en, summary_id = collect_ap_rows(customer_id)
            # Friendly model names for the portal only -- collect_ap_rows is
            # shared with the PDF and the PDF stays as-is, so the rename
            # happens here rather than in report_lib.
            for r in rows:
                r["code"] = r.get("model")
                r["model"] = ap_model_name(r.get("model"))
            out["rows"] = rows
            out["summary_en"], out["summary_id"] = summary_en, summary_id
        elif panel == "uplinks":
            times, series = portal_uplinks(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {k: [_num(x) for x in v] for k, v in series.items()}
        elif panel == "conntrack":
            times, series = portal_conntrack(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {k: [_num(x) for x in v] for k, v in series.items()}
        elif panel == "incidents":
            out.update(portal_incidents(customer_id, start, end))
        elif panel == "cores":
            times, series = portal_cores(customer_id, start, end)
            out["times"] = _epochs(times)
            out["series"] = {k: [_num(x) for x in v] for k, v in series.items()}
        elif panel == "environment":
            out["rows"] = portal_environment(customer_id)
        elif panel == "bands":
            out["rows"] = portal_bands(customer_id)
        elif panel == "inventory":
            out.update(portal_inventory(customer_id))
        elif panel == "iferrors":
            out["rows"] = portal_iferrors(customer_id)
        elif panel == "apdetail":
            out["rows"] = portal_ap_detail(customer_id)
        elif panel == "abuse":
            out["rows"] = portal_abuse(customer_id, start, end)
        elif panel == "dhcp":
            # Latest reading per pool. A full pool means guests silently fail
            # to get an address -- it presents as "wifi is broken", so this is
            # worth a card of its own rather than a line on a graph.
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT DISTINCT ON (d.router_id, d.pool_name)
                       r.identity_name AS router, d.pool_name,
                       d.total_addresses, d.active_leases, d.utilization_pct
                FROM dhcp_pool_metrics d JOIN routers r ON r.id = d.router_id
                WHERE r.customer_id = %s AND d.time > now() - interval '24 hours'
                ORDER BY d.router_id, d.pool_name, d.time DESC
                """,
                (customer_id,),
            )
            out["rows"] = [
                {"router": r["router"], "pool": r["pool_name"],
                 "total": r["total_addresses"], "used": r["active_leases"],
                 "pct": float(r["utilization_pct"]) if r["utilization_pct"] is not None else None}
                for r in cur.fetchall()
            ]
            conn.close()
            out["rows"].sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0)))
        elif panel == "sla":
            # SLA and tickets are monthly by nature. Tying them to the range
            # picker means a 24h view renders an empty card even though the
            # customer has records -- so this panel always covers the last
            # three whole months plus the current one, whatever the picker says.
            sla_start = (end.replace(day=1) - timedelta(days=62)).replace(day=1)
            months, overall, ytd = collect_sla(customer_id, sla_start, end)
            out["overall"] = overall
            out["ytd"] = ytd
            out["months"] = [
                {
                    "label": m["label"],
                    "total_sla": m["total_sla"],
                    "total_nodes": m["total_nodes"],
                    "services": [
                        {"service_id": s["service_id"], "service_name": s["service_name"],
                         "node_count": s["node_count"], "sla": s["sla_fmt"]}
                        for s in m["services"]
                    ],
                    "tickets": [
                        {"ticket_no": t["ticket_no"], "date": t["tanggal_fmt"],
                         "description": t["description"], "action": t["action"],
                         "mttr": t["mttr_fmt"], "status": t["status"]}
                        for t in m["tickets"]
                    ],
                }
                for m in months
            ]
        else:
            return JSONResponse({"error": "unknown panel"}, status_code=404)
    except Exception as e:
        # A single failing panel must not take the page down.
        return JSONResponse({"panel": panel, "error": str(e)[:200]}, status_code=200)

    return JSONResponse(_jsonable(out))


# --- Portal user management (admin side) -------------------------------------
# Passwords are generated here, never chosen by the operator, and shown exactly
# once -- there is no "view password" anywhere, only reset.

def _new_portal_password() -> str:
    """Readable but strong: 4 groups of 4 from an unambiguous alphabet, so it
    survives being read down a phone line to a hotel's IT contact."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def _portal_users(conn, customer_id):
    cur = conn.cursor()
    cur.execute(
        """SELECT id, email, is_active, created_at, last_login_at
           FROM customer_users WHERE customer_id = %s ORDER BY lower(email)""",
        (customer_id,),
    )
    return cur.fetchall()


@app.post("/customers/{customer_id}/portal-users")
def portal_user_create(request: Request, customer_id: int, email: str = Form("")):
    if getattr(request.state, "session_role", None) != ROLE_ADMIN:
        return RedirectResponse("/portal", status_code=303)
    email = (email or "").strip()
    if not email or "@" not in email:
        return RedirectResponse(f"/customers/{customer_id}?perror=email", status_code=303)
    password = _new_portal_password()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO customer_users (customer_id, email, password_hash) VALUES (%s,%s,%s)",
            (customer_id, email, hash_password(password)),
        )
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); conn.close()
        # The unique index is global (lower(email)) -- an address can only ever
        # belong to one customer, so say so rather than a generic failure.
        return RedirectResponse(f"/customers/{customer_id}?perror=duplicate", status_code=303)
    conn.close()
    # Password travels once, in the redirect, and is shown once.
    return RedirectResponse(
        f"/customers/{customer_id}?pnew={urllib.parse.quote(email)}&ppw={urllib.parse.quote(password)}",
        status_code=303,
    )


@app.post("/customers/{customer_id}/portal-users/{user_id}/reset")
def portal_user_reset(request: Request, customer_id: int, user_id: int):
    if getattr(request.state, "session_role", None) != ROLE_ADMIN:
        return RedirectResponse("/portal", status_code=303)
    password = _new_portal_password()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE customer_users SET password_hash = %s WHERE id = %s AND customer_id = %s RETURNING email",
        (hash_password(password), user_id, customer_id),
    )
    row = cur.fetchone()
    conn.commit(); conn.close()
    if not row:
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    return RedirectResponse(
        f"/customers/{customer_id}?pnew={urllib.parse.quote(row['email'])}&ppw={urllib.parse.quote(password)}",
        status_code=303,
    )


@app.post("/customers/{customer_id}/portal-users/{user_id}/toggle")
def portal_user_toggle(request: Request, customer_id: int, user_id: int):
    if getattr(request.state, "session_role", None) != ROLE_ADMIN:
        return RedirectResponse("/portal", status_code=303)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE customer_users SET is_active = NOT is_active WHERE id = %s AND customer_id = %s",
        (user_id, customer_id),
    )
    conn.commit(); conn.close()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/portal-users/{user_id}/delete")
def portal_user_delete(request: Request, customer_id: int, user_id: int):
    if getattr(request.state, "session_role", None) != ROLE_ADMIN:
        return RedirectResponse("/portal", status_code=303)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM customer_users WHERE id = %s AND customer_id = %s",
                (user_id, customer_id))
    conn.commit(); conn.close()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.get("/portal/topology/{file_id}")
def portal_topology(request: Request, file_id: int, as_: int = Query(None, alias="as")):
    """The customer's own network diagram. Scoped by customer_id in the WHERE
    clause, so guessing another customer's file id returns 404 rather than
    their document. Served as an attachment, never inline -- these are
    operator-supplied files and inline rendering is how an SVG would run
    script in the customer's session."""
    customer_id = portal_customer_id(request, as_)
    rows = _pg_rows(
        "SELECT filename, content_type, data FROM customer_topology_files "
        "WHERE id = %s AND customer_id = %s",
        (file_id, customer_id),
    )
    if not rows:
        return PlainTextResponse("Not found", status_code=404)
    f = rows[0]
    name = (f["filename"] or "topology").replace('"', "")
    return Response(
        bytes(f["data"]),
        media_type=f["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/portal/report")
def portal_report(request: Request, days: int = 30, start: str = "", end: str = "",
                  as_: int = Query(None, alias="as")):
    """The customer's own PDF. Same generator as the Console button, but the
    id comes from the session, so ?customer_id= cannot be forged.

    Period: explicit ?start=&end= (whole WIB days, inclusive) wins over ?days=,
    mirroring the Console's date picker."""
    customer_id = portal_customer_id(request, as_)
    start_dt = end_dt = None
    if start and end:
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=WIB)
            end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=WIB) + timedelta(days=1)
        except ValueError:
            return RedirectResponse("/portal?error=report_range", status_code=303)
        if end_dt <= start_dt or (end_dt - start_dt) > timedelta(days=366):
            return RedirectResponse("/portal?error=report_range", status_code=303)
    days = max(1, min(int(days), 90))
    customer_name, pdf_bytes = generate_report(customer_id, days=days,
                                               start=start_dt, end=end_dt)
    filename = f"{slugify(customer_name)}-report.pdf"
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
# -----------------------------------------------------------------------------

METRICS_TEMPLATES, FIRMWARE_TEMPLATES, BASELINE_TEMPLATES = load_templates()


def get_conn():
    # Session timezone Asia/Jakarta: every timestamptz comes back rendered
    # in WIB so pages match what the team's clocks say, while storage stays
    # absolute UTC instants. isoformat() values carry the +07:00 offset, so
    # snapshot links etc. round-trip through `WHERE time = %s` unchanged.
    conn = psycopg2.connect(DATABASE_URL, options="-c timezone=Asia/Jakarta")
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def is_online(last_seen_at):
    if not last_seen_at:
        return False
    age = datetime.now(timezone.utc) - last_seen_at
    return age.total_seconds() < 15 * 60  # no push in 15 min = flagged offline


def _ch_query(sql):
    """POST a query to ClickHouse's HTTP interface; return rows as lists of
    string cells (TabSeparated). Caller wraps for best-effort behaviour."""
    creds = urllib.parse.urlencode({"user": CH_USER, "password": CH_PASS})
    req = urllib.request.Request(f"{CLICKHOUSE_URL}/?{creds}", data=sql.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        text = resp.read().decode().strip()
    return [line.split("\t") for line in text.splitlines()] if text else []


def unattributed_exporters():
    """Exporter IPs seen in flows_raw (last 24h) not covered by exporter_map --
    the same learn-and-flag the sync logs to stdout, surfaced here for one-click
    assignment. Best-effort: ClickHouse down/absent -> [] (no error shown)."""
    try:
        rows = _ch_query(
            "SELECT exporter_ip, count() AS n, min(ts), max(ts) "
            "FROM flow.flows_raw WHERE ts > now()-86400 "
            "AND exporter_ip NOT IN (SELECT exporter_ip FROM flow.exporter_map) "
            "GROUP BY exporter_ip ORDER BY n DESC FORMAT TabSeparated"
        )
        return [{"ip": r[0], "n": int(r[1]), "first_seen": r[2], "last_seen": r[3]}
                for r in rows if len(r) == 4]
    except Exception:
        return []


def customer_has_flow(customer_id):
    """True if the customer has any flow exporter mapped in ClickHouse. Best-effort
    like report_lib.flow_enabled: CH down/absent -> False, so a transient blip just
    omits the (bonus) flow section from a shared dashboard rather than erroring."""
    try:
        rows = _ch_query(
            f"SELECT count() FROM flow.exporter_map WHERE customer_id = {int(customer_id)} "
            "FORMAT TabSeparated")
        return bool(rows) and int(rows[0][0]) > 0
    except Exception:
        return False


def _flow_redirect(router_id, redirect_to, error=None):
    """Back to the router edit page's Flow Attribution section, preserving the
    original ?redirect_to so Save/Cancel still return where the user came from."""
    q = {}
    if redirect_to:
        q["redirect_to"] = redirect_to
    if error:
        q["flow_error"] = error
    qs = ("?" + urllib.parse.urlencode(q)) if q else ""
    return RedirectResponse(f"/routers/{router_id}/edit{qs}#flow", status_code=303)


@app.get("/")
def list_routers(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, last_seen_at, last_deploy_status, priority FROM routers")
    routers = cur.fetchall()
    conn.close()

    total = len(routers)
    online = sum(1 for r in routers if is_online(r["last_seen_at"]))
    critical = sum(1 for r in routers if r["priority"] == "critical")
    ok_deploy = sum(1 for r in routers if r["last_deploy_status"] == "ok")
    fail_deploy = sum(1 for r in routers if r["last_deploy_status"] == "failed")

    stats = {
        "total": total,
        "online": online,
        "critical": critical,
        "ok_deploy": ok_deploy,
        "fail_deploy": fail_deploy
    }
    return templates.TemplateResponse("routers_list.html", {"request": request, "stats": stats})


@app.get("/api/routers")
def api_routers(
    search: str = "",
    page: int = 1,
    per_page: int = 25,
    sort_col: str = "customer_name",
    sort_dir: str = "asc"
):
    allowed_sort_cols = {
        "identity_name": "r.identity_name",
        "customer_name": "c.name",
        "mgmt_host": "r.mgmt_host",
        "status": "r.last_seen_at",
        "priority": "r.priority",
        "last_seen": "r.last_seen_at",
        "last_deploy": "r.last_deploy_at"
    }
    db_sort_col = allowed_sort_cols.get(sort_col, "c.name")
    db_sort_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

    offset = (page - 1) * per_page
    conn = get_conn()
    try:
        cur = conn.cursor()

        if search:
            search_query = f"%{search}%"
            cur.execute("""
                SELECT COUNT(*) AS count
                FROM routers r
                LEFT JOIN customers c ON c.id = r.customer_id
                WHERE r.identity_name ILIKE %s OR c.name ILIKE %s OR r.mgmt_host ILIKE %s
            """, (search_query, search_query, search_query))
            total = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT r.*, c.name AS customer_name
                FROM routers r
                LEFT JOIN customers c ON c.id = r.customer_id
                WHERE r.identity_name ILIKE %s OR c.name ILIKE %s OR r.mgmt_host ILIKE %s
                ORDER BY {db_sort_col} {db_sort_dir}, r.identity_name ASC
                LIMIT %s OFFSET %s
            """, (search_query, search_query, search_query, per_page, offset))
        else:
            cur.execute("SELECT COUNT(*) AS count FROM routers")
            total = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT r.*, c.name AS customer_name
                FROM routers r
                LEFT JOIN customers c ON c.id = r.customer_id
                ORDER BY {db_sort_col} {db_sort_dir}, r.identity_name ASC
                LIMIT %s OFFSET %s
            """, (per_page, offset))

        routers = cur.fetchall()
    finally:
        conn.close()

    for r in routers:
        r["online"] = is_online(r["last_seen_at"])
        if r["last_seen_at"]:
            r["last_seen_at"] = r["last_seen_at"].isoformat()
        if r["last_deploy_at"]:
            r["last_deploy_at"] = r["last_deploy_at"].isoformat()

    return {"data": routers, "total": total}


@app.get("/routers/new")
def new_router_form(request: Request, redirect_to: str = "/"):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "router_form.html",
        {"request": request, "router": None, "customers": customers,
         "default_admin_user": ROUTER_API_DEFAULT_USER,
         "default_admin_password": ROUTER_API_DEFAULT_PASSWORD,
         "redirect_to": _safe_next(redirect_to)}
    )


@app.post("/routers/new")
def create_router(
    identity_name: str = Form(...),
    customer_id: int = Form(...),
    mgmt_host: str = Form(None),
    mgmt_port: int = Form(8728),
    admin_user: str = Form(None),
    admin_password: str = Form(None),
    wan_interface: str = Form("ether1"),
    wan_interface_backup: str = Form(""),
    use_ssl: bool = Form(False),
    priority: str = Form("standard"),
    redirect_to: str = Form("/"),
):
    token = secrets.token_hex(24)
    mgmt_host_clean = mgmt_host.strip() if mgmt_host else None
    admin_user_clean = admin_user.strip() if admin_user else None
    admin_password_clean = admin_password.strip() if admin_password else None
    
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO routers (customer_id, identity_name, auth_token, mgmt_host, mgmt_port, admin_user, admin_password, wan_interface, wan_interface_backup, use_ssl, priority) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (customer_id, identity_name, token, mgmt_host_clean, mgmt_port, admin_user_clean, admin_password_clean, wan_interface, wan_interface_backup or None, use_ssl, priority),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(_safe_next(redirect_to), status_code=303)


@app.get("/routers/{router_id}/edit")
def edit_router_form(request: Request, router_id: int, redirect_to: str = "/"):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers WHERE id = %s", (router_id,))
    router = cur.fetchone()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    cur.execute(
        "SELECT id, cidr, note, created_at FROM router_flow_exporters "
        "WHERE router_id = %s ORDER BY cidr",
        (router_id,),
    )
    flow_exporters = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "router_form.html",
        {"request": request, "router": router, "customers": customers,
         "redirect_to": _safe_next(redirect_to),
         "flow_exporters": flow_exporters,
         "flow_tiers": VALID_FLOW_TIERS,
         "unattributed": unattributed_exporters(),
         "flow_error": request.query_params.get("flow_error", "")}
    )


@app.post("/routers/{router_id}/edit")
def update_router(
    router_id: int,
    identity_name: str = Form(...),
    customer_id: int = Form(...),
    mgmt_host: str = Form(None),
    mgmt_port: int = Form(8728),
    admin_user: str = Form(None),
    admin_password: str = Form(None),
    wan_interface: str = Form("ether1"),
    wan_interface_backup: str = Form(""),
    use_ssl: bool = Form(False),
    priority: str = Form("standard"),
    redirect_to: str = Form("/"),
):
    mgmt_host_clean = mgmt_host.strip() if mgmt_host else None
    admin_user_clean = admin_user.strip() if admin_user else None
    admin_password_clean = admin_password.strip() if admin_password else None

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE routers SET identity_name=%s, customer_id=%s, mgmt_host=%s, mgmt_port=%s, "
        "admin_user=%s, admin_password=%s, wan_interface=%s, wan_interface_backup=%s, use_ssl=%s, priority=%s WHERE id=%s",
        (identity_name, customer_id, mgmt_host_clean, mgmt_port, admin_user_clean, admin_password_clean, wan_interface, wan_interface_backup or None, use_ssl, priority, router_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(_safe_next(redirect_to), status_code=303)


@app.post("/routers/{router_id}/flow-tier")
def update_router_flow_tier(
    router_id: int,
    flow_enabled: bool = Form(False),
    flow_tier: str = Form(""),
    sampling_interval: str = Form(""),
    sampling_space: str = Form(""),
    redirect_to: str = Form(""),
):
    tier = flow_tier.strip() or None
    if tier is not None and tier not in VALID_FLOW_TIERS:
        tier = None
    # Sampling is optional and per-router: a positive interval turns it on,
    # anything else (blank/0/garbage) = off = full capture, both NULL.
    interval = space = None
    try:
        si = int(sampling_interval)
        if si > 0:
            interval = si
            space = max(int(sampling_space or 0), 0)
    except ValueError:
        interval = space = None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE routers SET flow_enabled = %s, flow_tier = %s, "
        "flow_sampling_interval = %s, flow_sampling_space = %s WHERE id = %s",
        (flow_enabled, tier, interval, space, router_id),
    )
    conn.commit()
    conn.close()
    return _flow_redirect(router_id, redirect_to)


@app.post("/routers/{router_id}/flow-exporters")
def add_flow_exporter(
    router_id: int,
    cidr: str = Form(...),
    note: str = Form(""),
    redirect_to: str = Form(""),
):
    # Validate exactly as the sync will read it (ipaddress + MAX_EXPAND), so a
    # row the Console accepts is a row the sync can actually attribute.
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        return _flow_redirect(router_id, redirect_to, error="bad_cidr")
    if net.num_addresses > FLOW_EXPAND_MAX:
        return _flow_redirect(router_id, redirect_to, error="range_too_big")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO router_flow_exporters (router_id, cidr, note) VALUES (%s, %s, %s)",
        (router_id, str(net), note.strip() or None),
    )
    conn.commit()
    conn.close()
    return _flow_redirect(router_id, redirect_to)


@app.post("/routers/{router_id}/flow-exporters/{row_id}/delete")
def delete_flow_exporter(router_id: int, row_id: int, redirect_to: str = Form("")):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM router_flow_exporters WHERE id = %s AND router_id = %s",
        (row_id, router_id),
    )
    conn.commit()
    conn.close()
    return _flow_redirect(router_id, redirect_to)


def sync_identity_name(router_id, current_identity, actual_identity):
    """
    Keeps routers.identity_name in sync with the router's real RouterOS
    identity after a successful deploy. Admin-entered values can drift
    from what's actually configured on the device -- confirmed in
    practice (MELIA/KESBANG had been onboarded with a shortened/friendly
    name instead of the router's exact identity) -- and the Loki `host`
    label used for per-router log correlation always reflects the real
    identity, not our record of it. Returns the new name if it changed,
    None if it already matched.
    """
    if actual_identity == current_identity:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE routers SET identity_name = %s WHERE id = %s", (actual_identity, router_id))
    conn.commit()
    conn.close()
    return actual_identity


def deploy_and_record(router):
    """
    Pushes to one router and records the outcome on its own row
    (last_deploy_status/at/detail) so a background batch (deploy_all_bg)
    gives live per-router progress instead of only a result visible at
    the end of one blocking HTTP request. Opens its own DB connection --
    callers may be running in a background thread with no request-scoped
    connection to reuse.

    Returns the same {"identity_name", "ok", "detail"} shape
    deploy_result.html already expects, so deploy_router (still
    synchronous -- a single router is quick enough not to need
    backgrounding) can keep using it directly.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        actual_identity, warnings = push_to_router(
            router, INGEST_BASE_URL, METRICS_TEMPLATES, FIRMWARE_TEMPLATES, SFTP_CONFIG, SYSLOG_CONFIG,
            baseline_templates=BASELINE_TEMPLATES, radius_config=RADIUS_CONFIG, gmedia_cidrs=GMEDIA_CIDRS,
            flow_config=FLOW_CONFIG,
        )
        renamed_to = sync_identity_name(router["id"], router["identity_name"], actual_identity)
        detail = "deployed" if not renamed_to else f"deployed (identity_name synced: {router['identity_name']!r} -> {renamed_to!r})"
        if warnings:
            detail += " -- " + " | ".join(f"WARNING: {w}" for w in warnings)
        result = {"identity_name": renamed_to or router["identity_name"], "ok": True, "detail": detail}
    except Exception as e:
        detail = str(e)
        if "not allowed by device-mode" in detail:
            detail += (
                " -- RouterOS 7.17+ device-mode lock: run"
                " /system/device-mode/update scheduler=yes fetch=yes"
                " on the router, then short-press its reset/mode button or"
                " power-cycle it within 5 minutes (soft reboot doesn't count),"
                " and deploy again. See routeros/README.md."
            )
        result = {"identity_name": router["identity_name"], "ok": False, "detail": detail}

    cur.execute(
        "UPDATE routers SET last_deploy_status = %s, last_deploy_at = %s, last_deploy_detail = %s WHERE id = %s",
        ("ok" if result["ok"] else "failed", datetime.now(timezone.utc), result["detail"], router["id"]),
    )
    conn.commit()
    conn.close()
    return result


def deploy_all_bg(router_ids):
    """
    Runs in a background thread (see /deploy-all) -- deliberately not
    async/awaited from the request handler, since 200 routers at a few
    seconds each is a 10-30+ minute batch that must not hold an HTTP
    request (and its proxy/browser timeout) open the whole time. Progress
    is visible via each router's own last_deploy_status/at/detail
    (routers_list.html), not via this function's return value.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers WHERE id = ANY(%s) ORDER BY identity_name", (router_ids,))
    routers = cur.fetchall()
    conn.close()
    for router in routers:
        # No management IP/credentials = unmanaged, not a deploy failure:
        # leave last_deploy_* untouched so the row keeps showing when the
        # router last actually received a script. (/deploy-all already
        # filters these out; this guards direct callers.)
        if not (router["mgmt_host"] and router["admin_user"] and router["admin_password"]):
            continue
        deploy_and_record(router)


@app.post("/routers/{router_id}/deploy")
def deploy_router(request: Request, router_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers WHERE id = %s", (router_id,))
    router = cur.fetchone()
    conn.close()

    if not router["mgmt_host"] or not router["admin_user"] or not router["admin_password"]:
        result = {
            "identity_name": router["identity_name"],
            "ok": False,
            "detail": "Failed: No management IP or credentials configured (configure manually for NAT routers)"
        }
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE routers SET last_deploy_status = %s, last_deploy_at = %s, last_deploy_detail = %s WHERE id = %s",
            ("failed", datetime.now(timezone.utc), result["detail"], router["id"]),
        )
        conn.commit()
        conn.close()
        return templates.TemplateResponse("deploy_result.html", {"request": request, "results": [result]})

    result = deploy_and_record(router)
    return templates.TemplateResponse("deploy_result.html", {"request": request, "results": [result]})


@app.get("/routers/{router_id}/manual-script")
def get_manual_script(request: Request, router_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers WHERE id = %s", (router_id,))
    router = cur.fetchone()
    conn.close()

    if not router:
        return RedirectResponse("/", status_code=303)

    token = router["auth_token"]
    wan_interface = router["wan_interface"] or "ether1"
    wan_interface_backup = router["wan_interface_backup"] or ""

    # Compile the metrics scripts
    metrics_tpl_v7 = METRICS_TEMPLATES["v7"]
    metrics_tpl_v6 = METRICS_TEMPLATES["v6"]

    # Ingest URL
    ingest_url = f"{INGEST_BASE_URL}/ingest"
    firmware_url = f"{INGEST_BASE_URL}/ingest/firmware"

    # Replacements for metrics
    metrics_src_v7 = metrics_tpl_v7.replace(
        '"https://monitor.yourisp.com/ingest"', f'"{ingest_url}"'
    ).replace(
        '"PER_ROUTER_AUTH_TOKEN"', f'"{token}"'
    ).replace(
        '"WAN_INTERFACE_PLACEHOLDER"', f'"{wan_interface}"'
    ).replace(
        '"WAN_INTERFACE_BACKUP_PLACEHOLDER"', f'"{wan_interface_backup}"'
    )

    metrics_src_v6 = metrics_tpl_v6.replace(
        '"https://monitor.yourisp.com/ingest"', f'"{ingest_url}"'
    ).replace(
        '"PER_ROUTER_AUTH_TOKEN"', f'"{token}"'
    ).replace(
        '"WAN_INTERFACE_PLACEHOLDER"', f'"{wan_interface}"'
    ).replace(
        '"WAN_INTERFACE_BACKUP_PLACEHOLDER"', f'"{wan_interface_backup}"'
    )

    # Compile the firmware scripts (v6/v7 split: show-sensitive doesn't
    # parse on RouterOS 6, same as the metrics scripts' split)
    def _compile_firmware(tpl):
        return tpl.replace(
            '"https://monitor.yourisp.com/ingest/firmware"', f'"{firmware_url}"'
        ).replace(
            '"PER_ROUTER_AUTH_TOKEN"', f'"{token}"'
        ).replace(
            '"SFTP_HOST_PLACEHOLDER"', f'"{SFTP_CONFIG["host"]}"'
        ).replace(
            '"SFTP_PORT_PLACEHOLDER"', f'"{SFTP_CONFIG["port"]}"'
        ).replace(
            '"SFTP_USER_PLACEHOLDER"', f'"{SFTP_CONFIG["user"]}"'
        ).replace(
            '"SFTP_PASSWORD_PLACEHOLDER"', f'"{SFTP_CONFIG["password"]}"'
        )

    firmware_src_v7 = _compile_firmware(FIRMWARE_TEMPLATES["v7"])
    firmware_src_v6 = _compile_firmware(FIRMWARE_TEMPLATES["v6"])

    # Use the pre-configured IP (SYSLOG_IP) for the copy-paste commands --
    # RouterOS 6.x rejects hostnames in the 'remote' field, so an IP is safer.
    syslog_host = SYSLOG_CONFIG["host"]
    syslog_port = SYSLOG_CONFIG["port"]
    syslog_ip = os.environ.get("SYSLOG_IP", syslog_host)

    return templates.TemplateResponse(
        "router_manual_script.html",
        {
            "request": request,
            "router": router,
            "metrics_src_v7": metrics_src_v7,
            "metrics_src_v6": metrics_src_v6,
            "firmware_src_v7": firmware_src_v7,
            "firmware_src_v6": firmware_src_v6,
            "syslog_host": syslog_host,
            "syslog_ip": syslog_ip,
            "syslog_port": syslog_port,
        },
    )


@app.post("/deploy-all")
def deploy_all(priority: str = Form(None)):
    """
    priority: pass "critical" to only deploy routers marked
    priority='critical' (the phased-rollout "critical customer first"
    path); omitted/blank deploys every router, same as before.

    Returns immediately once the background thread is started -- refresh
    the router list to watch last_deploy_status/at/detail update per
    router as it works through the batch, rather than waiting for one
    big result table at the end.

    Routers without a management IP/credentials are skipped up front
    (unmanaged, not failed -- their last_deploy_* fields stay untouched);
    the redirect carries started/skipped counts for the list-page banner.
    """
    conn = get_conn()
    cur = conn.cursor()
    if priority:
        cur.execute("SELECT id, mgmt_host, admin_user, admin_password FROM routers WHERE priority = %s", (priority,))
    else:
        cur.execute("SELECT id, mgmt_host, admin_user, admin_password FROM routers")
    rows = cur.fetchall()
    conn.close()

    deployable = [r["id"] for r in rows if r["mgmt_host"] and r["admin_user"] and r["admin_password"]]
    skipped = len(rows) - len(deployable)

    if deployable:
        threading.Thread(target=deploy_all_bg, args=(deployable,), daemon=True).start()
    return RedirectResponse(f"/?deploy_started={len(deployable)}&deploy_skipped={skipped}", status_code=303)


@app.get("/customers")
def list_customers(request: Request):
    return templates.TemplateResponse("customers_list.html", {"request": request})


@app.get("/api/customers")
def api_customers(
    search: str = "",
    page: int = 1,
    per_page: int = 25,
    sort_col: str = "name",
    sort_dir: str = "asc"
):
    allowed_sort_cols = {
        "name": "c.name",
        "address": "c.address",
        "connected": "COALESCE(r.router_count, 0) + COALESCE(s.site_count, 0)",
        "report_email": "c.report_email"
    }
    db_sort_col = allowed_sort_cols.get(sort_col, "c.name")
    db_sort_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

    base_query = """
        SELECT c.*,
               COALESCE(r.router_count, 0) AS router_count,
               COALESCE(s.site_count, 0) AS site_count
        FROM customers c
        LEFT JOIN (
            SELECT customer_id, COUNT(*) AS router_count
            FROM routers
            GROUP BY customer_id
        ) r ON r.customer_id = c.id
        LEFT JOIN (
            SELECT customer_id, COUNT(*) AS site_count
            FROM sites
            GROUP BY customer_id
        ) s ON s.customer_id = c.id
    """

    offset = (page - 1) * per_page
    conn = get_conn()
    try:
        cur = conn.cursor()

        if search:
            search_query = f"%{search}%"
            cur.execute("""
                SELECT COUNT(*) AS count
                FROM customers c
                WHERE c.name ILIKE %s OR c.address ILIKE %s OR c.report_email ILIKE %s
            """, (search_query, search_query, search_query))
            total = cur.fetchone()["count"]

            cur.execute(f"""
                {base_query}
                WHERE c.name ILIKE %s OR c.address ILIKE %s OR c.report_email ILIKE %s
                ORDER BY {db_sort_col} {db_sort_dir}, c.name ASC
                LIMIT %s OFFSET %s
            """, (search_query, search_query, search_query, per_page, offset))
        else:
            cur.execute("SELECT COUNT(*) AS count FROM customers")
            total = cur.fetchone()["count"]

            cur.execute(f"""
                {base_query}
                ORDER BY {db_sort_col} {db_sort_dir}, c.name ASC
                LIMIT %s OFFSET %s
            """, (per_page, offset))

        customers = cur.fetchall()
    finally:
        conn.close()

    return {"data": customers, "total": total}


@app.get("/customers/new")
def new_customer_form(request: Request):
    return templates.TemplateResponse("customer_new.html", {"request": request})


@app.post("/customers/new")
def create_customer(
    name: str = Form(...),
    address: str = Form(""),
    report_email: str = Form(None)
):
    report_email_clean = report_email.strip() if report_email else None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO customers (name, address, report_email) VALUES (%s, %s, %s)",
        (name, address or None, report_email_clean)
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/customers", status_code=303)


@app.get("/customers/{customer_id}")
def show_customer_detail(request: Request, customer_id: int):
    conn = get_conn()
    cur = conn.cursor()
    
    # Get Customer info
    cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
    customer = cur.fetchone()
    if not customer:
        conn.close()
        return RedirectResponse("/customers", status_code=303)
        
    # Get Customer Routers
    cur.execute("SELECT * FROM routers WHERE customer_id = %s ORDER BY identity_name", (customer_id,))
    routers = cur.fetchall()
    
    # Populate online flag on routers
    for r in routers:
        r["online"] = is_online(r["last_seen_at"])
        
    # Get Customer Sites joined with controller info
    cur.execute("""
        SELECT s.*, c.name AS controller_name 
        FROM sites s 
        LEFT JOIN controllers c ON c.id = s.controller_id 
        WHERE s.customer_id = %s 
        ORDER BY s.site_desc
    """, (customer_id,))
    sites = cur.fetchall()

    # Get Unassigned Sites (customer_id is NULL) to allow connecting them
    cur.execute("""
        SELECT s.*, c.name AS controller_name
        FROM sites s
        LEFT JOIN controllers c ON c.id = s.controller_id
        WHERE s.customer_id IS NULL
        ORDER BY s.site_desc, s.unifi_site_name
    """)
    unassigned_sites = cur.fetchall()
    
    # SLA & tickets for the month picked in the card (default: previous
    # month WIB -- the month a monthly report would be about).
    sla_month = _parse_month(request.query_params.get("sla_month", ""))
    if sla_month is None:
        now_wib = datetime.now(WIB).date()
        sla_month = (now_wib.replace(day=1) - timedelta(days=1)).replace(day=1)
    cur.execute(
        "SELECT id, service_id, service_name, node_count, sla_pct FROM customer_sla_services "
        "WHERE customer_id = %s AND month = %s ORDER BY service_id",
        (customer_id, sla_month),
    )
    sla_services = cur.fetchall()
    next_month = (sla_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    cur.execute(
        "SELECT id, ticket_no, tanggal, description, action, mttr_seconds, status FROM customer_tickets "
        "WHERE customer_id = %s AND tanggal >= %s AND tanggal < %s ORDER BY tanggal, ticket_no",
        (customer_id, sla_month, next_month),
    )
    sla_tickets = cur.fetchall()
    for t in sla_tickets:
        s = t["mttr_seconds"]
        t["mttr_hms"] = f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s is not None else ""

    # Topology attachments -- metadata only, never pull the BYTEA into the
    # page render (files are served by their own route below).
    cur.execute(
        "SELECT id, label, filename, content_type, size_bytes, uploaded_at "
        "FROM customer_topology_files WHERE customer_id = %s ORDER BY uploaded_at DESC",
        (customer_id,),
    )
    topology_files = cur.fetchall()

    portal_users = _portal_users(conn, customer_id)

    conn.close()

    return templates.TemplateResponse(
        "customer_detail.html",
        {
            "request": request,
            "customer": customer,
            "portal_users": portal_users,
            "routers": routers,
            "sites": sites,
            "unassigned_sites": unassigned_sites,
            "topology_files": topology_files,
            "report_start_default": (datetime.now(WIB) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "report_end_default": datetime.now(WIB).strftime("%Y-%m-%d"),
            "sla_month": sla_month.strftime("%Y-%m"),
            "sla_services": sla_services,
            "sla_tickets": sla_tickets
        }
    )


# Topology attachments: PDFs/images of the customer's network diagram,
# stored as BYTEA (they ride the nightly pg-backup). Whitelist maps each
# accepted content-type to its expected filename extensions -- both must
# agree, so neither a mislabeled upload nor a renamed one gets through.
# No SVG: it can carry scripts and these files are served inline.
TOPOLOGY_ALLOWED_TYPES = {
    "application/pdf": {".pdf"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
}
TOPOLOGY_MAX_BYTES = 16 * 1024 * 1024


@app.post("/customers/{customer_id}/topology")
async def upload_topology_file(customer_id: int, file: UploadFile = File(...), label: str = Form("")):
    ext = os.path.splitext(file.filename or "")[1].lower()
    allowed_exts = TOPOLOGY_ALLOWED_TYPES.get(file.content_type)
    if not allowed_exts or ext not in allowed_exts:
        return RedirectResponse(f"/customers/{customer_id}?error=topology_type", status_code=303)
    data = await file.read()
    if not data:
        return RedirectResponse(f"/customers/{customer_id}?error=topology_type", status_code=303)
    if len(data) > TOPOLOGY_MAX_BYTES:
        return RedirectResponse(f"/customers/{customer_id}?error=topology_size", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO customer_topology_files "
        "(customer_id, label, filename, content_type, size_bytes, data) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (customer_id, label.strip() or None, file.filename, file.content_type, len(data), data),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.get("/customers/{customer_id}/topology/{file_id}")
def get_topology_file(customer_id: int, file_id: int, download: int = 0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT filename, content_type, data FROM customer_topology_files "
        "WHERE id = %s AND customer_id = %s",
        (file_id, customer_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    # Headers are latin-1; fold the filename to ASCII so an exotic upload
    # name can't break the response.
    safe_name = (row["filename"] or "").encode("ascii", "ignore").decode().replace('"', "") or "topology"
    disposition = "attachment" if download else "inline"
    return Response(
        content=bytes(row["data"]),
        media_type=row["content_type"],
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# --- Manual SLA / ticket entry (interim source until the ERP API exists;
# see ERP_SLA_API.md -- the field names deliberately match that contract).

def _parse_month(value):
    """'YYYY-MM' -> first-of-month date, or None."""
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except (TypeError, ValueError):
        return None


def _parse_mttr(value):
    """'hh:mm:ss' or 'mm:ss' or plain minutes -> seconds, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0] * 60  # bare number = minutes
    return None


@app.post("/customers/{customer_id}/sla-services")
def upsert_sla_service(
    customer_id: int,
    month: str = Form(...),
    service_id: str = Form(...),
    service_name: str = Form(...),
    node_count: int = Form(1),
    sla_pct: float = Form(...),
):
    month_date = _parse_month(month)
    if month_date is None or not service_id.strip() or not (0 <= sla_pct <= 100):
        return RedirectResponse(f"/customers/{customer_id}?sla_month={month}&error=sla_input#sla", status_code=303)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO customer_sla_services (customer_id, month, service_id, service_name, node_count, sla_pct)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (customer_id, month, service_id)
        DO UPDATE SET service_name = EXCLUDED.service_name,
                      node_count = EXCLUDED.node_count,
                      sla_pct = EXCLUDED.sla_pct
        """,
        (customer_id, month_date, service_id.strip(), service_name.strip(), node_count, sla_pct),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/customers/{customer_id}?sla_month={month}#sla", status_code=303)


@app.post("/customers/{customer_id}/sla-services/{row_id}/delete")
def delete_sla_service(customer_id: int, row_id: int, sla_month: str = Form("")):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM customer_sla_services WHERE id = %s AND customer_id = %s", (row_id, customer_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/customers/{customer_id}?sla_month={sla_month}#sla", status_code=303)


@app.post("/customers/{customer_id}/tickets")
def create_ticket(
    customer_id: int,
    sla_month: str = Form(""),
    ticket_no: str = Form(...),
    tanggal: str = Form(...),
    description: str = Form(""),
    action: str = Form(""),
    mttr: str = Form(""),
    status: str = Form("closed"),
):
    try:
        tanggal_date = datetime.strptime(tanggal, "%Y-%m-%d").date()
    except ValueError:
        return RedirectResponse(f"/customers/{customer_id}?sla_month={sla_month}&error=sla_input#sla", status_code=303)
    if not ticket_no.strip():
        return RedirectResponse(f"/customers/{customer_id}?sla_month={sla_month}&error=sla_input#sla", status_code=303)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO customer_tickets (customer_id, ticket_no, tanggal, description, action, mttr_seconds, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (customer_id, ticket_no.strip(), tanggal_date, description.strip() or None,
         action.strip() or None, _parse_mttr(mttr), status.strip() or "closed"),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/customers/{customer_id}?sla_month={sla_month}#sla", status_code=303)


@app.post("/customers/{customer_id}/tickets/{row_id}/delete")
def delete_ticket(customer_id: int, row_id: int, sla_month: str = Form("")):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM customer_tickets WHERE id = %s AND customer_id = %s", (row_id, customer_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/customers/{customer_id}?sla_month={sla_month}#sla", status_code=303)


@app.post("/customers/{customer_id}/topology/{file_id}/delete")
def delete_topology_file(customer_id: int, file_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM customer_topology_files WHERE id = %s AND customer_id = %s",
        (file_id, customer_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.get("/customers/{customer_id}/edit")
def edit_customer_form(request: Request, customer_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
    customer = cur.fetchone()
    conn.close()
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    return templates.TemplateResponse(
        "customer_form.html", {"request": request, "customer": customer}
    )


@app.post("/customers/{customer_id}/edit")
def update_customer(
    customer_id: int,
    name: str = Form(...),
    address: str = Form(""),
    report_email: str = Form(""),
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE customers SET name = %s, address = %s, report_email = %s WHERE id = %s",
        (name.strip(), address.strip() or None, report_email.strip() or None, customer_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/{customer_id}/delete")
def delete_customer(customer_id: int):
    conn = get_conn()
    cur = conn.cursor()
    
    # Check for active routers
    cur.execute("SELECT count(*) AS cnt FROM routers WHERE customer_id = %s", (customer_id,))
    router_count = cur.fetchone()["cnt"]
    
    # Check for active sites
    cur.execute("SELECT count(*) AS cnt FROM sites WHERE customer_id = %s", (customer_id,))
    site_count = cur.fetchone()["cnt"]
    
    if router_count > 0 or site_count > 0:
        conn.close()
        return RedirectResponse("/customers?error=cannot_delete_active", status_code=303)
        
    cur.execute("DELETE FROM customers WHERE id = %s", (customer_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/{customer_id}/share-dashboard")
def share_dashboard(request: Request, customer_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
    customer = cur.fetchone()
    # Per-customer section flags: strip sections the customer has no data for so
    # the shared clone never shows empty router/wireless/flow panels.
    cur.execute("SELECT count(*) AS n FROM routers WHERE customer_id = %s", (customer_id,))
    has_routers = cur.fetchone()["n"] > 0
    cur.execute("SELECT count(*) AS n FROM sites WHERE customer_id = %s", (customer_id,))
    has_wireless = cur.fetchone()["n"] > 0
    conn.close()
    flags = {"has_routers": has_routers, "has_wireless": has_wireless,
             "include_flow": customer_has_flow(customer_id)}

    try:
        url = share_dashboard_for_customer(customer_id, customer["name"], flags)
        result = {"ok": True, "detail": url}
    except Exception as e:
        result = {"ok": False, "detail": str(e)}

    return templates.TemplateResponse(
        "share_dashboard_result.html", {"request": request, "customer": customer, "result": result}
    )


@app.get("/customers/{customer_id}/report")
def download_report(customer_id: int, days: int = 30, start: str = "", end: str = ""):
    """
    Generates the customer's QoE PDF on demand. Synchronous by design --
    ~6 panel renders at 1-3s each is an acceptable wait for a click, and
    the browser shows its own loading state.

    Period: explicit ?start=YYYY-MM-DD&end=YYYY-MM-DD (interpreted as
    whole WIB days, inclusive) wins over ?days=N (trailing window,
    default 30 -- the customers-list button and back-compat path).
    """
    start_dt = end_dt = None
    if start and end:
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=WIB)
            # inclusive end date -> exclusive next-midnight bound
            end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=WIB) + timedelta(days=1)
        except ValueError:
            return RedirectResponse(f"/customers/{customer_id}?error=report_range", status_code=303)
        end_dt = min(end_dt, datetime.now(WIB))
        if start_dt >= end_dt or (end_dt - start_dt).days > 366:
            return RedirectResponse(f"/customers/{customer_id}?error=report_range", status_code=303)

    customer_name, pdf_bytes = generate_report(customer_id, days=days, start=start_dt, end=end_dt)
    if start_dt is not None:
        period_tag = f"{start.replace('-', '')}-{end.replace('-', '')}"
    else:
        period_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"qoe-report-{slugify(customer_name)}-{period_tag}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/customers/{customer_id}/report-email")
def set_report_email(customer_id: int, report_email: str = Form("")):
    """
    Sets/clears the address the monthly reporter emails this customer's
    PDF to. Blank = don't email (the reporter skips those customers).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE customers SET report_email = %s WHERE id = %s",
        (report_email.strip() or None, customer_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/customers", status_code=303)


@app.get("/sites")
def list_sites(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT customer_id FROM sites")
    sites = cur.fetchall()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()

    total = len(sites)
    collecting = sum(1 for s in sites if s["customer_id"] is not None)
    not_collected = total - collecting

    stats = {
        "total": total,
        "collecting": collecting,
        "not_collected": not_collected
    }
    return templates.TemplateResponse(
        "sites_list.html", {"request": request, "stats": stats, "customers": customers}
    )


@app.get("/api/sites")
def api_sites(
    search: str = "",
    page: int = 1,
    per_page: int = 25,
    sort_col: str = "site_name",
    sort_dir: str = "asc"
):
    allowed_sort_cols = {
        "collecting": "s.customer_id",
        "controller_name": "ctl.name",
        "site_name": "COALESCE(s.site_desc, s.unifi_site_name)",
        "discovered": "s.discovered_at",
        "customer_name": "c.name"
    }
    db_sort_col = allowed_sort_cols.get(sort_col, "COALESCE(s.site_desc, s.unifi_site_name)")
    db_sort_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

    offset = (page - 1) * per_page
    conn = get_conn()
    try:
        cur = conn.cursor()

        if search:
            search_query = f"%{search}%"
            cur.execute("""
                SELECT COUNT(*) AS count
                FROM sites s
                LEFT JOIN controllers ctl ON ctl.id = s.controller_id
                LEFT JOIN customers c ON c.id = s.customer_id
                WHERE s.site_desc ILIKE %s OR s.unifi_site_name ILIKE %s OR ctl.name ILIKE %s OR c.name ILIKE %s
            """, (search_query, search_query, search_query, search_query))
            total = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT s.*, ctl.name AS controller_name, c.name AS customer_name
                FROM sites s
                LEFT JOIN controllers ctl ON ctl.id = s.controller_id
                LEFT JOIN customers c ON c.id = s.customer_id
                WHERE s.site_desc ILIKE %s OR s.unifi_site_name ILIKE %s OR ctl.name ILIKE %s OR c.name ILIKE %s
                ORDER BY {db_sort_col} {db_sort_dir}
                LIMIT %s OFFSET %s
            """, (search_query, search_query, search_query, search_query, per_page, offset))
        else:
            cur.execute("SELECT COUNT(*) AS count FROM sites")
            total = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT s.*, ctl.name AS controller_name, c.name AS customer_name
                FROM sites s
                LEFT JOIN controllers ctl ON ctl.id = s.controller_id
                LEFT JOIN customers c ON c.id = s.customer_id
                ORDER BY {db_sort_col} {db_sort_dir}
                LIMIT %s OFFSET %s
            """, (per_page, offset))

        sites = cur.fetchall()
    finally:
        conn.close()

    for s in sites:
        if s["discovered_at"]:
            s["discovered_at"] = s["discovered_at"].isoformat()

    return {"data": sites, "total": total}


@app.post("/sites/{site_id}/assign")
def assign_site(site_id: int, customer_id: int = Form(...), redirect_to: str = "/sites"):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE sites SET customer_id = %s WHERE id = %s", (customer_id, site_id))
    conn.commit()
    conn.close()
    # Only allow same-site relative paths -- never an absolute/scheme URL or
    # protocol-relative "//host" -- so redirect_to can't be used as an open
    # redirect off to another site.
    if not (redirect_to.startswith("/") and not redirect_to.startswith("//")):
        redirect_to = "/sites"
    return RedirectResponse(redirect_to, status_code=303)


@app.get("/config-snapshots/{router_id}")
def list_config_snapshots(request: Request, router_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT identity_name FROM routers WHERE id = %s", (router_id,))
    router = cur.fetchone()
    cur.execute(
        "SELECT time, size_bytes FROM router_config_snapshots "
        "WHERE router_id = %s ORDER BY time DESC LIMIT 90",
        (router_id,),
    )
    snapshots = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "config_snapshots.html",
        {"request": request, "router": router, "router_id": router_id, "snapshots": snapshots},
    )


@app.get("/config-snapshots/{router_id}/{timestamp}")
def download_config_snapshot(router_id: int, timestamp: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT r.identity_name, cs.config_text FROM router_config_snapshots cs "
        "JOIN routers r ON r.id = cs.router_id WHERE cs.router_id = %s AND cs.time = %s",
        (router_id, timestamp),
    )
    row = cur.fetchone()
    conn.close()
    filename = f"{row['identity_name']}-{timestamp}.rsc"
    return PlainTextResponse(
        row["config_text"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/config-snapshots/{router_id}/{timestamp}/view")
def view_config_snapshot(request: Request, router_id: int, timestamp: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT cs.config_text, r.identity_name FROM router_config_snapshots cs "
        "JOIN routers r ON r.id = cs.router_id WHERE cs.router_id = %s AND cs.time = %s",
        (router_id, timestamp),
    )
    row = cur.fetchone()
    conn.close()

    rendered = [
        f'<span class="diff-line"><span class="line-no">{i}</span>{html.escape(line)}</span>'
        for i, line in enumerate(row["config_text"].splitlines(), start=1)
    ]
    config_html = "".join(rendered)

    return templates.TemplateResponse(
        "config_view.html",
        {
            "request": request, 
            "router_id": router_id, 
            "identity_name": row["identity_name"],
            "timestamp": timestamp, 
            "config_html": config_html
        },
    )


# RouterOS renders 1k / 1M / 1G bandwidth-style values as either the suffixed
# or the raw form depending on build -- both mean the same number.
_ROS_SUFFIX = {"k": 1_000, "M": 1_000_000, "G": 1_000_000_000}


def normalise_ros_config(text):
    """
    Canonicalise a RouterOS `/export` for diffing, so two exports of the
    SAME config always compare equal even when RouterOS rendered them
    differently. Applied to both sides of the diff view only -- stored
    snapshots stay byte-faithful (they're recovery artefacts).

    Measured on a real fleet router pair: cuts a 972-changed-line diff to
    ~274, all of them genuine changes. Three noise classes handled:

    1. Line-wrap shift (the big one): exports wrap long lines with a
       trailing `\\` + 4-space-indented continuation, breaking even
       mid-token. Any value-length change shifts every later wrap point in
       the statement, producing phantom diffs like `disabled=no` -> `\\`.
       Unwrapping joins continuations with NO separator (mid-token breaks).
    2. Boolean rendering: true/false vs yes/no across builds.
    3. Unit suffixes: max-limit=100M/100M vs 100000000/100000000 (also
       pcq-rate, limit-at, cache-size=81920KiB vs 81920, ...). Anchored to
       value position (right after = or /, followed by a boundary) so enum
       values like advertise=1000M-full, speed=1Gbps, and quoted names
       like name="30M - DN" are never touched.
    """
    text = re.sub(r'\\\n    ', '', text)
    text = re.sub(r'\b([\w-]+=)true\b',  r'\1yes', text)
    text = re.sub(r'\b([\w-]+=)false\b', r'\1no',  text)

    def expand(m):
        return str(int(float(m.group(1)) * _ROS_SUFFIX[m.group(2)]))

    text = re.sub(r'(?<==)(\d+(?:\.\d+)?)([kMG])(?=[\s/]|$)', expand, text, flags=re.M)
    text = re.sub(r'(?<=/)(\d+(?:\.\d+)?)([kMG])(?=[\s/]|$)', expand, text, flags=re.M)
    text = re.sub(r'(?<==)(\d+)KiB(?=\s|$)', r'\1', text, flags=re.M)
    return text


@app.get("/config-snapshots/{router_id}/{timestamp}/diff")
def diff_config_snapshot(request: Request, router_id: int, timestamp: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT cs.config_text, cs.time, r.identity_name FROM router_config_snapshots cs "
        "JOIN routers r ON r.id = cs.router_id WHERE cs.router_id = %s AND cs.time = %s",
        (router_id, timestamp),
    )
    current = cur.fetchone()
    cur.execute(
        "SELECT config_text, time FROM router_config_snapshots "
        "WHERE router_id = %s AND time < %s ORDER BY time DESC LIMIT 1",
        (router_id, timestamp),
    )
    previous = cur.fetchone()
    # For prev/next navigation between diffs -- "older" just reuses the
    # previous snapshot's own timestamp (its diff page compares it against
    # whatever came before *it*); "newer" needs a separate lookup since we
    # haven't otherwise fetched anything past `current`.
    cur.execute(
        "SELECT time FROM router_config_snapshots "
        "WHERE router_id = %s AND time > %s ORDER BY time ASC LIMIT 1",
        (router_id, timestamp),
    )
    newer = cur.fetchone()
    conn.close()

    diff_html = None
    if previous:
        prev_lines = normalise_ros_config(previous["config_text"]).splitlines()
        curr_lines = normalise_ros_config(current["config_text"]).splitlines()

        diff_lines = difflib.unified_diff(
            prev_lines,
            curr_lines,
            fromfile=str(previous["time"]),
            tofile=str(current["time"]),
            lineterm="",
        )
        # Built here rather than looped in the template -- avoids Jinja2
        # whitespace-control pitfalls (a stray newline between the {% for %}
        # tags was doubling up with .diff-line's `display: block`, double-
        # spacing every line).
        #
        # Two line-number columns (old file, new file) since a unified diff
        # tracks both -- a hunk header ("@@ -12,4 +12,7 @@") gives the
        # starting point for each; context lines advance both counters,
        # +lines advance only the new one, -lines only the old one.
        hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
        old_no = new_no = None
        rendered = []
        for line in diff_lines:
            if line.startswith("+++") or line.startswith("---"):
                rendered.append(f'<span class="diff-line">{html.escape(line)}</span>')
                continue
            if line.startswith("@@"):
                m = hunk_re.match(line)
                if m:
                    old_no, new_no = int(m.group(1)), int(m.group(2))
                rendered.append(f'<span class="diff-line diff-hunk">{html.escape(line)}</span>')
                continue

            if line.startswith("+"):
                old_disp, css_class = "", " diff-add"
                new_disp = str(new_no) if new_no is not None else ""
                if new_no is not None:
                    new_no += 1
            elif line.startswith("-"):
                new_disp, css_class = "", " diff-del"
                old_disp = str(old_no) if old_no is not None else ""
                if old_no is not None:
                    old_no += 1
            else:
                css_class = ""
                old_disp = str(old_no) if old_no is not None else ""
                new_disp = str(new_no) if new_no is not None else ""
                if old_no is not None:
                    old_no += 1
                if new_no is not None:
                    new_no += 1

            rendered.append(
                f'<span class="diff-line{css_class}">'
                f'<span class="line-no">{old_disp}</span><span class="line-no">{new_disp}</span>'
                f'{html.escape(line)}</span>'
            )
        # Joined with no separator -- .diff-line's `display: block` is what
        # puts each line on its own row; adding a literal newline here too
        # would double-space every line.
        diff_html = "".join(rendered)

    return templates.TemplateResponse(
        "config_diff.html",
        {
            "request": request,
            "router_id": router_id,
            "identity_name": current["identity_name"] if current else None,
            "diff_html": diff_html,
            "older_timestamp": previous["time"].isoformat() if previous else None,
            "newer_timestamp": newer["time"].isoformat() if newer else None,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
