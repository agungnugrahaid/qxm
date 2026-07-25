"""
dashboard_share.py -- generates a customer-locked clone of the master
Customer Overview dashboard, scoped to one customer, for sharing inside
the NOC's own team (opened via the existing Grafana login -- no public,
unauthenticated link; that was considered and deliberately dropped).

The master dashboard scopes every panel through a $customer_id template
variable -- fine for NOC use since everyone already has full access, but
this clone bakes the customer_id in as a literal integer in every query
instead, so there's no variable left to accidentally switch away from
while looking at one customer's data.

It also strips the NOC-only rows (raw router logs, admin session/login
audit trail, config snapshot downloads) so the shared view stays focused
on that customer's QoE picture.

The generated dashboard is pushed straight to Grafana via /api/dashboards/db
(not written into the provisioned-from-file folder) so it's usable
immediately -- file-based provisioning only re-scans every 30s, which
would make the very first "Share" click fail.
"""
import base64
import json
import os
import re
import urllib.request

SOURCE_DASHBOARD_PATH = "/grafana-dashboards/customer_overview.json"
GRAFANA_INTERNAL_URL = "http://grafana:3000"
GRAFANA_ADMIN_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")
# Base URL used only to build the link shown back to the NOC user -- the
# dashboard itself still requires the normal Grafana login to open.
GRAFANA_PUBLIC_URL = os.environ.get("GRAFANA_PUBLIC_URL", "https://grafana.yourisp.com")

# Rows that are internal-NOC-only -- never shown to an external customer.
EXCLUDED_ROWS = {"Logs & Diagnostics", "Inventory / Config"}

# Rows that only make sense when the customer actually has that data configured,
# mapped to the flag (in `flags`) that gates them. A router-only customer gets no
# empty "Wireless / UniFi" section; a wireless-only customer gets no empty router
# sections; a customer without flow collection gets no "Traffic Flow" section.
# Titles absent from the dashboard JSON are simply no-ops.
ROW_REQUIREMENTS = {
    "Network Quality": "has_routers",
    "Router Resource Health": "has_routers",
    "Wireless / UniFi": "has_wireless",
    "Traffic Flow": "include_flow",
}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _row_ranges(panels):
    rows = sorted((p for p in panels if p["type"] == "row"), key=lambda p: p["gridPos"]["y"])
    ranges = []
    for i, row in enumerate(rows):
        start = row["gridPos"]["y"]
        end = rows[i + 1]["gridPos"]["y"] if i + 1 < len(rows) else float("inf")
        ranges.append((row["title"], start, end))
    return ranges


def generate_dashboard_json(customer_id, customer_name, flags=None):
    """flags: {"has_routers":bool, "has_wireless":bool, "include_flow":bool} --
    sections whose requirement (ROW_REQUIREMENTS) is unmet are stripped from the
    clone so the customer never sees an empty panel for data they don't have.
    Missing/None flags default to True (show the section)."""
    flags = flags or {}
    with open(SOURCE_DASHBOARD_PATH) as f:
        d = json.load(f)

    # Bake $customer_id into every query as a literal -- see module
    # docstring for why this matters.
    raw = json.dumps(d).replace("$customer_id", str(customer_id))
    d = json.loads(raw)

    # NOC-only rows are always excluded; requirement-gated rows are excluded only
    # when their flag is explicitly False. Both feed the same y-range + shift
    # machinery below, which handles expanded rows and the collapsed Traffic Flow
    # row alike (excluding a collapsed row's title drops it and its nested panels).
    excluded_titles = set(EXCLUDED_ROWS)
    for title, req in ROW_REQUIREMENTS.items():
        if flags.get(req, True) is False:
            excluded_titles.add(title)

    ranges = _row_ranges(d["panels"])
    excluded_ranges = [(s, e) for title, s, e in ranges if title in excluded_titles]

    def is_excluded(p):
        y = p["gridPos"]["y"]
        return any(s <= y < e for s, e in excluded_ranges)

    kept = [p for p in d["panels"] if not is_excluded(p)]
    for p in kept:
        shift = sum(e - s for s, e in excluded_ranges if e != float("inf") and s < p["gridPos"]["y"])
        p["gridPos"]["y"] -= shift
    d["panels"] = kept

    # customer_id variable is gone (baked in above); router variable stays
    # (still useful for the customer to pick between their own routers)
    # but its own query needs the same baking so it doesn't dangle a
    # reference to a variable that no longer exists.
    d["templating"]["list"] = [v for v in d["templating"]["list"] if v["name"] != "customer_id"]
    for v in d["templating"]["list"]:
        if v["name"] == "router":
            v["query"] = v["query"].replace("$customer_id", str(customer_id))

    slug = slugify(customer_name)
    d["uid"] = f"customer-{slug}"
    d["id"] = None
    d["title"] = f"{customer_name} — QoE Dashboard"
    return d, slug


def _grafana_api(method, path, body=None):
    auth = base64.b64encode(f"admin:{GRAFANA_ADMIN_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{GRAFANA_INTERNAL_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def share_dashboard_for_customer(customer_id, customer_name, flags=None):
    d, slug = generate_dashboard_json(customer_id, customer_name, flags)
    uid = d["uid"]

    result = _grafana_api(
        "POST", "/api/dashboards/db", {"dashboard": d, "overwrite": True, "message": "share-dashboard"}
    )
    # Grafana returns the dashboard's own relative url (e.g.
    # "/d/customer-melia-purosani/melia-purosani-qoe-dashboard").
    return f"{GRAFANA_PUBLIC_URL}{result['url']}"
