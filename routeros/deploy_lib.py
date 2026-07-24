"""
deploy_lib.py — shared logic for pushing the qoe-push scripts to a MikroTik
router over the RouterOS API. Used by both bulk_deploy.py (CSV-driven bulk
import) and the admin-ui web app (one-router-at-a-time, via a form/button).

Keeping this in one place means both tools stay in sync — no risk of the
CLI and the web UI drifting apart on how a script gets pushed.
"""

import os
import socket
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
FIRMWARE_V7_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-push-firmware-v7.rsc")
FIRMWARE_V6_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-push-firmware-v6.rsc")
BASELINE_V7_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-baseline-hardening-v7.rsc")
BASELINE_V6_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "qoe-baseline-hardening-v6.rsc")


def load_templates():
    """
    Returns (metrics_templates, firmware_templates, baseline_templates),
    each {"v7": ..., "v6": ...} -- push_to_router picks between them based
    on the target router's actual RouterOS major version (see
    routeros/README.md for why: `/ping ... as-value` isn't parseable at
    all on at least one RouterOS 6.49.8 long-term build -- confirmed
    separately that the baseline script's v7-only NTP syntax and the
    firmware script's `:export show-sensitive` hit the exact same class
    of hard parse failure on v6, hence every script now splits).
    """
    with open(METRICS_V7_TEMPLATE_PATH) as f:
        metrics_v7 = f.read()
    with open(METRICS_V6_TEMPLATE_PATH) as f:
        metrics_v6 = f.read()
    with open(FIRMWARE_V7_TEMPLATE_PATH) as f:
        firmware_v7 = f.read()
    with open(FIRMWARE_V6_TEMPLATE_PATH) as f:
        firmware_v6 = f.read()
    with open(BASELINE_V7_TEMPLATE_PATH) as f:
        baseline_v7 = f.read()
    with open(BASELINE_V6_TEMPLATE_PATH) as f:
        baseline_v6 = f.read()
    return (
        {"v7": metrics_v7, "v6": metrics_v6},
        {"v7": firmware_v7, "v6": firmware_v6},
        {"v7": baseline_v7, "v6": baseline_v6},
    )


def build_ros_array_literal(values):
    """
    Renders a Python list of strings as a RouterOS array literal, e.g.
    ["a", "b"] -> '{"a";"b"}' -- semicolon-separated, not comma (RouterOS
    uses comma inside a single value like a dst-port list, so a real
    array needs the other separator to stay unambiguous).
    """
    return "{" + ";".join(f'"{v}"' for v in values) + "}"


def detect_major_version(api):
    res = list(api(cmd="/system/resource/print"))
    version_str = res[0].get("version", "")
    try:
        return int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return 7  # unknown/unparseable -- assume current-gen syntax


def get_identity(api):
    return list(api(cmd="/system/identity/print"))[0]["name"]


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


SCRIPT_POLICY = "ftp,policy,read,write,reboot,test,sensitive"


def upsert_script(api, name, source):
    existing = list(api(cmd="/system/script/print"))
    match = next((s for s in existing if s.get("name") == name), None)
    if match:
        list(api(cmd="/system/script/set", **{".id": match[".id"], "source": source, "policy": SCRIPT_POLICY}))
    else:
        list(api(cmd="/system/script/add", name=name, source=source, policy=SCRIPT_POLICY))


def upsert_scheduler(api, name, on_event, interval):
    existing = list(api(cmd="/system/scheduler/print"))
    match = next((s for s in existing if s.get("name") == name), None)
    if match:
        list(api(cmd="/system/scheduler/set", **{".id": match[".id"], "interval": interval, "on-event": on_event}))
    else:
        list(api(cmd="/system/scheduler/add", name=name, interval=interval, **{"on-event": on_event}))


