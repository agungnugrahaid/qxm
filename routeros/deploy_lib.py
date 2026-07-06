"""
deploy_lib.py — shared logic for pushing the qoe-push scripts to a MikroTik
router over the RouterOS API. Used by both bulk_deploy.py (CSV-driven bulk
import) and the admin-ui web app (one-router-at-a-time, via a form/button).

Keeping this in one place means both tools stay in sync — no risk of the
CLI and the web UI drifting apart on how a script gets pushed.
"""

import os
import ssl

from librouteros import connect

# RouterOS api-ssl services are set up with a self-signed cert per router
# (see routeros/README.md) -- there's no shared CA to verify against, so
# we trust whatever cert the router presents. Do not reuse this relaxed
# SSL context for anything internet-facing.
_SSL_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_V7_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-push-metrics-v7.rsc")
METRICS_V6_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-push-metrics-v6.rsc")
FIRMWARE_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-push-firmware.rsc")


def load_templates():
    """
    Returns (metrics_templates, firmware_tpl) where metrics_templates is
    {"v7": ..., "v6": ...} -- push_to_router picks between them based on
    the target router's actual RouterOS major version (see
    routeros/README.md for why: `/ping ... as-value` isn't parseable at
    all on at least one RouterOS 6.49.8 long-term build).
    """
    with open(METRICS_V7_TEMPLATE_PATH) as f:
        metrics_v7 = f.read()
    with open(METRICS_V6_TEMPLATE_PATH) as f:
        metrics_v6 = f.read()
    with open(FIRMWARE_TEMPLATE_PATH) as f:
        firmware_tpl = f.read()
    return {"v7": metrics_v7, "v6": metrics_v6}, firmware_tpl


def detect_major_version(api):
    res = list(api(cmd="/system/resource/print"))
    version_str = res[0].get("version", "")
    try:
        return int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return 7  # unknown/unparseable -- assume current-gen syntax


def render_script(template, replacements):
    """
    replacements: dict of {exact substring in template: replacement string}.
    Kept generic (rather than fixed positional args) since the metrics
    script needs a WAN-interface substitution the firmware script doesn't.
    """
    out = template
    for placeholder, value in replacements.items():
        out = out.replace(placeholder, value)
    return out


def upsert_script(api, name, source):
    existing = list(api(cmd="/system/script/print"))
    match = next((s for s in existing if s.get("name") == name), None)
    if match:
        list(api(cmd="/system/script/set", **{".id": match[".id"], "source": source}))
    else:
        list(api(cmd="/system/script/add", name=name, source=source, policy="read,write,test"))


def upsert_scheduler(api, name, on_event, interval):
    existing = list(api(cmd="/system/scheduler/print"))
    match = next((s for s in existing if s.get("name") == name), None)
    if match:
        list(api(cmd="/system/scheduler/set", **{".id": match[".id"], "interval": interval, "on-event": on_event}))
    else:
        list(api(cmd="/system/scheduler/add", name=name, interval=interval, **{"on-event": on_event}))


def run_script(api, name):
    """
    Fire the script once immediately after deploying, so data shows up
    right away instead of waiting for the next scheduled cycle.

    Caveat: `/ping ... as-value` (used for path_metrics) only returns
    real data when RouterOS's own scheduler fires the script -- a run
    triggered this way (like any API-triggered run) gets an empty
    result, reported as 100% loss / 0ms. Confirmed on 7.20.4; everything
    else (uptime, CPU/RAM/disk, uplinks, DHCP) is accurate immediately.
    The next real scheduled run corrects the ping numbers 5 minutes later.
    """
    existing = list(api(cmd="/system/script/print"))
    match = next((s for s in existing if s.get("name") == name), None)
    if match:
        list(api(cmd="/system/script/run", **{".id": match[".id"]}))


def push_to_router(router, ingest_base_url, metrics_templates, firmware_tpl, sftp_config):
    """
    router: dict with identity_name, mgmt_host, mgmt_port, admin_user,
    admin_password, auth_token, wan_interface. Raises on any failure —
    caller decides how to report it (CLI print vs. web UI flash message).

    metrics_templates: {"v7": ..., "v6": ...} from load_templates() --
    the actual template used is chosen after connecting, based on the
    router's real RouterOS major version (not assumed from config).

    sftp_config: {"host": ..., "port": ..., "user": ..., "password": ...}
    for the daily config-snapshot upload -- see config-snapshot-watcher/
    and routeros/README.md for why this goes over SFTP, not HTTP.
    """
    token = router["auth_token"]
    wan_interface = router.get("wan_interface") or "ether1"
    wan_interface_backup = router.get("wan_interface_backup") or ""

    firmware_src = render_script(firmware_tpl, {
        '"https://monitor.yourisp.com/ingest/firmware"': f'"{ingest_base_url}/ingest/firmware"',
        '"PER_ROUTER_AUTH_TOKEN"': f'"{token}"',
        '"SFTP_HOST_PLACEHOLDER"': f'"{sftp_config["host"]}"',
        '"SFTP_PORT_PLACEHOLDER"': f'"{sftp_config["port"]}"',
        '"SFTP_USER_PLACEHOLDER"': f'"{sftp_config["user"]}"',
        '"SFTP_PASSWORD_PLACEHOLDER"': f'"{sftp_config["password"]}"',
    })

    connect_kwargs = {}
    if router.get("use_ssl"):
        connect_kwargs["ssl_wrapper"] = _SSL_CTX.wrap_socket

    api = connect(
        host=router["mgmt_host"],
        username=router["admin_user"],
        password=router["admin_password"],
        port=int(router.get("mgmt_port") or 8728),
        **connect_kwargs,
    )
    try:
        major_version = detect_major_version(api)
        metrics_tpl = metrics_templates["v7"] if major_version >= 7 else metrics_templates["v6"]
        metrics_src = render_script(metrics_tpl, {
            '"https://monitor.yourisp.com/ingest"': f'"{ingest_base_url}/ingest"',
            '"PER_ROUTER_AUTH_TOKEN"': f'"{token}"',
            '"WAN_INTERFACE_PLACEHOLDER"': f'"{wan_interface}"',
            '"WAN_INTERFACE_BACKUP_PLACEHOLDER"': f'"{wan_interface_backup}"',
        })

        upsert_script(api, "qoe-push-metrics", metrics_src)
        upsert_script(api, "qoe-push-firmware", firmware_src)
        upsert_scheduler(api, "qoe-push-metrics", "qoe-push-metrics", "00:05:00")
        upsert_scheduler(api, "qoe-push-firmware", "qoe-push-firmware", "1d")
        # Best-effort -- the scheduled cycle will still deliver data on its
        # own even if this immediate kick fails (e.g. a slow/flaky router
        # timing out on the run command), so don't let that failure make
        # an otherwise-successful deploy look like it failed.
        try:
            run_script(api, "qoe-push-metrics")
            run_script(api, "qoe-push-firmware")
        except Exception:
            pass
    finally:
        api.close()
