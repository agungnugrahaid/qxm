#!/bin/bash
# Restricts the SFTP config-snapshot port (docker-compose.yml's `sftp`
# service) to known network ranges only, via the DOCKER-USER iptables
# chain.
#
# UFW alone does NOT work here -- Docker inserts its own port-publishing
# rules ahead of UFW's normal INPUT-chain filtering, so a `ufw allow/deny`
# on a docker-published port is silently ignored. DOCKER-USER is the
# chain Docker itself guarantees will be consulted first, specifically
# so operators can add rules like this.
#
# Confirmed necessary in practice, not just theoretical: this port was
# seen under active brute-force (repeated failed root logins) within
# days of being opened.
#
# Install:
#   sudo bash deploy/setup-sftp-firewall.sh
#
# Re-run any time SFTP_ALLOWED_CIDRS changes (e.g. a new router's IP
# range needs adding) -- safe to re-run, replaces its own rules rather
# than duplicating them.

set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$EUID" -ne 0 ]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo ".env not found -- copy .env.example to .env and fill it in first" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${SFTP_PORT:?SFTP_PORT not set in .env}"
: "${SFTP_ALLOWED_CIDRS:?SFTP_ALLOWED_CIDRS not set in .env}"

# IMPORTANT: DOCKER-USER sits in the `filter` table's FORWARD chain, which
# traffic reaches *after* Docker's own `nat` table PREROUTING chain has
# already done its destination-NAT rewrite (host port SFTP_PORT -> the
# sftp container's actual internal port, 22 -- fixed, since that's just
# the container's own sshd listening port, unrelated to whatever host
# port .env publishes it as). Matching --dport "$SFTP_PORT" here matches
# nothing (the packet's dest port is already 22 by this point) -- rules
# silently never fire. Confirmed the hard way: rules applied cleanly,
# `iptables -L` looked correct, but brute-force traffic kept getting
# through anyway. Match the container's real listening port instead.
CONTAINER_PORT=22

TAG="qxm-sftp-acl"

echo "Removing any previously-added rules for this port..."
# Delete from highest line number to lowest so earlier deletions don't
# shift the line numbers of ones still queued for removal.
existing_lines=$(iptables -L DOCKER-USER -n --line-numbers | grep -F "$TAG" | awk '{print $1}' | sort -rn || true)
for line in $existing_lines; do
  iptables -D DOCKER-USER "$line"
done

echo "Adding allow rules for each CIDR in SFTP_ALLOWED_CIDRS..."
IFS=',' read -ra CIDRS <<< "$SFTP_ALLOWED_CIDRS"
for cidr in "${CIDRS[@]}"; do
  cidr_trimmed="$(echo "$cidr" | xargs)"
  [ -z "$cidr_trimmed" ] && continue
  iptables -A DOCKER-USER -p tcp --dport "$CONTAINER_PORT" -s "$cidr_trimmed" -m comment --comment "$TAG" -j ACCEPT
done
iptables -A DOCKER-USER -p tcp --dport "$CONTAINER_PORT" -m comment --comment "$TAG" -j DROP

echo
echo "Done. Current DOCKER-USER rules (host port $SFTP_PORT -> container port $CONTAINER_PORT):"
iptables -L DOCKER-USER -n --line-numbers | grep -E "dpt:$CONTAINER_PORT|^Chain"

echo
echo "These rules do NOT survive a reboot by default. To persist them:"
echo "  sudo apt-get install -y iptables-persistent"
echo "  sudo netfilter-persistent save"
