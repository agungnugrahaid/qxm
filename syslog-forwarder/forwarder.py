import json
import re
import socket
import time
import urllib.request

LISTEN_ADDR = ("0.0.0.0", 1514)
LOKI_PUSH_URL = "http://loki:3100/loki/api/v1/push"

SEVERITY_NAMES = [
    "emergency", "alert", "critical", "error",
    "warning", "notice", "informational", "debug",
]

# Grafana's Logs panel color-codes rows only when it finds a label
# literally named `level` with one of its own short keywords (critical/
# error/warning/info/debug) -- RFC 5424's long-form names above
# ("informational", "notice", "emergency"...) don't match, so without
# this mapping every line renders in the same default color.
LEVEL_NAMES = [
    "critical", "critical", "critical", "error",
    "warning", "info", "info", "debug",
]

# BSD syslog (RFC 3164), one message per UDP datagram -- what RouterOS's
# `/system logging action` with remote-protocol=udp actually sends.
# Promtail's own syslog scrape target requires RFC 6587 octet-counting
# framing even over UDP, which RouterOS doesn't do, so it silently drops
# every real packet. This forwarder parses the datagram directly instead.
PRI_RE = re.compile(rb"^<(\d+)>(.*)$", re.DOTALL)


def parse(data):
    m = PRI_RE.match(data)
    if not m:
        return "unknown", "unknown", "unknown", data.decode("utf-8", errors="replace")

    pri = int(m.group(1))
    idx = pri & 0x7
    severity = SEVERITY_NAMES[idx]
    level = LEVEL_NAMES[idx]
    rest = m.group(2).decode("utf-8", errors="replace").strip()

    # "Mon dd hh:mm:ss host tag: msg" -- host is the 4th whitespace-separated token
    parts = rest.split(None, 4)
    host = parts[3] if len(parts) >= 4 else "unknown"
    return severity, level, host, rest


def push(host, severity, level, message):
    payload = {
        "streams": [{
            "stream": {"job": "mikrotik_syslog", "host": host, "severity": severity, "level": level},
            "values": [[str(time.time_ns()), message]],
        }]
    }
    req = urllib.request.Request(
        LOKI_PUSH_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(LISTEN_ADDR)
    print(f"listening for syslog on {LISTEN_ADDR[0]}:{LISTEN_ADDR[1]}/udp", flush=True)
    while True:
        data, addr = sock.recvfrom(65535)
        severity, level, host, message = parse(data)
        try:
            push(host, severity, level, message)
        except Exception as exc:
            print(f"failed to push log from {addr}: {exc}", flush=True)


if __name__ == "__main__":
    main()
