"""
report_lib.py -- builds the customer-facing QoE PDF report.

Shared by admin-ui (on-demand "Download PDF report" button) and the
reporter container (monthly email) -- both Dockerfiles COPY this file, the
same shared-lib pattern as routeros/deploy_lib.py, so the two callers
can't drift.

Pipeline per report: regenerate the customer-locked dashboard clone (so
panels always reflect the current dashboard definitions), render a curated
set of its panels to PNG via the grafana-image-renderer service, pull
headline KPI numbers + manual SLA/ticket entries straight from TimescaleDB,
and assemble everything into a bilingual (English/Indonesian) PDF by
rendering report_template.html (Lumina Console design tokens) through
WeasyPrint.

Panel images come from the LOCKED clone (uid customer-<slug>), not the
master dashboard -- the clone has customer_id baked into every query and
the NOC-only rows stripped, so a rendering mistake can't leak another
customer's data into a customer-facing document.
"""

import base64
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

import jinja2
import psycopg2
import psycopg2.extras
from weasyprint import HTML

import charts
from dashboard_share import slugify

DATABASE_URL = os.environ["DATABASE_URL"]

# Traffic-flow analytics live in the sibling flow stack (ClickHouse). The flow
# section is best-effort -- if ClickHouse is down or a customer has no flows,
# the report drops it silently.
CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
CLICKHOUSE_USER = os.environ.get("CH_USER", "flow")
CLICKHOUSE_PASSWORD = os.environ.get("CH_PASS", "")

WIB = timezone(timedelta(hours=7))  # Asia/Jakarta

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_NAME = "report_template.html"
# Full wordmark if the operator has dropped it in (assets/gmedia-logo.png),
# else the G-mark + a text wordmark rendered by the template.
LOGO_FULL_PATH = os.path.join(_APP_DIR, "assets", "gmedia-logo.png")
LOGO_MARK_PATH = os.path.join(_APP_DIR, "assets", "g-mark.png")

MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
             "Juli", "Agustus", "September", "Oktober", "November", "Desember"]


def _fmt_pct(v):
    return f"{float(v):.2f}%" if v is not None else "n/a"


def _fmt_hms(seconds):
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _png_uri(png_bytes):
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()


def _fmt_bytes(n):
    """Human byte total (IEC) for the providers table."""
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.2f} {unit}")
        n /= 1024


# Font Awesome 7 Free glyphs (PUA codepoints) for the Top Content Providers
# table. Brand logos live in the Brands face ("Font Awesome 7 Brands"); the
# globe fallback is in the Solid face ("Font Awesome 7 Free", weight 900). The
# OTFs are vendored in assets/fonts/ and installed as system fonts by the
# Dockerfiles (same path as Montserrat), so WeasyPrint finds them via pango.
# On FA7 (not FA6) so the Roblox Creator Studio glyph (added in v7) is available.
_ICON_BRANDS = "fa-b"  # CSS class -> font-family "Font Awesome 7 Brands"
_ICON_SOLID = "fa-s"   # CSS class -> font-family "Font Awesome 7 Free" (900)


def _provider_icon(label):
    """(glyph_char, css_font_class) for a provider label. A brand glyph when we
    recognise the network, a globe otherwise. Matched on the uppercased label;
    cache-qualified checks come first so GOOGLE-CACHE -> YouTube beats the plain
    GOOGLE rule. Keys are substrings of the iptoasn ASN name (so AKAMAI-LINODE
    matches LINODE). Monochrome by design (pure Font Awesome). Networks with no
    Font Awesome glyph -- Fastly, Akamai, Netflix, Alibaba/Taobao, Shopee, and
    every ISP/CDN -- fall through to the globe. Codepoints are FA7 Free PUA,
    verified against the vendored fa-brands-400.otf cmap."""
    u = (label or "").upper()
    # Google family -- on-net cache first (GOOGLE-CDN/-CACHE (GMEDIA-*) is the
    # YouTube-heavy cache node, so it gets the YouTube mark, not the Google one).
    # Both spellings matched: cdn_override labels were renamed *-CACHE -> *-CDN
    # on 2026-07-27 and history predating the re-backfill may still say CACHE.
    if "GOOGLE" in u and ("CACHE" in u or "CDN" in u):
        return "", _ICON_BRANDS   # youtube
    if "YOUTUBE" in u:
        return "", _ICON_BRANDS   # youtube
    if "GOOGLE" in u:
        return "", _ICON_BRANDS   # google
    # Meta -- Facebook/Instagram/WhatsApp traffic all resolves to the FB ASN.
    if "META" in u or "FACEBOOK" in u:
        return "", _ICON_BRANDS   # meta
    if "TIKTOK" in u or "BYTEDANCE" in u:
        return "", _ICON_BRANDS   # tiktok
    # Other recognisable networks in the fleet's top providers.
    if "AMAZON" in u or "AWS" in u:
        return "", _ICON_BRANDS   # amazon
    if "APPLE" in u:
        return "", _ICON_BRANDS   # apple
    if "CLOUDFLARE" in u:
        return "", _ICON_BRANDS   # cloudflare
    if "MICROSOFT" in u:
        return "", _ICON_BRANDS   # microsoft
    if "TELEGRAM" in u:
        return "", _ICON_BRANDS   # telegram
    if "LINODE" in u:
        return "", _ICON_BRANDS   # linode (Akamai Connected Cloud)
    if "DIGITALOCEAN" in u:
        return "", _ICON_BRANDS   # digitalocean
    if "ROBLOX" in u:
        return "", _ICON_BRANDS   # roblox-creator-studio
    return "", _ICON_SOLID        # globe (fastly / akamai / gmedia / ISPs)

