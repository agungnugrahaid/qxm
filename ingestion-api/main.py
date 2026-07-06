"""
Ingestion API — receives pushed stats from customer-premise MikroTik routers.

Each router authenticates with a per-router bearer token (set up in the
`routers` table). This also gives you offline detection for free: compare
`last_seen_at` against "now" to see which routers have gone quiet.

Two endpoints:
  POST /ingest           every ~5 min: interface counters, ping-path results,
                         DHCP pool utilization.
  POST /ingest/firmware  once a day: RouterOS version / firmware — this
                         rarely changes so it doesn't need the 5-min cadence.

Daily config snapshots (`/export compact`) are pushed over SFTP instead of
through this API — see config-snapshot-watcher/ and routeros/README.md.
`/file get ... contents` (needed to read the export into an HTTP POST
body) silently fails above some size threshold well below what real
fleet routers' configs run to, so SFTP (which transfers the file
directly from flash, never materializing it into a script variable)
replaced the HTTP push for this one.
"""

import os
from datetime import datetime, timezone
from typing import List, Optional

import psycopg2
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]

app = FastAPI(title="QoE Ingestion API")


class PingResult(BaseModel):
    target_name: str          # e.g. "google", "cloudflare"
    target_host: str          # e.g. "8.8.8.8"
    rtt_min_ms: Optional[float] = None
    rtt_avg_ms: Optional[float] = None
    rtt_max_ms: Optional[float] = None
    packet_loss_pct: Optional[float] = None


class DhcpPoolResult(BaseModel):
    pool_name: str
    total_addresses: int
    active_leases: int


class UplinkResult(BaseModel):
    label: str          # "main" or "backup"
    interface: str
    rx_bytes: int
    tx_bytes: int


class CpuCoreResult(BaseModel):
    core: str            # e.g. "cpu0"
    load_pct: float


class RouterPayload(BaseModel):
    router_id: str
    rx_bytes: int
    tx_bytes: int
    uptime: str
    cpu_load_pct: Optional[float] = None
    ram_used_bytes: Optional[int] = None
    ram_total_bytes: Optional[int] = None
    disk_used_bytes: Optional[int] = None
    disk_total_bytes: Optional[int] = None
    pings: List[PingResult] = []
    dhcp_pools: List[DhcpPoolResult] = []
    uplinks: List[UplinkResult] = []
    cpu_cores: List[CpuCoreResult] = []


class FirmwarePayload(BaseModel):
    router_id: str
    routeros_version: str
    current_firmware: str
    upgrade_firmware: Optional[str] = None
    architecture: Optional[str] = None
    board_name: Optional[str] = None


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def authenticate_router(cur, authorization: Optional[str]) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]

    # Token alone identifies the router -- it's a random 24-byte value,
    # already unique per row. Previously this also required the pushed
    # `router_id` (the CPE's own RouterOS identity) to match
    # `identity_name` exactly, which meant a typo'd or later-renamed
    # identity broke ingestion with a bare 403 instead of just working.
    cur.execute("SELECT id FROM routers WHERE auth_token = %s", (token,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Unknown router or bad token")
    return row[0]


@app.post("/ingest")
def ingest(payload: RouterPayload, authorization: str = Header(None)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        router_id = authenticate_router(cur, authorization)
        now = datetime.now(timezone.utc)

        cur.execute(
            "INSERT INTO router_metrics "
            "(time, router_id, rx_bytes, tx_bytes, uptime, cpu_load_pct, ram_used_bytes, ram_total_bytes, disk_used_bytes, disk_total_bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (now, router_id, payload.rx_bytes, payload.tx_bytes, payload.uptime,
             payload.cpu_load_pct, payload.ram_used_bytes, payload.ram_total_bytes,
             payload.disk_used_bytes, payload.disk_total_bytes),
        )

        for p in payload.pings:
            cur.execute(
                "INSERT INTO path_metrics "
                "(time, router_id, target_name, target_host, rtt_min_ms, rtt_avg_ms, rtt_max_ms, packet_loss_pct) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (now, router_id, p.target_name, p.target_host, p.rtt_min_ms, p.rtt_avg_ms, p.rtt_max_ms, p.packet_loss_pct),
            )

        for pool in payload.dhcp_pools:
            utilization = (
                round(100 * pool.active_leases / pool.total_addresses, 1)
                if pool.total_addresses > 0
                else 0
            )
            cur.execute(
                "INSERT INTO dhcp_pool_metrics "
                "(time, router_id, pool_name, total_addresses, active_leases, utilization_pct) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (now, router_id, pool.pool_name, pool.total_addresses, pool.active_leases, utilization),
            )

        for u in payload.uplinks:
            cur.execute(
                "INSERT INTO uplink_metrics (time, router_id, uplink_label, interface_name, rx_bytes, tx_bytes) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (now, router_id, u.label, u.interface, u.rx_bytes, u.tx_bytes),
            )

        for c in payload.cpu_cores:
            cur.execute(
                "INSERT INTO cpu_core_metrics (time, router_id, core_name, load_pct) "
                "VALUES (%s, %s, %s, %s)",
                (now, router_id, c.core, c.load_pct),
            )

        cur.execute("UPDATE routers SET last_seen_at = %s WHERE id = %s", (now, router_id))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/ingest/firmware")
def ingest_firmware(payload: FirmwarePayload, authorization: str = Header(None)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        router_id = authenticate_router(cur, authorization)
        now = datetime.now(timezone.utc)

        cur.execute(
            "INSERT INTO router_firmware (time, router_id, routeros_version, current_firmware, upgrade_firmware, architecture, board_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (now, router_id, payload.routeros_version, payload.current_firmware, payload.upgrade_firmware,
             payload.architecture, payload.board_name),
        )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}