def _resolve_ip(host):
    """
    RouterOS 6.49.8's logging-action `remote` field is strictly IP-typed
    (rejects hostnames outright: "invalid value for argument ip"); 7.20.4
    accepts either. Resolving here lets SYSLOG_HOST stay a human-readable
    hostname in .env while working on both versions, instead of forcing
    the .env value itself to be a hardcoded IP.
    """
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        return socket.gethostbyname(host)


def upsert_syslog_forwarding(api, remote_host, remote_port):
    """
    Points RouterOS's remote syslog action at our syslog-forwarder service
    (see docker-compose.yml / syslog-forwarder/) and makes sure it's wired
    to fire.

    Critical: one rule per topic, not one rule with a comma-separated
    topics list. RouterOS ANDs multiple topics within a single rule rather
    than ORing them, so a rule like topics="info,warning,error,critical"
    would require a single log entry to match all four simultaneously --
    which never happens -- and silently never fires. Confirmed the hard
    way across two different routers/RouterOS versions before finding
    this. Any stray multi-topic rule left over from that is cleaned up
    below so re-running deploy on an already-fixed router doesn't leave
    dead config behind.

    Some RouterOS builds' `remote` logging-action schema has no
    `remote-log-format`/`remote-protocol` property at all (unlike others'
    default/syslog/cef enum and explicit udp/tcp choice) -- they only
    ever send UDP, and use a separate boolean `bsd-syslog` field for the
    format instead. Passing either fails outright with "unknown
    parameter" on those builds. This is NOT a clean function of RouterOS
    major version -- confirmed in practice: 7.20.4 has the new-style
    fields, but 7.16.1 doesn't and uses the old (v6-style) schema, so
    branching on major_version alone (an earlier version of this
    function did exactly that) breaks on some v7 routers. Detect by
    inspecting the router's own built-in default 'remote' action instead
    of trusting the version number. Both schemas settle on the same wire
    format our syslog-forwarder expects (BSD/RFC3164-style `<PRI>...`
    framing).
    """
    actions = list(api(cmd="/system/logging/action/print"))
    match = next((a for a in actions if a.get("name") == "remoteloki"), None)
    default_remote = next((a for a in actions if a.get("name") == "remote"), None)
    supports_new_style = bool(default_remote) and "remote-protocol" in default_remote

    action_kwargs = {
        "target": "remote",
        "remote": _resolve_ip(remote_host),
        "remote-port": str(remote_port),
    }
    if supports_new_style:
        action_kwargs["remote-protocol"] = "udp"
        action_kwargs["remote-log-format"] = "syslog"
    else:
        action_kwargs["bsd-syslog"] = True
    if match:
        list(api(cmd="/system/logging/action/set", **{".id": match[".id"], **action_kwargs}))
    else:
        list(api(cmd="/system/logging/action/add", name="remoteloki", **action_kwargs))

    rules = list(api(cmd="/system/logging/print"))
    remoteloki_rules = [r for r in rules if r.get("action") == "remoteloki"]
    for r in remoteloki_rules:
        if "," in r.get("topics", ""):
            list(api(cmd="/system/logging/remove", **{".id": r[".id"]}))

    existing_topics = {r.get("topics") for r in remoteloki_rules if "," not in r.get("topics", "")}
    for topic in ("info", "warning", "error", "critical"):
        if topic not in existing_topics:
            list(api(cmd="/system/logging/add", topics=topic, action="remoteloki"))


# Trimmed IPFIX field template (the "keep only what a usage report needs"
# lever). This is exactly the validated v6.48.6 canary set -- an unconfigured
# router ships all ~36 fields on (v7 default), which bloats every exported
# record with tcp-seq/ack/window, MACs, ttl, tos, masks etc. that nothing in
# the pipeline reads. Field names are identical across v6 and v7, so the same
# set applies to both. Keep: timing, addresses+ports, protocol, byte/packet
# counts, in/out interface, and the nat-* addresses/ports (harmless, matches
# the canary). Everything else is turned off.
IPFIX_FIELDS_ON = frozenset({
    "bytes", "dst-address", "dst-port", "first-forwarded", "in-interface",
    "last-forwarded", "nat-dst-address", "nat-dst-port", "nat-src-address",
    "nat-src-port", "out-interface", "packets", "protocol", "src-address",
    "src-port",
})


