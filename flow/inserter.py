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
# Drop decode-garbage records (see the parse loop). MikroTik regenerates the
# IPFIX template on any /ip traffic-flow config change -- and every deploy_lib
# push does -- and goflow2 decodes a short burst of records against the
# in-flight template, yielding garbage: reserved addresses and near-UInt64-max
# byte/packet counts (found on Grand Ambarrukmo: 21 rows at the sampling-enable
# instant, e.g. bytes ~1.2e19). These caps sit far above any real CPE flow
# (max seen ~0.2 GB / ~2e5 pkts over the 30m active timeout) and far below the
# garbage, so this only ever sheds decode artifacts.
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(10**13)))      # 10 TB
MAX_PACKETS = int(os.environ.get("MAX_PACKETS", str(10**10)))  # 10 billion

buf, last, total, errs, dropped, t0 = [], time.time(), 0, 0, 0, time.time()


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
                     f"buffered={len(buf)} errs={errs} dropped={dropped}\n")
    sys.stderr.flush()


next_stat = time.time() + 30
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
        nbytes = int(r.get("bytes", 0))
        npkts = int(r.get("packets", 0))
        # Shed template-refresh decode garbage (see MAX_BYTES/MAX_PACKETS above)
        # before it reaches flows_raw and skews ad-hoc raw queries.
        if nbytes > MAX_BYTES or npkts > MAX_PACKETS:
            dropped += 1
            if dropped <= 5:
                sys.stderr.write(f"[inserter] dropped malformed record "
                                 f"bytes={nbytes} packets={npkts}\n")
            continue
        ts = datetime.fromtimestamp(int(r["time_received_ns"]) // 1_000_000_000,
                                    tz=timezone.utc)
        buf.append([ts, r.get("sampler_address", ""), r.get("src_addr", ""),
                    r.get("dst_addr", ""), int(r.get("src_port", 0)),
                    int(r.get("dst_port", 0)), r.get("proto", "") or "",
                    nbytes, npkts,
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
