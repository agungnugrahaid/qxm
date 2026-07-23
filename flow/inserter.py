"""Reads goflow2 JSON flow records on stdin, batch-inserts into ClickHouse.

Runs in the same container as goflow2 (goflow2 stdout is piped to this), so the
pipe gives natural backpressure and there's no Kafka/spool file. goflow2's own
logs go to stderr and stay visible in `docker logs`.
"""
import sys, os, json, time
from datetime import datetime, timezone
import clickhouse_connect

client = clickhouse_connect.get_client(
    host=os.environ.get("CH_HOST", "clickhouse"),
    username=os.environ.get("CH_USER", "flow"),
    password=os.environ.get("CH_PASS", "flowpass"),
    database=os.environ.get("CH_DB", "flow"),
)
COLS = ["ts", "exporter_ip", "src_addr", "dst_addr", "src_port", "dst_port",
        "proto", "bytes", "packets", "in_if", "out_if", "sampling_rate"]
BATCH = int(os.environ.get("BATCH", "1000"))
FLUSH_S = float(os.environ.get("FLUSH_S", "5"))

buf, last, total, errs, t0 = [], time.time(), 0, 0, time.time()


def flush():
    global buf, total, last
    if buf:
        client.insert("flows_raw", buf, column_names=COLS)
        total += len(buf)
        buf = []
    last = time.time()


def stat():
    dt = time.time() - t0
    sys.stderr.write(f"[inserter] total={total} rate={total/dt:.1f}/s "
                     f"buffered={len(buf)} errs={errs}\n")
    sys.stderr.flush()


next_stat = time.time() + 30
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
        ts = datetime.fromtimestamp(int(r["time_received_ns"]) // 1_000_000_000,
                                    tz=timezone.utc)
        buf.append([ts, r.get("sampler_address", ""), r.get("src_addr", ""),
                    r.get("dst_addr", ""), int(r.get("src_port", 0)),
                    int(r.get("dst_port", 0)), r.get("proto", "") or "",
                    int(r.get("bytes", 0)), int(r.get("packets", 0)),
                    int(r.get("in_if", 0)), int(r.get("out_if", 0)),
                    int(r.get("sampling_rate", 0))])
    except Exception as e:
        errs += 1
        if errs <= 5:
            sys.stderr.write(f"[inserter] parse err: {e}\n")
        continue
    now = time.time()
    if len(buf) >= BATCH or (now - last) >= FLUSH_S:
        try:
            flush()
        except Exception as e:
            sys.stderr.write(f"[inserter] insert err: {e}; retry in 2s\n")
            time.sleep(2)
            try:
                flush()
            except Exception as e2:
                sys.stderr.write(f"[inserter] retry failed: {e2}; drop {len(buf)}\n")
                buf, last = [], time.time()
    if now >= next_stat:
        stat()
        next_stat = now + 30

try:
    flush()
except Exception:
    pass
stat()