def upsert_traffic_flow(api, interfaces, target_host, target_port,
                        cache_entries, sampling=None):
    """
    Configure /ip traffic-flow to export trimmed IPFIX to the QXM flow
    collector, mirroring the validated canary config. Three submenus:

      /ip traffic-flow set        -- enable, WAN-only interfaces, cache size,
                                     timeouts, packet-sampling
      /ip traffic-flow ipfix set  -- the trimmed field template (IPFIX_FIELDS_ON)
      /ip traffic-flow target      -- the collector (dst-address:port, ipfix)

    interfaces: comma-separated RouterOS interface list (WAN + backup uplink),
    e.g. "ether1-wahana" or "ether1,lte1" -- NOT "all" (WAN-only is the whole
    point; it captures 100% of internet flows and drops intra-LAN noise).

    sampling: None/falsey = packet-sampling off (the default, and what the
    canary runs -- byte totals come from interface counters anyway, so sampling
    only trades composition accuracy for CPU on busy routers). Otherwise a dict
    {"interval": int, "space": int} to sample interval-consecutive packets then
    skip space (packet fraction interval/(interval+space)).
    """
    settings = {
        "enabled": "yes",
        "interfaces": interfaces,
        "cache-entries": cache_entries,
        "active-flow-timeout": "30m",
        "inactive-flow-timeout": "15s",
    }
    if sampling:
        settings["packet-sampling"] = "yes"
        settings["sampling-interval"] = str(sampling["interval"])
        settings["sampling-space"] = str(sampling["space"])
    else:
        settings["packet-sampling"] = "no"
        settings["sampling-interval"] = "0"
        settings["sampling-space"] = "0"
    list(api(cmd="/ip/traffic-flow/set", **settings))

    # Trim the IPFIX template. Set every field toggle the router actually
    # exposes (yes if in IPFIX_FIELDS_ON, no otherwise) -- reading the schema
    # first, rather than blindly setting names, is the same defensive pattern
    # as upsert_syslog_forwarding: it survives any per-build field-name variance
    # (unknown fields simply get left off the template, which is the goal).
    current = list(api(cmd="/ip/traffic-flow/ipfix/print"))
    if current:
        field_kwargs = {
            k: ("yes" if k in IPFIX_FIELDS_ON else "no")
            for k in current[0] if not k.startswith(".")
        }
        if field_kwargs:
            list(api(cmd="/ip/traffic-flow/ipfix/set", **field_kwargs))

    # Point at the collector. Upsert on our (dst-address, port) so re-deploying
    # is idempotent and we never touch any other traffic-flow target the router
    # might legitimately have.
    target_kwargs = {
        "dst-address": target_host,
        "port": str(target_port),
        "version": "ipfix",
        "src-address": "0.0.0.0",
        "v9-template-refresh": "20",
        "v9-template-timeout": "30m",
        "disabled": "no",
    }
    targets = list(api(cmd="/ip/traffic-flow/target/print"))
    match = next(
        (t for t in targets
         if t.get("dst-address") == target_host
         and str(t.get("port")) == str(target_port)),
        None,
    )
    if match:
        list(api(cmd="/ip/traffic-flow/target/set", **{".id": match[".id"], **target_kwargs}))
    else:
        list(api(cmd="/ip/traffic-flow/target/add", **target_kwargs))


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