# Controller model codes -> "[Brand] [commercial name]" for the customer-facing
# inventory. The controllers report internal codes, which mean nothing to a
# hotel's IT contact.
#
# ⚠️ The UniFi "U7*" prefix is the AC-era chipset family, NOT Wi-Fi 7: U7PG2 is
# the AC Pro and U7LT the AC Lite. The fleet also contains U7PRO, which IS the
# Wi-Fi 7 U7 Pro -- so mapping U7PG2 to "U7 Pro" would give two different
# products the same name and describe 221 Wi-Fi 5 APs as Wi-Fi 7.
AP_MODEL_NAMES = {
    # UniFi -- original
    "BZ2": "UniFi AP", "BZ2LR": "UniFi AP LR",
    "U2O": "UniFi AP Outdoor", "U2Sv2": "UniFi AP v2",
    # UniFi -- AC generation (Wi-Fi 5)
    "U7LT": "UniFi AC Lite", "U7LR": "UniFi AC LR", "U7PG2": "UniFi AC Pro",
    "U7MP": "UniFi AC Mesh Pro", "U7MSH": "UniFi AC Mesh",
    "U7HD": "UniFi AC HD", "U7NHD": "UniFi nanoHD",
    # UniFi -- Wi-Fi 6 / 7
    "UAP6MP": "UniFi U6 Pro", "UALR6v2": "UniFi U6 LR", "U6M": "UniFi U6 Mesh",
    "U7PRO": "UniFi U7 Pro",
    # UniFi switches (they land in ap_inventory too)
    "US24P250": "UniFi Switch 24 PoE", "US24PRO": "UniFi Switch Pro 24",
    "US48P500": "UniFi Switch 48 PoE", "USL48P": "UniFi Switch Lite 48 PoE",
    # Ruijie
    "AP680(CD)": "Ruijie RG-AP680(CD)", "AP720-L": "Ruijie RG-AP720-L",
    "AP820-L(V2)": "Ruijie RG-AP820-L (V2)", "AP820-L(V3)": "Ruijie RG-AP820-L (V3)",
    "AP840-L": "Ruijie RG-AP840-L",
    "RAP2200(E)": "Ruijie RG-RAP2200(E)", "RAP2260(G)": "Ruijie RG-RAP2260(G)",
    "RAP6262(G)": "Ruijie RG-RAP6262(G)", "RAP73Pro": "Ruijie RG-RAP73 Pro",
}


