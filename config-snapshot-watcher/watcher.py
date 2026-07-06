"""
config-snapshot-watcher — picks up RouterOS config exports (`/export
compact`) uploaded by routers via SFTP into a shared volume, and inserts
them into router_config_snapshots.

Routers all share one SFTP account (see docker-compose.yml's `sftp`
service) rather than per-router accounts -- atmoz/sftp only reads its
user list at container startup, so per-router accounts would mean
restarting the SFTP container on every new router, which would in turn
mean giving admin-ui Docker socket access. Not justified at this fleet
size. Instead, routers upload as "<auth_token>.rsc" -- the filename
itself carries the same per-router credential already used everywhere
else (matches ingestion-api's bearer-token auth), so a file is only
ever attributed to the router whose real token it's named after.

Files with no matching token (garbage, or someone probing the shared
account) are just dropped rather than accumulating.
"""

import os
import time
from datetime import datetime, timezone

import psycopg2

UPLOAD_DIR = "/uploads/upload"  # matches the sftp service's chroot layout -- see docker-compose.yml
DATABASE_URL = os.environ["DATABASE_URL"]
POLL_INTERVAL_SECONDS = 30


def process_file(path, conn):
    token = os.path.basename(path).removesuffix(".rsc")
    cur = conn.cursor()
    cur.execute("SELECT id FROM routers WHERE auth_token = %s", (token,))
    row = cur.fetchone()
    if row:
        with open(path, "r", errors="replace") as f:
            config_text = f.read()
        cur.execute(
            "INSERT INTO router_config_snapshots (time, router_id, config_text, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (datetime.now(timezone.utc), row[0], config_text, os.path.getsize(path)),
        )
        conn.commit()
        print(f"stored config snapshot for router_id={row[0]} ({os.path.getsize(path)} bytes)")
    else:
        print(f"dropped unrecognized upload: {os.path.basename(path)}")
    os.remove(path)


def main():
    while True:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            try:
                for name in os.listdir(UPLOAD_DIR):
                    if name.endswith(".rsc"):
                        process_file(os.path.join(UPLOAD_DIR, name), conn)
            finally:
                conn.close()
        except Exception as e:
            print(f"error during poll cycle: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
