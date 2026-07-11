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

import difflib
import html
import os
import re
import secrets
import threading
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard_share import share_dashboard_for_customer, slugify
from deploy_lib import load_templates, push_to_router
from report_lib import generate_report

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
# Vendored assets (simple-datatables) -- served locally rather than from a
# CDN so the admin UI has no external dependency at page load.
app.mount("/static", StaticFiles(directory="static"), name="static")
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
    sort_col: str = "identity_name",
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
    db_sort_col = allowed_sort_cols.get(sort_col, "r.identity_name")
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
                ORDER BY {db_sort_col} {db_sort_dir}
                LIMIT %s OFFSET %s
            """, (search_query, search_query, search_query, per_page, offset))
        else:
            cur.execute("SELECT COUNT(*) AS count FROM routers")
            total = cur.fetchone()["count"]

            cur.execute(f"""
                SELECT r.*, c.name AS customer_name
                FROM routers r
                LEFT JOIN customers c ON c.id = r.customer_id
                ORDER BY {db_sort_col} {db_sort_dir}
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
    mgmt_host: str = Form(None),
    mgmt_port: int = Form(8728),
    admin_user: str = Form(None),
    admin_password: str = Form(None),
    wan_interface: str = Form("ether1"),
    wan_interface_backup: str = Form(""),
    use_ssl: bool = Form(False),
    priority: str = Form("standard"),
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
    mgmt_host: str = Form(None),
    mgmt_port: int = Form(8728),
    admin_user: str = Form(None),
    admin_password: str = Form(None),
    wan_interface: str = Form("ether1"),
    wan_interface_backup: str = Form(""),
    use_ssl: bool = Form(False),
    priority: str = Form("standard"),
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
            router, INGEST_BASE_URL, METRICS_TEMPLATES, FIRMWARE_TPL, SFTP_CONFIG, SYSLOG_CONFIG,
            baseline_templates=BASELINE_TEMPLATES, radius_config=RADIUS_CONFIG, gmedia_cidrs=GMEDIA_CIDRS,
        )
        renamed_to = sync_identity_name(router["id"], router["identity_name"], actual_identity)
        detail = "deployed" if not renamed_to else f"deployed (identity_name synced: {router['identity_name']!r} -> {renamed_to!r})"
        if warnings:
            detail += " -- " + " | ".join(f"WARNING: {w}" for w in warnings)
        result = {"identity_name": renamed_to or router["identity_name"], "ok": True, "detail": detail}
    except Exception as e:
        result = {"identity_name": router["identity_name"], "ok": False, "detail": str(e)}

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

    # Compile the firmware script
    firmware_src = FIRMWARE_TPL.replace(
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
            "firmware_src": firmware_src,
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
    """
    conn = get_conn()
    cur = conn.cursor()
    if priority:
        cur.execute("SELECT id FROM routers WHERE priority = %s", (priority,))
    else:
        cur.execute("SELECT id FROM routers")
    router_ids = [row["id"] for row in cur.fetchall()]
    conn.close()

    threading.Thread(target=deploy_all_bg, args=(router_ids,), daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.get("/customers")
def list_customers(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
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
        ORDER BY c.name
    """)
    customers = cur.fetchall()
    conn.close()
    return templates.TemplateResponse("customers_list.html", {"request": request, "customers": customers})


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
    
    conn.close()
    
    return templates.TemplateResponse(
        "customer_detail.html", 
        {
            "request": request, 
            "customer": customer, 
            "routers": routers, 
            "sites": sites,
            "unassigned_sites": unassigned_sites
        }
    )


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
    conn.close()

    try:
        url = share_dashboard_for_customer(customer_id, customer["name"])
        result = {"ok": True, "detail": url}
    except Exception as e:
        result = {"ok": False, "detail": str(e)}

    return templates.TemplateResponse(
        "share_dashboard_result.html", {"request": request, "customer": customer, "result": result}
    )


@app.get("/customers/{customer_id}/report")
def download_report(customer_id: int, days: int = 30):
    """
    Generates the customer's QoE PDF on demand. Synchronous by design --
    ~6 panel renders at 1-3s each is an acceptable wait for a click, and
    the browser shows its own loading state.
    """
    customer_name, pdf_bytes = generate_report(customer_id, days=days)
    filename = f"qoe-report-{slugify(customer_name)}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
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
        def normalise_ros_booleans(text):
            """
            RouterOS 6.x exports booleans as 'true'/'false'; 7.x (and some 6
            builds after a firmware bump) uses 'yes'/'no'. Normalise everything
            to yes/no before diffing so a RouterOS upgrade doesn't produce a
            wall of spurious ±disabled=false/±disabled=no noise that buries
            the real config changes.

            Only touches property-value pairs (word=true / word=false) to
            avoid accidentally rewriting content inside string values.
            """
            import re as _re
            text = _re.sub(r'\b(\w+=)true\b',  r'\1yes',  text)
            text = _re.sub(r'\b(\w+=)false\b', r'\1no',   text)
            return text

        prev_lines = normalise_ros_booleans(previous["config_text"]).splitlines()
        curr_lines = normalise_ros_booleans(current["config_text"]).splitlines()

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
