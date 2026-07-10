"""
report_lib.py -- builds the customer-facing QoE PDF report.

Shared by admin-ui (on-demand "Download PDF report" button) and the
reporter container (monthly email) -- both Dockerfiles COPY this file, the
same shared-lib pattern as routeros/deploy_lib.py, so the two callers
can't drift.

Pipeline per report: regenerate the customer-locked dashboard clone (so
panels always reflect the current dashboard definitions), render a curated
set of its panels to PNG via the grafana-image-renderer service, pull
headline KPI numbers straight from TimescaleDB, and assemble everything
into a bilingual (English/Indonesian) PDF with fpdf2.

Panel images come from the LOCKED clone (uid customer-<slug>), not the
master dashboard -- the clone has customer_id baked into every query and
the NOC-only rows stripped, so a rendering mistake can't leak another
customer's data into a customer-facing document.
"""

import base64
import io
import os
import urllib.request
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras
from fpdf import FPDF

from dashboard_share import share_dashboard_for_customer, slugify

DATABASE_URL = os.environ["DATABASE_URL"]
GRAFANA_INTERNAL_URL = "http://grafana:3000"
GRAFANA_ADMIN_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")

WIB = timezone(timedelta(hours=7))  # Asia/Jakarta

# Curated, non-repeated panels only -- the repeat-per-router panels
# (Config Changes, CPU Cores, etc.) don't render via d-solo, and are
# NOC-facing anyway. (panel_id, height_px, english_caption, indonesian_caption)
REPORT_PANELS = [
    (10, 500, "Internet Traffic (All Routers Combined)", "Trafik Internet (Gabungan Semua Router)"),
    (3, 450, "Latency, Jitter & Packet Loss", "Latensi, Jitter & Kehilangan Paket"),
    (9, 500, "Router Resource Usage (CPU / RAM / Disk)", "Penggunaan Sumber Daya Router (CPU / RAM / Disk)"),
    (6, 500, "Wi-Fi Clients Over Time", "Jumlah Perangkat Wi-Fi dari Waktu ke Waktu"),
    (7, 500, "Wi-Fi Quality Trends (Signal / Satisfaction / Retry)", "Tren Kualitas Wi-Fi (Sinyal / Kepuasan / Pengulangan)"),
    (8, 600, "Access Point Status", "Status Access Point"),
]


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def render_panel(uid, panel_id, days, width=1000, height=500):
    auth = base64.b64encode(f"admin:{GRAFANA_ADMIN_PASSWORD}".encode()).decode()
    url = (
        f"{GRAFANA_INTERNAL_URL}/render/d-solo/{uid}/r"
        f"?panelId={panel_id}&width={width}&height={height}"
        f"&from=now-{days}d&to=now&theme=light&tz=Asia%2FJakarta"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    # First render after a renderer cold start is slow (headless browser
    # spin-up) -- give it a generous timeout rather than failing the report.
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def collect_kpis(customer_id, days):
    """
    Headline numbers for the cover page. Period averages for the path
    metrics (what was the month actually like), current 15-minute values
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
        WHERE r.customer_id = %s AND pm.time > now() - make_interval(days => %s)
          AND NOT (pm.rtt_avg_ms = 0 AND pm.packet_loss_pct = 100)
        """,
        (customer_id, days),
    )
    path = cur.fetchone()

    cur.execute(
        """
        SELECT round(avg(100.0 * tx_retries / NULLIF(wifi_tx_attempts, 0))::numeric, 1) AS avg_retry
        FROM client_metrics cm JOIN sites s ON s.id = cm.site_id
        WHERE s.customer_id = %s AND cm.time > now() - make_interval(days => %s)
          AND cm.is_wired = false
        """,
        (customer_id, days),
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


def build_pdf(customer_name, days, kpis, panel_sections):
    """
    panel_sections: list of (png_bytes, english_caption, indonesian_caption).
    Returns the PDF as bytes.
    """
    now_wib = datetime.now(WIB)
    period_en = f"Last {days} days (to {now_wib.strftime('%d %b %Y')})"
    period_id = f"{days} hari terakhir (s.d. {now_wib.strftime('%d %b %Y')})"

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    # --- Cover page ---
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 22)
    pdf.ln(20)
    pdf.cell(0, 10, "Network Quality Report", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, "Laporan Kualitas Jaringan", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, customer_name, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 7, f"{period_en}  |  {period_id}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Generated / Dibuat: {now_wib.strftime('%d %b %Y %H:%M')} WIB", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(12)

    # KPI table
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, "Summary / Ringkasan", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    for en, idn, value in kpis:
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(120, 8, en, border="B")
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, value, border="B", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 4, idn, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)

    # --- Panel pages ---
    for png, en, idn in panel_sections:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 9, en, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 6, idn, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)
        pdf.image(io.BytesIO(png), x=10, w=190)

    return bytes(pdf.output())


def generate_report(customer_id, days=30):
    """
    End-to-end: returns (customer_name, pdf_bytes). Raises on any failure --
    caller decides how to surface it (HTTP error vs. reporter log line).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM customers WHERE id = %s", (customer_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        raise ValueError(f"no customer with id {customer_id}")
    customer_name = row["name"]

    # Regenerate the locked clone so the report reflects the latest panel
    # definitions (idempotent -- same uid, overwrite=True).
    share_dashboard_for_customer(customer_id, customer_name)
    uid = f"customer-{slugify(customer_name)}"

    sections = []
    for panel_id, height, en, idn in REPORT_PANELS:
        png = render_panel(uid, panel_id, days, width=1000, height=height)
        sections.append((png, en, idn))

    kpis = collect_kpis(customer_id, days)
    return customer_name, build_pdf(customer_name, days, kpis, sections)
