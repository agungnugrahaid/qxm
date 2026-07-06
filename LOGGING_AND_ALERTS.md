# Log Collection & Warning Alerts

Adds two containers to the same stack — Loki (log storage) and Promtail (log shipper/receiver) — plus a Loki datasource in the Grafana you already have running. No separate server needed.

## What each piece does

- **Promtail** listens for syslog on UDP port 1514 (for your MikroTik routers) and also tails your own containers' logs (`ingestion-api`, `collector`) via the Docker socket.
- **Loki** stores everything Promtail forwards, indexed by labels (job, host, container) rather than full-text indexing everything — cheap to run, and it's what Grafana already knows how to query.
- **Grafana** now has both your metrics (TimescaleDB) and your logs (Loki) as datasources, so you can look at a QoE dip and the router's log lines from the same time window side by side.

## 1. Point MikroTik routers at the log collector

Add this to the same RouterOS setup you're already doing for the metrics push (or as its own scheduler-free, one-time config — logging doesn't need scheduling, it streams continuously once configured):

```
/system logging action add name=remote-loki target=remote remote=<your-server-ip> remote-port=1514 syslog-facility=local0

/system logging add topics=info,warning,error,critical action=remote-loki
```

If that turns out to be noisy, narrow it to the topics you actually care about instead of everything:

```
/system logging add topics=wireless action=remote-loki
/system logging add topics=ppp action=remote-loki
/system logging add topics=firewall action=remote-loki
```

## 2. Bring the new services up

```
docker compose up -d --build
docker compose logs -f promtail
```

You should see Promtail start without errors. Once a router sends its first syslog line, or your containers log anything, it'll show up in Loki within seconds.

## 3. Verify logs are flowing

In Grafana (`http://<your-server>:3000`), go to **Explore**, pick the **Loki** datasource, and query:

```
{job="mikrotik_syslog"}
```

or, for your own services:

```
{container="qoe-pilot-collector-1"}
```

(exact container label will match whatever `docker compose ps` shows — check with `{job="docker_containers"}` first if unsure.)

## 4. First warning rules (pattern/threshold tier)

Start with rules that match known-bad log lines rather than trying to detect "anomalies" — this is the tier worth building first. In Grafana: **Alerting → Alert rules → New alert rule**, pick the Loki datasource, and use queries like:

```
# Any link-down event in the last 5 minutes
count_over_time({job="mikrotik_syslog"} |= "link down" [5m])

# Repeated PPP disconnects — possible flapping line
count_over_time({job="mikrotik_syslog"} |= "ppp" |= "terminating" [15m])

# Your own collector throwing errors (poll failures, bad credentials, etc.)
count_over_time({job="docker_containers", container=~".*collector.*"} |= "error" [5m])
```

Set the condition to "is above 0" (or a small threshold for the flapping-line query, e.g. above 5), and route notifications the same way you'd route any Grafana alert (email/Slack/webhook — under **Alerting → Contact points**).

## 5. Where "real" anomaly detection fits later

Pattern rules only catch what you already know to look for. Once you've watched this run for a few weeks and have a sense of normal log volume per site/router, a good next step is a small scheduled job that computes log line counts per source per hour and flags statistical outliers (e.g. a site suddenly producing 10x its usual log volume) — that catches problems you didn't think to write a rule for. Not worth building until the pattern-based tier has been running long enough to know what "normal" looks like.