def push_to_router(
    router, ingest_base_url, metrics_templates, firmware_templates, sftp_config, syslog_config,
    baseline_templates=None, radius_config=None, gmedia_cidrs=None, flow_config=None,
):
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

    syslog_config: {"host": ..., "port": ...} for the remote syslog
    target (syslog-forwarder/) -- see upsert_syslog_forwarding.

    baseline_templates: {"v7": ..., "v6": ...} for the baseline-hardening
    script (see qoe-baseline-hardening-v7/v6.rsc) -- optional so existing
    callers that haven't been updated yet don't break; baseline push is
    skipped entirely when not provided.

    radius_config: {"secret1": ..., "secret2": ...} for the two fixed
    fleet-wide RADIUS servers the baseline script wires up.

    gmedia_cidrs: list of CIDR strings for the gmedia-all-ip allowlist
    (same list as .env's SFTP_ALLOWED_CIDRS -- see qoe-baseline-hardening
    header comment for why this is reused rather than tracked separately).

    flow_config: {"target": <collector host/ip>, "port": 4739,
    "cache_entries": "512k", "sampling": None} for the traffic-flow (IPFIX)
    export step -- optional, so callers that don't pass it (bulk_deploy) skip
    flow entirely. Applied only when the router's flow_enabled is set and its
    flow_tier isn't cgnat (see upsert_traffic_flow).

    Returns (actual_identity, warnings) on success. actual_identity is the
    router's real /system identity name -- callers should write it back
    into routers.identity_name if it differs from what's on file
    (admin-entered values can drift from the router's real identity, and
    the Loki `host` label used for per-router log correlation is always
    the real identity, not our record of it). warnings is a list of
    non-fatal misconfiguration strings (e.g. a wan_interface that doesn't
    exist on the router) the caller should surface to whoever ran the
    deploy.
    """
    token = router["auth_token"]
    wan_interface = router.get("wan_interface") or "ether1"
    wan_interface_backup = router.get("wan_interface_backup") or ""

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

        # The metrics script reads WAN counters by interface name and dies
        # on the first read if the name doesn't exist -- while the firmware
        # and hardening scripts (which never touch the WAN) keep working,
        # so the failure mode is a router whose dailies look healthy but
        # never reports a single metric. Confirmed in practice on a router
        # onboarded with pppoe-out1 when the device only had pppoe-out2.
        # Warn, don't abort: everything else about the deploy is still
        # worth pushing, and the fix is correcting the router record.
        warnings = []
        iface_names = {i.get("name") for i in api(cmd="/interface/print")}
        if wan_interface not in iface_names:
            warnings.append(
                f"wan_interface {wan_interface!r} does not exist on this router "
                f"-- metrics will NOT be reported until it is corrected. "
                f"Router has: {', '.join(sorted(n for n in iface_names if n))}"
            )
        if wan_interface_backup and wan_interface_backup not in iface_names:
            warnings.append(
                f"wan_interface_backup {wan_interface_backup!r} does not exist on this "
                f"router -- metrics will NOT be reported until it is corrected"
            )

        metrics_tpl = metrics_templates["v7"] if major_version >= 7 else metrics_templates["v6"]
        metrics_src = render_script(metrics_tpl, {
            '"https://monitor.yourisp.com/ingest"': f'"{ingest_base_url}/ingest"',
            '"PER_ROUTER_AUTH_TOKEN"': f'"{token}"',
            '"WAN_INTERFACE_PLACEHOLDER"': f'"{wan_interface}"',
            '"WAN_INTERFACE_BACKUP_PLACEHOLDER"': f'"{wan_interface_backup}"',
        })

        # Version-split like metrics/baseline: v7's `:export show-sensitive`
        # is a hard parse failure on v6, which killed every v6 router's
        # daily firmware push AND config snapshot when the flag shipped
        # fleet-wide in one shared file (2026-07-11).
        firmware_tpl = firmware_templates["v7"] if major_version >= 7 else firmware_templates["v6"]
        firmware_src = render_script(firmware_tpl, {
            '"https://monitor.yourisp.com/ingest/firmware"': f'"{ingest_base_url}/ingest/firmware"',
            '"PER_ROUTER_AUTH_TOKEN"': f'"{token}"',
            '"SFTP_HOST_PLACEHOLDER"': f'"{sftp_config["host"]}"',
            '"SFTP_PORT_PLACEHOLDER"': f'"{sftp_config["port"]}"',
            '"SFTP_USER_PLACEHOLDER"': f'"{sftp_config["user"]}"',
            '"SFTP_PASSWORD_PLACEHOLDER"': f'"{sftp_config["password"]}"',
        })

        upsert_script(api, "qoe-push-metrics", metrics_src)
        upsert_script(api, "qoe-push-firmware", firmware_src)
        upsert_scheduler(api, "qoe-push-metrics", "qoe-push-metrics", "00:05:00")
        upsert_scheduler(api, "qoe-push-firmware", "qoe-push-firmware", "1d")
        upsert_syslog_forwarding(api, syslog_config["host"], syslog_config["port"])

        if baseline_templates is not None:
            baseline_tpl = baseline_templates["v7"] if major_version >= 7 else baseline_templates["v6"]
            baseline_src = render_script(baseline_tpl, {
                '"RADIUS_SECRET_1_PLACEHOLDER"': f'"{radius_config["secret1"]}"',
                '"RADIUS_SECRET_2_PLACEHOLDER"': f'"{radius_config["secret2"]}"',
                "{GMEDIA_CIDR_ARRAY_PLACEHOLDER}": build_ros_array_literal(gmedia_cidrs),
            })
            upsert_script(api, "qoe-baseline-hardening", baseline_src)
            upsert_scheduler(api, "qoe-baseline-hardening", "qoe-baseline-hardening", "1d")

        # Traffic-flow (IPFIX) export -- gated on the Console's flow_enabled
        # switch, and skipped for CGNAT routers (their flows share a public IP
        # and can't be attributed to one customer). Only ever ENABLED here:
        # turning flow off is a deliberate manual step, so a routine deploy
        # never disables a router's traffic-flow out from under other uses.
        if flow_config is not None and router.get("flow_enabled"):
            if router.get("flow_tier") == "cgnat":
                warnings.append(
                    "flow_enabled but flow_tier=cgnat -- traffic-flow NOT pushed "
                    "(a shared/CGNAT public IP can't be attributed to one customer)"
                )
            else:
                flow_ifaces = [i for i in (wan_interface, wan_interface_backup)
                               if i and i in iface_names]
                if not flow_ifaces:
                    warnings.append(
                        "flow_enabled but no valid WAN interface on the router "
                        "-- traffic-flow NOT pushed"
                    )
                else:
                    # Sampling is a PER-ROUTER property (busy routers only),
                    # read off the router row -- not a fleet-wide flow_config
                    # default. interval 0/None = off (full capture).
                    sampling = None
                    si = router.get("flow_sampling_interval")
                    sp = router.get("flow_sampling_space")
                    if si and int(si) > 0:
                        sampling = {"interval": int(si), "space": int(sp or 0)}
                    upsert_traffic_flow(
                        api,
                        interfaces=",".join(flow_ifaces),
                        target_host=_resolve_ip(flow_config["target"]),
                        target_port=flow_config.get("port", 4739),
                        cache_entries=flow_config.get("cache_entries", "512k"),
                        sampling=sampling,
                    )

        # Best-effort -- the scheduled cycle will still deliver data on its
        # own even if this immediate kick fails (e.g. a slow/flaky router
        # timing out on the run command), so don't let that failure make
        # an otherwise-successful deploy look like it failed.
        #
        # Deliberately NOT auto-running qoe-baseline-hardening here, unlike
        # the other two -- it touches firewall/management-port/RADIUS
        # config, and firing it on every deploy (including a fleet-wide
        # /deploy-all) would remove the "test on one router first, confirm
        # reconnect" safety step. It waits for its own daily scheduler on
        # first deploy; the initial rollout run is triggered explicitly and
        # individually per pilot router during verification instead.
        try:
            run_script(api, "qoe-push-metrics")
            run_script(api, "qoe-push-firmware")
        except Exception:
            pass

        return get_identity(api), warnings
    finally:
        api.close()