def ap_model_name(code):
    """Friendly name for a controller model code. Unknown codes fall back to
    brand + the raw code rather than a guess -- a wrong product name on a
    customer's equipment list is worse than an unfamiliar one."""
    if not code:
        return "Unknown"
    code = code.strip()
    if code in AP_MODEL_NAMES:
        return AP_MODEL_NAMES[code]
    if code.upper().startswith(("RAP", "AP")):
        return f"Ruijie RG-{code}"
    if code.upper().startswith(("U", "BZ", "US")):
        return f"UniFi {code}"
    return code


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def flow_enabled(customer_id):
    """True if this customer has traffic-flow collection (an exporter_map row).
    ClickHouse is a sibling stack; if it's unreachable, return False so the
    report still generates -- the flow section is a bonus, never a dependency."""
    try:
        q = f"SELECT count() FROM flow.exporter_map WHERE customer_id = {int(customer_id)}"
        creds = urllib.parse.urlencode({"user": CLICKHOUSE_USER, "password": CLICKHOUSE_PASSWORD})
        req = urllib.request.Request(f"{CLICKHOUSE_URL}/?{creds}", data=q.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int((resp.read().decode().strip() or "0")) > 0
    except Exception:
        return False


def collect_flow_providers(customer_id, from_ms, to_ms, limit=15):
    """Top content providers for the period, straight from the ClickHouse
    provider_hourly rollup -- the same query flow-overview panel 1 runs, but
    returned as rows so the PDF can render a branded table instead of a flat
    Grafana PNG. Each row carries a Font Awesome brand glyph via _provider_icon.
    Best-effort: returns [] on any ClickHouse error, so the caller just drops
    the section (same posture as flow_enabled). Note the `AS total` alias --
    ClickHouse rejects `sum(bytes) AS bytes` (String collision)."""
    from_s = int(from_ms // 1000)
    to_s = int(to_ms // 1000)
    q = (
        "SELECT provider, sum(bytes) AS total FROM flow.provider_hourly "
        "WHERE exporter_ip IN (SELECT exporter_ip FROM flow.exporter_map "
        f"WHERE customer_id = {int(customer_id)}) "
        f"AND hour >= toDateTime({from_s}) AND hour < toDateTime({to_s}) "
        f"GROUP BY provider ORDER BY total DESC LIMIT {int(limit)} "
        "FORMAT TabSeparated"
    )
    try:
        creds = urllib.parse.urlencode({"user": CLICKHOUSE_USER, "password": CLICKHOUSE_PASSWORD})
        req = urllib.request.Request(f"{CLICKHOUSE_URL}/?{creds}", data=q.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
    except Exception:
        return []
    rows = []
    for line in text.splitlines():
        provider, tab, total = line.partition("\t")
        if not tab:
            continue
        try:
            total = int(total)
        except ValueError:
            continue
        icon, icon_font = _provider_icon(provider)
        rows.append({
            "provider": provider,
            "bytes_human": _fmt_bytes(total),
            "icon": icon,
            "icon_font": icon_font,
        })
    return rows


def collect_kpis(customer_id, start, end):
    """
    Headline numbers for the cover page. Period averages for the path
    metrics (what was the period actually like), current 15-minute values
    for the wireless state (what does it look like right now) -- same SQL
    shapes as the dashboard cards, customer-scoped.
    """
    conn = get_conn()
    cur = conn.cursor()

    # Placeholder rows from deploy-time manual runs report 0ms/100% loss
    # (the documented scheduler-only ping quirk) -- a handful per month,
    # but a 100%-loss row visibly skews a monthly loss average, so they
    # are excluded from the period aggregates.
    cur.execute(
        """
        SELECT round(avg(pm.rtt_avg_ms)::numeric, 1) AS avg_latency,
               round(avg(pm.jitter_ms)::numeric, 1) AS avg_jitter,
               round(avg(pm.packet_loss_pct)::numeric, 2) AS avg_loss
        FROM path_metrics pm JOIN routers r ON r.id = pm.router_id
        WHERE r.customer_id = %s AND pm.time >= %s AND pm.time < %s
          AND NOT (pm.rtt_avg_ms = 0 AND pm.packet_loss_pct = 100)
        """,
        (customer_id, start, end),
    )
    path = cur.fetchone()

    cur.execute(
        """
        SELECT round(avg(100.0 * tx_retries / NULLIF(wifi_tx_attempts, 0))::numeric, 1) AS avg_retry
        FROM client_metrics cm JOIN sites s ON s.id = cm.site_id
        WHERE s.customer_id = %s AND cm.time >= %s AND cm.time < %s
          AND cm.is_wired = false
        """,
        (customer_id, start, end),
    )
    retry = cur.fetchone()

    cur.execute(
        """
        SELECT count(*) FILTER (WHERE NOT is_wired) AS wifi_clients,
               round(avg(satisfaction) FILTER (WHERE NOT is_wired)::numeric, 1) AS avg_satisfaction,
               round(avg(signal - noise) FILTER (WHERE NOT is_wired)::numeric, 1) AS avg_snr
        FROM (SELECT DISTINCT ON (cm.site_id, cm.client_mac) cm.is_wired, cm.satisfaction, cm.signal, cm.noise
              FROM client_metrics cm JOIN sites s ON s.id = cm.site_id
              WHERE s.customer_id = %s AND cm.time > now() - interval '15 minutes'
              ORDER BY cm.site_id, cm.client_mac, cm.time DESC) latest
        """,
        (customer_id,),
    )
    wifi = cur.fetchone()

    cur.execute(
        """
        SELECT count(*) FILTER (WHERE state = 1) AS aps_online,
               count(*) FILTER (WHERE state != 1 OR state IS NULL) AS aps_offline
        FROM (SELECT DISTINCT ON (ai.ap_mac) ai.state
              FROM ap_inventory ai JOIN sites s ON s.id = ai.site_id
              WHERE s.customer_id = %s AND ai.time > now() - interval '15 minutes'
              ORDER BY ai.ap_mac, ai.time DESC) latest
        """,
        (customer_id,),
    )
    aps = cur.fetchone()
    conn.close()

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "n/a"

    return [
        # (english label, indonesian label, value)
        ("Average Latency", "Rata-rata Latensi", fmt(path["avg_latency"], " ms")),
        ("Average Jitter", "Rata-rata Jitter", fmt(path["avg_jitter"], " ms")),
        ("Average Packet Loss", "Rata-rata Kehilangan Paket", fmt(path["avg_loss"], " %")),
        ("Average Wi-Fi Retry Rate", "Rata-rata Pengulangan Wi-Fi", fmt(retry["avg_retry"], " %")),
        ("Wi-Fi Clients Connected (now)", "Perangkat Wi-Fi Terhubung (saat ini)", fmt(wifi["wifi_clients"])),
        ("Average Wi-Fi Satisfaction (now)", "Rata-rata Kepuasan Wi-Fi (saat ini)", fmt(wifi["avg_satisfaction"], " %")),
        ("Average Signal Quality / SNR (now)", "Rata-rata Kualitas Sinyal / SNR (saat ini)", fmt(wifi["avg_snr"], " dB")),
        ("Access Points Online (now)", "Access Point Aktif (saat ini)", f"{aps['aps_online'] or 0} / {(aps['aps_online'] or 0) + (aps['aps_offline'] or 0)}"),
    ]


def collect_ap_rows(customer_id):
    """
    Full AP list for the report -- same latest-row-per-AP shape and 15-min
    freshness window as the 'Access Points Online (now)' KPI, so the table
    always agrees with the cover-page count. Offline APs sort first so
    problems land on the first page. Returns (rows, summary_en, summary_id).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ap_name, ap_mac, CASE WHEN state = 1 THEN 'online' ELSE 'offline' END AS status,
               num_sta, satisfaction, cpu_pct, model
        FROM (SELECT DISTINCT ON (ai.ap_mac) ai.ap_name, ai.ap_mac, ai.state, ai.num_sta,
                     ai.satisfaction, ai.cpu_pct, ai.model
              FROM ap_inventory ai JOIN sites s ON s.id = ai.site_id
              WHERE s.customer_id = %s AND ai.time > now() - interval '15 minutes'
              ORDER BY ai.ap_mac, ai.time DESC) latest
        ORDER BY (COALESCE(state, 0) = 1), ap_name
        """,
        (customer_id,),
    )
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        # UniFi reports satisfaction -1 when it has no measurement yet --
        # that's "unknown", not "bad": render as a dash, never as a warning.
        if r["satisfaction"] is not None and r["satisfaction"] < 0:
            r["satisfaction"] = None
        # Chip color per the design plan: offline -> terracotta; online but
        # hot CPU or poor satisfaction -> amber; healthy -> blue.
        if r["status"] == "offline":
            r["chip"] = "chip-err"
        elif (r["cpu_pct"] is not None and r["cpu_pct"] >= 70) or \
             (r["satisfaction"] is not None and r["satisfaction"] < 70):
            r["chip"] = "chip-warn"
        else:
            r["chip"] = "chip-ok"
        # "UniFi AC Pro" rather than "U7PG2" -- the controller code means
        # nothing to the customer reading the report. Raw code kept as `code`
        # so the portal can show it alongside; the PDF prints the name only.
        r["code"] = r["model"]
        r["model"] = ap_model_name(r["model"])
    online = sum(1 for r in rows if r["status"] == "online")
    offline = len(rows) - online
    summary_en = f"{len(rows)} access points -- {online} online / {offline} offline"
    summary_id = f"{len(rows)} access point -- {online} aktif / {offline} nonaktif"
    return rows, summary_en, summary_id


def collect_sla(customer_id, start, end):
    """
    Manual SLA + ticket entries (interim source until the ERP API exists --
    see ERP_SLA_API.md) for every month overlapping [start, end), plus a
    period roll-up and a year-to-date roll-up for the report-end year.
    Returns (sla_months, sla_overall, ytd); sla_months is [] when no data.
    Totals are node-weighted averages, matching how the manual SLA report
    presented its Total row.
    """
    period_start = start.astimezone(WIB).date().replace(day=1)
    period_end = end.astimezone(WIB).date()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, month, service_id, service_name, node_count, sla_pct "
        "FROM customer_sla_services "
        "WHERE customer_id = %s AND month >= %s AND month < %s "
        "ORDER BY month, service_id",
        (customer_id, period_start, period_end),
    )
    service_rows = cur.fetchall()
    cur.execute(
        "SELECT ticket_no, tanggal, description, action, mttr_seconds, status "
        "FROM customer_tickets "
        "WHERE customer_id = %s AND tanggal >= %s AND tanggal < %s "
        "ORDER BY tanggal, ticket_no",
        (customer_id, period_start, period_end),
    )
    ticket_rows = cur.fetchall()

    year = end.astimezone(WIB).year
    cur.execute(
        "SELECT COALESCE(sum(sla_pct * node_count) / NULLIF(sum(node_count), 0), NULL) AS sla "
        "FROM customer_sla_services "
        "WHERE customer_id = %s AND month >= %s AND month < %s",
        (customer_id, f"{year}-01-01", f"{year + 1}-01-01"),
    )
    ytd_sla = cur.fetchone()["sla"]
    cur.execute(
        "SELECT count(*) AS n, COALESCE(sum(mttr_seconds), 0) AS dur "
        "FROM customer_tickets "
        "WHERE customer_id = %s AND tanggal >= %s AND tanggal < %s",
        (customer_id, f"{year}-01-01", f"{year + 1}-01-01"),
    )
    ytd_tickets = cur.fetchone()
    conn.close()

    sla_months = []
    for row in service_rows:
        m = row["month"]
        if not sla_months or sla_months[-1]["month"] != m:
            sla_months.append({
                "month": m,
                "label": f"{MONTHS_ID[m.month - 1]} {m.year}",
                "services": [], "tickets": [],
            })
        row["sla_fmt"] = _fmt_pct(row["sla_pct"])
        sla_months[-1]["services"].append(row)
    for block in sla_months:
        svcs = block["services"]
        total_nodes = sum(s["node_count"] for s in svcs)
        weighted = sum(float(s["sla_pct"]) * s["node_count"] for s in svcs)
        block["total_nodes"] = total_nodes
        block["total_sla"] = _fmt_pct(weighted / total_nodes if total_nodes else None)
        for t in ticket_rows:
            if t["tanggal"].replace(day=1) == block["month"]:
                t["tanggal_fmt"] = t["tanggal"].strftime("%d %b %Y")
                t["mttr_fmt"] = _fmt_hms(t["mttr_seconds"]) if t["mttr_seconds"] is not None else "–"
                block["tickets"].append(t)

    if not sla_months:
        return [], None, None

    all_nodes = sum(b["total_nodes"] for b in sla_months)
    all_weighted = sum(
        float(s["sla_pct"]) * s["node_count"] for b in sla_months for s in b["services"]
    )
    overall_sla = all_weighted / all_nodes if all_nodes else None
    sla_overall = {
        "sla": _fmt_pct(overall_sla),
        "downtime": _fmt_pct(100 - overall_sla if overall_sla is not None else None),
        "tickets": len(ticket_rows),
        "duration": _fmt_hms(sum(t["mttr_seconds"] or 0 for t in ticket_rows)),
    }
    ytd = {
        "year": year,
        "sla": _fmt_pct(ytd_sla),
        "downtime": _fmt_pct(100 - float(ytd_sla) if ytd_sla is not None else None),
        "tickets": ytd_tickets["n"],
        "duration": _fmt_hms(ytd_tickets["dur"]),
    }
    return sla_months, sla_overall, ytd


# --- Native timeseries collectors --------------------------------------------
# Each runs the SAME SQL the Grafana panel runs (from customer_overview.json),
# with the Grafana macros swapped for bind params: $__timeFilter(c) -> c BETWEEN
# %(start)s AND %(end)s, $__timeFrom()/$__timeTo() -> %(start)s/%(end)s,
# $customer_id/$router -> real ids. time_bucket_gapfill is a TimescaleDB function
# and runs unchanged. Rows come back oldest-first (ORDER BY time).

def _ts_query(sql, params):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def collect_traffic(customer_id, start, end):
    """Panel 10 -- aggregate uplink download/upload (bits/sec). Stays on RAW (no
    cagg): uplink_metrics is small so this is already ~0.2s, and it's polled ~per
    5-min bucket, so a first()/last()-per-bucket cagg has first==last (zero delta)
    in most buckets and undercounts ~2x. The per-poll LAG (which crosses bucket
    boundaries) is needed and exact -- matches the dashboard."""
    sql = """
        WITH deltas AS (
          SELECT um.router_id, um.uplink_label, um.time,
            um.rx_bytes - LAG(um.rx_bytes) OVER (PARTITION BY um.router_id, um.uplink_label ORDER BY um.time) AS rx_delta,
            um.tx_bytes - LAG(um.tx_bytes) OVER (PARTITION BY um.router_id, um.uplink_label ORDER BY um.time) AS tx_delta,
            EXTRACT(EPOCH FROM (um.time - LAG(um.time) OVER (PARTITION BY um.router_id, um.uplink_label ORDER BY um.time))) AS seconds_elapsed
          FROM uplink_metrics um JOIN routers r ON r.id = um.router_id
          WHERE r.customer_id = %(customer_id)s AND um.time BETWEEN %(start)s AND %(end)s)
        SELECT time_bucket_gapfill('5 minutes', time, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
          sum(CASE WHEN rx_delta IS NULL THEN NULL ELSE GREATEST(rx_delta, 0) * 8 / NULLIF(seconds_elapsed, 0) END) AS download_bps,
          sum(CASE WHEN tx_delta IS NULL THEN NULL ELSE GREATEST(tx_delta, 0) * 8 / NULLIF(seconds_elapsed, 0) END) AS upload_bps
        FROM deltas GROUP BY 1 ORDER BY 1
    """
    rows = _ts_query(sql, {"customer_id": customer_id, "start": start, "end": end})
    return ([r["t"] for r in rows],
            [r["download_bps"] for r in rows],
            [r["upload_bps"] for r in rows])


def collect_path(router_id, start, end):
    """Panel 105 -- per-router latency / jitter / loss per target, from the
    path_metrics_5m rollup, reshaped into '<target> latency/jitter/loss' series."""
    sql = """
        SELECT time_bucket_gapfill('5 minutes', bucket, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               target_name, avg(latency) AS latency, avg(jitter) AS jitter, avg(loss) AS loss
        FROM path_metrics_5m WHERE router_id = %(router_id)s AND bucket BETWEEN %(start)s AND %(end)s
        GROUP BY 1, target_name ORDER BY 1
    """
    rows = _ts_query(sql, {"router_id": router_id, "start": start, "end": end})
    times, seen = [], set()
    for r in rows:
        if r["t"] not in seen:
            seen.add(r["t"]); times.append(r["t"])
    idx = {t: i for i, t in enumerate(times)}
    series = {}
    for r in rows:
        for metric in ("latency", "jitter", "loss"):
            key = f"{r['target_name']} {metric}"
            if key not in series:
                series[key] = [None] * len(times)
            series[key][idx[r["t"]]] = r[metric]
    return times, series


def collect_resource(customer_id, start, end):
    """Panel 9 -- per-router CPU / RAM / Disk %, from the router_metrics_5m
    rollup (RAM%/Disk% derived from the stored component averages). One scan,
    reshaped into '<router> CPU/RAM/Disk %' series."""
    sql = """
        SELECT time_bucket_gapfill('5 minutes', rm.bucket, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               r.identity_name AS router,
               avg(rm.cpu) AS cpu,
               avg(CASE WHEN rm.ram_total > 0 THEN 100.0 * rm.ram_used / rm.ram_total END) AS ram,
               avg(CASE WHEN rm.disk_total > 0 THEN 100.0 * rm.disk_used / rm.disk_total END) AS disk
        FROM router_metrics_5m rm JOIN routers r ON r.id = rm.router_id
        WHERE r.customer_id = %(customer_id)s AND rm.bucket BETWEEN %(start)s AND %(end)s
        GROUP BY 1, r.identity_name ORDER BY 1
    """
    rows = _ts_query(sql, {"customer_id": customer_id, "start": start, "end": end})
    times, seen = [], set()
    for r in rows:
        if r["t"] not in seen:
            seen.add(r["t"]); times.append(r["t"])
    idx = {t: i for i, t in enumerate(times)}
    series = {}
    for r in rows:
        for label, key in (("CPU %", "cpu"), ("RAM %", "ram"), ("Disk %", "disk")):
            name = f"{r['router']} {label}"
            if name not in series:
                series[name] = [None] * len(times)
            series[name][idx[r["t"]]] = r[key]
    return times, series


def collect_clients(customer_id, start, end):
    """Panel 6 -- distinct Wi-Fi clients per 5-min bucket over time. This one
    stays on RAW (no cagg): count(DISTINCT client_mac) across a customer's sites
    can't be pre-aggregated per-site without double-counting MACs seen on more
    than one of the customer's sites -- a per-site-sum cagg matched the median
    bucket but overcounted busy/overlapping buckets up to ~2x, and TimescaleDB
    core has no distinct rollup (no toolkit hyperloglog). It's the one ~3s
    collector for big customers; exact and matches the dashboard."""
    sql = """
        SELECT time_bucket_gapfill('5 minutes', cm.time, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               count(DISTINCT cm.client_mac) AS clients
        FROM client_metrics cm JOIN sites s ON s.id = cm.site_id
        WHERE s.customer_id = %(customer_id)s AND cm.time BETWEEN %(start)s AND %(end)s GROUP BY 1 ORDER BY 1
    """
    rows = _ts_query(sql, {"customer_id": customer_id, "start": start, "end": end})
    return [r["t"] for r in rows], {"Clients": [r["clients"] for r in rows]}


def collect_wifi_quality(customer_id, start, end):
    """Panel 7 -- avg signal (dBm), satisfaction (%), retry (%), from the
    wifi_quality_5m rollup. Averages are weighted across the customer's sites via
    the stored sums+counts (an avg of per-site averages would misweight).
    Satisfaction keeps UniFi's -1 'unknown', matching the current panel."""
    sql = """
        SELECT time_bucket_gapfill('5 minutes', w.bucket, %(start)s::timestamptz, %(end)s::timestamptz) AS t,
               sum(w.sig_sum)::numeric / NULLIF(sum(w.sig_cnt), 0) AS avg_signal,
               sum(w.sat_sum)::numeric / NULLIF(sum(w.sat_cnt), 0) AS avg_satisfaction,
               100.0 * sum(w.retries) / NULLIF(sum(w.attempts), 0) AS retry_pct
        FROM wifi_quality_5m w JOIN sites s ON s.id = w.site_id
        WHERE s.customer_id = %(customer_id)s AND w.bucket BETWEEN %(start)s AND %(end)s GROUP BY 1 ORDER BY 1
    """
    rows = _ts_query(sql, {"customer_id": customer_id, "start": start, "end": end})
    return ([r["t"] for r in rows],
            [r["avg_signal"] for r in rows],
            [r["avg_satisfaction"] for r in rows],
            [r["retry_pct"] for r in rows])


def collect_flow_users(customer_id, from_ms, to_ms, limit=15):
    """Panel 2 -- top internal users by traffic, from ClickHouse user_hourly.
    Same best-effort urllib pattern as collect_flow_providers; returns
    (labels, values, human_labels) for a horizontal bar chart, or ([],[],[])."""
    from_s = int(from_ms // 1000)
    to_s = int(to_ms // 1000)
    q = (
        "SELECT internal_ip, sum(bytes) AS total FROM flow.user_hourly "
        "WHERE exporter_ip IN (SELECT exporter_ip FROM flow.exporter_map "
        f"WHERE customer_id = {int(customer_id)}) "
        f"AND hour >= toDateTime({from_s}) AND hour < toDateTime({to_s}) "
        f"GROUP BY internal_ip ORDER BY total DESC LIMIT {int(limit)} FORMAT TabSeparated"
    )
    try:
        creds = urllib.parse.urlencode({"user": CLICKHOUSE_USER, "password": CLICKHOUSE_PASSWORD})
        req = urllib.request.Request(f"{CLICKHOUSE_URL}/?{creds}", data=q.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
    except Exception:
        return [], [], []
    labels, values, human = [], [], []
    for line in text.splitlines():
        ip, tab, total = line.partition("\t")
        if not tab:
            continue
        try:
            total = int(total)
        except ValueError:
            continue
        labels.append(ip); values.append(total); human.append(_fmt_bytes(total))
    return labels, values, human


def build_pdf(context):
    """Render report_template.html with the assembled context and print it
    to PDF via WeasyPrint. base_url makes relative asset paths resolve."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_APP_DIR),
        autoescape=True,
    )
    html = env.get_template(TEMPLATE_NAME).render(**context)
    return HTML(string=html, base_url=_APP_DIR).write_pdf()


def generate_report(customer_id, days=30, start=None, end=None):
    """
    End-to-end: returns (customer_name, pdf_bytes). Raises on any failure --
    caller decides how to surface it (HTTP error vs. reporter log line).

    Period is either the trailing `days` window (default, what the monthly
    reporter uses) or an explicit [start, end) pair of tz-aware datetimes
    (the customer-detail date picker); explicit dates win.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM customers WHERE id = %s", (customer_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"no customer with id {customer_id}")
    customer_name = row["name"]

    if start is not None and end is not None:
        # End is exclusive; the label shows the last included day.
        last_day = (end - timedelta(seconds=1)).astimezone(WIB)
        period_en = f"{start.astimezone(WIB).strftime('%d %b %Y')} - {last_day.strftime('%d %b %Y')} (WIB)"
        period_id = period_en
    else:
        end = datetime.now(WIB)
        start = end - timedelta(days=days)
        period_en = f"Last {days} days (to {end.strftime('%d %b %Y')})"
        period_id = f"{days} hari terakhir (s.d. {end.strftime('%d %b %Y')})"
    from_ms = int(start.timestamp() * 1000)
    to_ms = int(end.timestamp() * 1000)

    # Routers that actually have path data in the period -- each gets its
    # own latency/jitter/loss graph page (panel 105 is repeat-per-router;
    # d-solo renders it fine with the variable passed explicitly).
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
    ping_routers = cur.fetchall()
    conn.close()

    # Every report panel is now drawn natively (matplotlib, charts.py) from
    # direct SQL / ClickHouse -- no Grafana image-renderer and no dashboard-clone
    # regeneration on the report path. section() wraps a chart data: URI for the
    # template's image slot; order matches the previous Grafana layout.
    def section(uri, en, idn, note_en=None, note_id=None):
        return {"png_uri": uri, "title_en": en, "title_id": idn,
                "note_en": note_en, "note_id": note_id}

    sections = []

    # Internet Traffic -- aggregate uplink download/upload.
    t_times, t_down, t_up = collect_traffic(customer_id, start, end)
    sections.append(section(
        charts.area_updown_chart(t_times, t_down, t_up),
        "Internet Traffic (All Routers Combined)",
        "Trafik Internet (Gabungan Semua Router)"))

    # Traffic composition (flow customers only): Top Content Providers as a
    # native branded table (brand glyphs), Top Internal Users as a bar chart.
    # Both carry the sampling caveat -- see the .note comment in the template
    # for why the byte totals can't be taken as absolute. Shown unconditionally
    # rather than per-router: traffic-flow sampling is the standing default for
    # new routers (it is what bounds flows_raw disk growth), and the section
    # that does report real volume is Internet Traffic, from interface counters.
    if flow_enabled(customer_id):
        prov_rows = collect_flow_providers(customer_id, from_ms, to_ms)
        if prov_rows:
            sections.append({
                "rows": prov_rows,
                "title_en": "Top Content Providers",
                "title_id": "Konten / Layanan Teratas",
                "note_en": ("Indicative figures based on sampled traffic data. "
                            "Provider ranking is representative; absolute "
                            "volumes are lower than actual usage."),
                "note_id": ("Angka indikatif berdasarkan sampel data trafik. "
                            "Peringkat layanan bersifat representatif; volume "
                            "absolut lebih rendah dari pemakaian sebenarnya."),
            })
        u_labels, u_values, u_human = collect_flow_users(customer_id, from_ms, to_ms)
        if u_labels:
            sections.append(section(
                charts.hbar_chart(u_labels, u_values, u_human),
                "Top Internal Users", "Pengguna Internal Teratas",
                note_en=("Indicative figures based on sampled traffic data. "
                         "User ranking is representative; absolute volumes are "
                         "lower than actual usage."),
                note_id=("Angka indikatif berdasarkan sampel data trafik. "
                         "Peringkat pengguna bersifat representatif; volume "
                         "absolut lebih rendah dari pemakaian sebenarnya.")))

    # Path latency / jitter / loss -- one chart per router with path data.
    for r in ping_routers:
        p_times, p_series = collect_path(r["id"], start, end)
        sections.append(section(
            charts.line_chart(p_times, p_series, y_label="ms / %"),
            f"Path Latency, Jitter & Packet Loss — {r['identity_name']}",
            f"Latensi, Jitter & Kehilangan Paket — {r['identity_name']}"))

    # Router resource usage (CPU / RAM / Disk).
    rs_times, rs_series = collect_resource(customer_id, start, end)
    sections.append(section(
        charts.line_chart(rs_times, rs_series, y_label="%", y_suffix="%", y_max=100),
        "Router Resource Usage (CPU / RAM / Disk)",
        "Penggunaan Sumber Daya Router (CPU / RAM / Disk)"))

    # Wi-Fi clients over time.
    c_times, c_series = collect_clients(customer_id, start, end)
    sections.append(section(
        charts.line_chart(c_times, c_series, y_label="Clients"),
        "Wi-Fi Clients Over Time",
        "Jumlah Perangkat Wi-Fi dari Waktu ke Waktu"))

    # Wi-Fi quality trends (signal / satisfaction / retry).
    q_times, q_sig, q_sat, q_ret = collect_wifi_quality(customer_id, start, end)
    sections.append(section(
        charts.dual_axis_chart(q_times, q_sig, q_sat, q_ret),
        "Wi-Fi Quality Trends (Signal / Satisfaction / Retry)",
        "Tren Kualitas Wi-Fi (Sinyal / Kepuasan / Pengulangan)"))

    kpis = collect_kpis(customer_id, start, end)
    ap_rows, ap_summary_en, ap_summary_id = collect_ap_rows(customer_id)
    sla_months, sla_overall, ytd = collect_sla(customer_id, start, end)

    hero_kpis = []
    if sla_overall:
        hero_kpis.append({"value": sla_overall["sla"], "label": "SLA Achievement"})
        hero_kpis.append({"value": sla_overall["tickets"], "label": "Tickets This Period"})
    aps_online = next((v for en2, _, v in kpis if en2.startswith("Access Points Online")), None)
    if aps_online and aps_online != "0 / 0":
        hero_kpis.append({"value": aps_online, "label": "Access Points Online"})
    if not sla_overall:
        hero_kpis.append({"value": next((v for en2, _, v in kpis if en2 == "Average Latency"), "n/a"),
                          "label": "Average Latency"})
        hero_kpis.append({"value": next((v for en2, _, v in kpis if en2 == "Average Packet Loss"), "n/a"),
                          "label": "Average Packet Loss"})

    logo_full = os.path.exists(LOGO_FULL_PATH)
    with open(LOGO_FULL_PATH if logo_full else LOGO_MARK_PATH, "rb") as f:
        logo_uri = "data:image/png;base64," + base64.b64encode(f.read()).decode()

    footer_left = f"{customer_name} · {period_en} · Generated automatically — GMEDIA"
    context = {
        "customer_name": customer_name,
        "period_en": period_en,
        "period_id": period_id,
        "generated_at": datetime.now(WIB).strftime("%d %b %Y %H:%M"),
        "footer_left": footer_left.replace("\\", "").replace('"', "'"),
        "logo_uri": logo_uri,
        "logo_full": logo_full,
        "hero_kpis": hero_kpis,
        "kpis": kpis,
        "sla_months": sla_months,
        "sla_overall": sla_overall,
        "ytd": ytd,
        "panel_sections": sections,
        "ap_rows": ap_rows,
        "ap_summary_en": ap_summary_en,
        "ap_summary_id": ap_summary_id,
    }
    return customer_name, build_pdf(context)
