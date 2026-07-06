#!/bin/sh
# Watches the two web services' /health endpoints. After 3 consecutive
# failures (roughly 3 minutes) it posts to WEBHOOK_URL (Slack-compatible
# incoming-webhook JSON body — Discord and most chat webhooks accept the
# same {"text": "..."} shape too). Leave WEBHOOK_URL unset to just log
# locally without alerting anywhere.

THRESHOLD=3
FAIL_COUNT_INGEST=0
FAIL_COUNT_ADMIN=0

notify() {
  msg="$1"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $msg"
  if [ -n "$WEBHOOK_URL" ]; then
    curl -s -X POST -H 'Content-Type: application/json' \
      -d "{\"text\":\"$msg\"}" "$WEBHOOK_URL" >/dev/null 2>&1
  fi
}

while true; do
  if curl -sf http://ingestion-api:8000/health >/dev/null 2>&1; then
    FAIL_COUNT_INGEST=0
  else
    FAIL_COUNT_INGEST=$((FAIL_COUNT_INGEST + 1))
    if [ "$FAIL_COUNT_INGEST" -eq "$THRESHOLD" ]; then
      notify "⚠️ ingestion-api has failed health checks $THRESHOLD times in a row — CPE routers may not be able to push data."
    fi
  fi

  if curl -sf http://admin-ui:8082/health >/dev/null 2>&1; then
    FAIL_COUNT_ADMIN=0
  else
    FAIL_COUNT_ADMIN=$((FAIL_COUNT_ADMIN + 1))
    if [ "$FAIL_COUNT_ADMIN" -eq "$THRESHOLD" ]; then
      notify "⚠️ admin-ui has failed health checks $THRESHOLD times in a row."
    fi
  fi

  sleep 60
done
