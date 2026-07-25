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

from dashboard_share import share_dashboard_for_customer, slugify

DATABASE_URL = os.environ["DATABASE_URL"]
GRAFANA_INTERNAL_URL = "http://grafana:3000"
GRAFANA_ADMIN_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")

# Traffic-flow analytics live in the sibling flow stack (ClickHouse + the
# flow-overview Grafana dashboard). Panels render only for customers that
# actually have flow collection, and the whole section is best-effort -- if
# ClickHouse is down or a customer has no flows, the report drops it silently.
CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
CLICKHOUSE_USER = os.environ.get("CH_USER", "flow")
CLICKHOUSE_PASSWORD = os.environ.get("CH_PASS", "")
FLOW_DASHBOARD_UID = "flow-overview"
# (panel_id, height_px, english_caption, indonesian_caption) on flow-overview,
# rendered with &var-customer_id=<id>. Panel 3 (the traffic timeseries) is
# omitted -- the report already has the counter-based Internet Traffic panel.
# Panel 1 (Top Content Providers) is NOT here -- it's rendered as a native
# branded table (collect_flow_providers) instead of a Grafana PNG so each row
# can carry its provider's brand glyph.
REPORT_FLOW_PANELS = [
    (2, 380, "Top Internal Users", "Pengguna Internal Teratas"),
]

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
    # Google family -- cache first (on-net YouTube/Google cache -> YouTube).
    if "GOOGLE" in u and "CACHE" in u:
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

# Curated, non-repeated panels. (panel_id, height_px, english_caption,
# indonesian_caption). The ping graphs (panel 105, repeat-per-router) are
# rendered separately per router in generate_report -- d-solo renders a
# repeat panel fine when the variable is passed as &var-router=<id>. The
# AP list is a native PDF table (collect_ap_rows), not a panel image: a
# table image cuts off at the render height (~10 rows), and a hotel site
# can have 300+ APs.
REPORT_PANELS_HEAD = [
    (10, 500, "Internet Traffic (All Routers Combined)", "Trafik Internet (Gabungan Semua Router)"),
]
PING_PANEL_ID = 105
PING_PANEL_HEIGHT = 450
REPORT_PANELS_TAIL = [
    (9, 500, "Router Resource Usage (CPU / RAM / Disk)", "Penggunaan Sumber Daya Router (CPU / RAM / Disk)"),
    (6, 500, "Wi-Fi Clients Over Time", "Jumlah Perangkat Wi-Fi dari Waktu ke Waktu"),
    (7, 500, "Wi-Fi Quality Trends (Signal / Satisfaction / Retry)", "Tren Kualitas Wi-Fi (Sinyal / Kepuasan / Pengulangan)"),
]


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


def render_panel(uid, panel_id, from_ms, to_ms, width=1000, height=500, extra=""):
    auth = base64.b64encode(f"admin:{GRAFANA_ADMIN_PASSWORD}".encode()).decode()
    url = (
        f"{GRAFANA_INTERNAL_URL}/render/d-solo/{uid}/r"
        f"?panelId={panel_id}&width={width}&height={height}"
        f"&from={from_ms}&to={to_ms}&theme=light&tz=Asia%2FJakarta{extra}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    # First render after a renderer cold start is slow (headless browser
    # spin-up) -- give it a generous timeout rather than failing the report.
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


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
        SELECT ap_name, CASE WHEN state = 1 THEN 'online' ELSE 'offline' END AS status,
               num_sta, satisfaction, cpu_pct, model
        FROM (SELECT DISTINCT ON (ai.ap_mac) ai.ap_name, ai.state, ai.num_sta,
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

    # Regenerate the locked clone so the report reflects the latest panel
    # definitions (idempotent -- same uid, overwrite=True).
    share_dashboard_for_customer(customer_id, customer_name)
    uid = f"customer-{slugify(customer_name)}"

    # Panels render at 1400px wide (~200 DPI on an A4 content box).
    def section(png, en, idn):
        return {"png_uri": _png_uri(png), "title_en": en, "title_id": idn}

    sections = []
    for panel_id, height, en, idn in REPORT_PANELS_HEAD:
        png = render_panel(uid, panel_id, from_ms, to_ms, width=1400, height=int(height * 1.4))
        sections.append(section(png, en, idn))
    # Traffic composition (only if this customer has flow collection). Rendered
    # from the shared flow-overview dashboard with customer_id passed explicitly
    # -- same pattern as the per-router ping panel. Best-effort per panel.
    if flow_enabled(customer_id):
        # Top Content Providers: a native branded table (ClickHouse direct),
        # not a Grafana PNG, so each provider row carries its brand glyph. Keep
        # it in panel-1's old position (right after Internet Traffic).
        prov_rows = collect_flow_providers(customer_id, from_ms, to_ms)
        if prov_rows:
            sections.append({
                "rows": prov_rows,
                "title_en": "Top Content Providers",
                "title_id": "Konten / Layanan Teratas",
            })
        for panel_id, height, en, idn in REPORT_FLOW_PANELS:
            try:
                png = render_panel(FLOW_DASHBOARD_UID, panel_id, from_ms, to_ms, width=1400,
                                   height=int(height * 1.4), extra=f"&var-customer_id={customer_id}")
                sections.append(section(png, en, idn))
            except Exception:
                pass
    for r in ping_routers:
        png = render_panel(uid, PING_PANEL_ID, from_ms, to_ms, width=1400,
                           height=int(PING_PANEL_HEIGHT * 1.4), extra=f"&var-router={r['id']}")
        sections.append(section(
            png,
            f"Path Latency, Jitter & Packet Loss — {r['identity_name']}",
            f"Latensi, Jitter & Kehilangan Paket — {r['identity_name']}",
        ))
    for panel_id, height, en, idn in REPORT_PANELS_TAIL:
        png = render_panel(uid, panel_id, from_ms, to_ms, width=1400, height=int(height * 1.4))
        sections.append(section(png, en, idn))

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

    footer_left = f"{customer_name} · {period_en} · Generated automatically — GMedia NOC"
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
