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
  POST /deploy-all          push to every router, show a per-router result summary
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

import difflib
import html
import os
import re
import secrets
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard_share import share_dashboard_for_customer
from deploy_lib import load_templates, push_to_router

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
# Same CIDR set as SFTP's own allowlist -- see
# routeros/qoe-baseline-hardening-v7.rsc's header comment for why this is
# reused as the router-side management-access allowlist too, rather than
# tracked as a second separate list.
GMEDIA_CIDRS = [c.strip() for c in os.environ.get("SFTP_ALLOWED_CIDRS", "").split(",") if c.strip()]

app = FastAPI(title="QoE Fleet Admin")
templates = Jinja2Templates(directory="templates")

METRICS_TEMPLATES, FIRMWARE_TPL, BASELINE_TEMPLATES = load_templates()


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def is_online(last_seen_at):
    if not last_seen_at:
        return False
    age = datetime.now(timezone.utc) - last_seen_at
    return age.total_seconds() < 15 * 60  # no push in 15 min = flagged offline


@app.get("/")
def list_routers(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.*, c.name AS customer_name
        FROM routers r
        LEFT JOIN customers c ON c.id = r.customer_id
        ORDER BY r.identity_name
    """)
    routers = cur.fetchall()
    conn.close()

    for r in routers:
        r["online"] = is_online(r["last_seen_at"])

    return templates.TemplateResponse("routers_list.html", {"request": request, "routers": routers})


@app.get("/routers/new")
def new_router_form(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "router_form.html", {"request": request, "router": None, "customers": customers}
    )


@app.post("/routers/new")
def create_router(
    identity_name: str = Form(...),
    customer_id: int = Form(...),
    mgmt_host: str = Form(...),
    mgmt_port: int = Form(8728),
    admin_user: str = Form(...),
    admin_password: str = Form(...),
    wan_interface: str = Form("ether1"),
    wan_interface_backup: str = Form(""),
    use_ssl: bool = Form(False),
):
    token = secrets.token_hex(24)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO routers (customer_id, identity_name, auth_token, mgmt_host, mgmt_port, admin_user, admin_password, wan_interface, wan_interface_backup, use_ssl) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (customer_id, identity_name, token, mgmt_host, mgmt_port, admin_user, admin_password, wan_interface, wan_interface_backup or None, use_ssl),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/", status_code=303)


@app.get("/routers/{router_id}/edit")
def edit_router_form(request: Request, router_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers WHERE id = %s", (router_id,))
    router = cur.fetchone()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "router_form.html", {"request": request, "router": router, "customers": customers}
    )


@app.post("/routers/{router_id}/edit")
def update_router(
    router_id: int,
    identity_name: str = Form(...),
    customer_id: int = Form(...),
    mgmt_host: str = Form(...),
    mgmt_port: int = Form(8728),
    admin_user: str = Form(...),
    admin_password: str = Form(...),
    wan_interface: str = Form("ether1"),
    wan_interface_backup: str = Form(""),
    use_ssl: bool = Form(False),
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE routers SET identity_name=%s, customer_id=%s, mgmt_host=%s, mgmt_port=%s, "
        "admin_user=%s, admin_password=%s, wan_interface=%s, wan_interface_backup=%s, use_ssl=%s WHERE id=%s",
        (identity_name, customer_id, mgmt_host, mgmt_port, admin_user, admin_password, wan_interface, wan_interface_backup or None, use_ssl, router_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/", status_code=303)


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


@app.post("/routers/{router_id}/deploy")
def deploy_router(request: Request, router_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers WHERE id = %s", (router_id,))
    router = cur.fetchone()
    conn.close()

    results = []
    try:
        actual_identity = push_to_router(
            router, INGEST_BASE_URL, METRICS_TEMPLATES, FIRMWARE_TPL, SFTP_CONFIG, SYSLOG_CONFIG,
            baseline_templates=BASELINE_TEMPLATES, radius_config=RADIUS_CONFIG, gmedia_cidrs=GMEDIA_CIDRS,
        )
        renamed_to = sync_identity_name(router_id, router["identity_name"], actual_identity)
        detail = "deployed" if not renamed_to else f"deployed (identity_name synced: {router['identity_name']!r} -> {renamed_to!r})"
        results.append({"identity_name": renamed_to or router["identity_name"], "ok": True, "detail": detail})
    except Exception as e:
        results.append({"identity_name": router["identity_name"], "ok": False, "detail": str(e)})

    return templates.TemplateResponse("deploy_result.html", {"request": request, "results": results})


@app.post("/deploy-all")
def deploy_all(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM routers ORDER BY identity_name")
    routers = cur.fetchall()
    conn.close()

    results = []
    for router in routers:
        try:
            actual_identity = push_to_router(
                router, INGEST_BASE_URL, METRICS_TEMPLATES, FIRMWARE_TPL, SFTP_CONFIG, SYSLOG_CONFIG,
                baseline_templates=BASELINE_TEMPLATES, radius_config=RADIUS_CONFIG, gmedia_cidrs=GMEDIA_CIDRS,
            )
            renamed_to = sync_identity_name(router["id"], router["identity_name"], actual_identity)
            detail = "deployed" if not renamed_to else f"deployed (identity_name synced: {router['identity_name']!r} -> {renamed_to!r})"
            results.append({"identity_name": renamed_to or router["identity_name"], "ok": True, "detail": detail})
        except Exception as e:
            results.append({"identity_name": router["identity_name"], "ok": False, "detail": str(e)})

    return templates.TemplateResponse("deploy_result.html", {"request": request, "results": results})


@app.get("/customers")
def list_customers(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()
    return templates.TemplateResponse("customers_list.html", {"request": request, "customers": customers})


@app.post("/customers/new")
def create_customer(name: str = Form(...), address: str = Form("")):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO customers (name, address) VALUES (%s, %s)", (name, address))
    conn.commit()
    conn.close()
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/{customer_id}/share-dashboard")
def share_dashboard(request: Request, customer_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
    customer = cur.fetchone()
    conn.close()

    try:
        url = share_dashboard_for_customer(customer_id, customer["name"])
        result = {"ok": True, "detail": url}
    except Exception as e:
        result = {"ok": False, "detail": str(e)}

    return templates.TemplateResponse(
        "share_dashboard_result.html", {"request": request, "customer": customer, "result": result}
    )


@app.get("/sites")
def list_sites(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.*, ctl.name AS controller_name, c.name AS customer_name
        FROM sites s
        LEFT JOIN controllers ctl ON ctl.id = s.controller_id
        LEFT JOIN customers c ON c.id = s.customer_id
        ORDER BY ctl.name, s.unifi_site_name
    """)
    sites = cur.fetchall()
    cur.execute("SELECT * FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "sites_list.html", {"request": request, "sites": sites, "customers": customers}
    )


@app.post("/sites/{site_id}/assign")
def assign_site(site_id: int, customer_id: int = Form(...)):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE sites SET customer_id = %s WHERE id = %s", (customer_id, site_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/sites", status_code=303)


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
        "SELECT config_text FROM router_config_snapshots WHERE router_id = %s AND time = %s",
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
        {"request": request, "router_id": router_id, "timestamp": timestamp, "config_html": config_html},
    )


@app.get("/config-snapshots/{router_id}/{timestamp}/diff")
def diff_config_snapshot(request: Request, router_id: int, timestamp: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT config_text, time FROM router_config_snapshots WHERE router_id = %s AND time = %s",
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
        diff_lines = difflib.unified_diff(
            previous["config_text"].splitlines(),
            current["config_text"].splitlines(),
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
            "diff_html": diff_html,
            "older_timestamp": previous["time"].isoformat() if previous else None,
            "newer_timestamp": newer["time"].isoformat() if newer else None,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
