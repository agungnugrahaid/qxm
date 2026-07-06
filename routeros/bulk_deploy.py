"""
bulk_deploy.py — one-time (or occasional) CSV-driven bulk import: pushes the
qoe-push scripts to every router listed in router_inventory.csv, over your
existing management channel. Good for onboarding a batch of routers from a
spreadsheet you already have.

For adding or redeploying to routers one at a time going forward, use the
admin-ui web app instead — same underlying logic (deploy_lib.py), just a
form instead of a CSV row.

For each router in the CSV, this:
  1. Generates a per-router auth token (or reuses the one already on file).
  2. Upserts the matching row in the `routers` Postgres table, including
     its management details.
  3. Pushes/updates both qoe-push scripts and their scheduler entries via
     deploy_lib.push_to_router().

Safe to re-run: existing scripts/schedulers are updated in place, and
existing tokens are preserved.

Requires: pip install librouteros psycopg2-binary
"""

import csv
import os
import secrets
import sys

import psycopg2

from deploy_lib import load_templates, push_to_router

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://qoe:changeme@localhost:5432/qoe")
INGEST_BASE_URL = os.environ.get("INGEST_BASE_URL", "https://monitor.yourisp.com")
INVENTORY_CSV = os.environ.get("INVENTORY_CSV", "router_inventory.csv")
SFTP_CONFIG = {
    "host": os.environ.get("SFTP_HOST", "monitor.yourisp.com"),
    "port": os.environ.get("SFTP_PORT", "2222"),
    "user": os.environ.get("SFTP_USER", "configupload"),
    "password": os.environ.get("SFTP_PASSWORD", "changeme"),
}


def ensure_router_row(pg_conn, row, new_token):
    identity_name = row["identity_name"]
    mgmt_host = row["mgmt_host"]
    mgmt_port = int(row.get("mgmt_port") or 8728)
    admin_user = row["admin_user"]
    admin_password = row["admin_password"]
    customer_id = row.get("customer_id") or None
    wan_interface = row.get("wan_interface") or "ether1"
    wan_interface_backup = row.get("wan_interface_backup") or None
    use_ssl = (row.get("use_ssl") or "").strip().lower() in ("1", "true", "yes")

    cur = pg_conn.cursor()
    cur.execute("SELECT id, auth_token FROM routers WHERE identity_name = %s", (identity_name,))
    existing = cur.fetchone()

    if existing:
        router_id, existing_token = existing
        token = existing_token or new_token
        cur.execute(
            "UPDATE routers SET mgmt_host = %s, mgmt_port = %s, admin_user = %s, "
            "admin_password = %s, auth_token = %s, wan_interface = %s, wan_interface_backup = %s, use_ssl = %s WHERE id = %s",
            (mgmt_host, mgmt_port, admin_user, admin_password, token, wan_interface, wan_interface_backup, use_ssl, router_id),
        )
    else:
        token = new_token
        cur.execute(
            "INSERT INTO routers (customer_id, identity_name, auth_token, mgmt_host, mgmt_port, admin_user, admin_password, wan_interface, wan_interface_backup, use_ssl) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (customer_id, identity_name, token, mgmt_host, mgmt_port, admin_user, admin_password, wan_interface, wan_interface_backup, use_ssl),
        )
        router_id = cur.fetchone()[0]

    pg_conn.commit()
    cur.close()
    return {
        "id": router_id,
        "identity_name": identity_name,
        "mgmt_host": mgmt_host,
        "mgmt_port": mgmt_port,
        "admin_user": admin_user,
        "admin_password": admin_password,
        "auth_token": token,
        "wan_interface": wan_interface,
        "wan_interface_backup": wan_interface_backup,
        "use_ssl": use_ssl,
    }


def main():
    metrics_templates, firmware_tpl = load_templates()
    pg_conn = psycopg2.connect(DATABASE_URL)

    ok, failed = 0, 0
    with open(INVENTORY_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            identity_name = row.get("identity_name")
            try:
                router = ensure_router_row(pg_conn, row, secrets.token_hex(24))
                push_to_router(router, INGEST_BASE_URL, metrics_templates, firmware_tpl, SFTP_CONFIG)
                print(f"[{identity_name}] deployed OK (router_id={router['id']})")
                ok += 1
            except Exception as e:
                print(f"[{identity_name}] FAILED — {e}", file=sys.stderr)
                failed += 1

    pg_conn.close()
    print(f"\nDone: {ok} succeeded, {failed} failed.")


if __name__ == "__main__":
    main()
