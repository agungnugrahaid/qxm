# qoe-push-metrics-v6.rsc -- for RouterOS v6 (at least the "long-term"
# branch; confirmed on 6.49.8) where `/ping ... as-value` isn't recognized
# by the script parser at all. That's a hard parse failure, not a runtime
# one -- it kills the whole script, not just the ping block -- so this
# variant drops structured ping capture entirely rather than risk the
# uplink/resource/DHCP data failing to push too. Ping Latency & Loss will
# show no data for routers deployed with this file. deploy_lib.py picks
# this file automatically for major version 6 -- see routeros/README.md.
#
# Runs every ~5 minutes via /system scheduler. Pushes interface counters,
# resource usage, and DHCP pool utilization to the central ingestion API.

:local url "https://monitor.yourisp.com/ingest"
:local token "PER_ROUTER_AUTH_TOKEN"
:local wanInterface "WAN_INTERFACE_PLACEHOLDER"
:local wanInterfaceBackup "WAN_INTERFACE_BACKUP_PLACEHOLDER"
:local routerId [/system identity get name]

# WAN/uplink interface name -- varies per customer (ether1 for plain
# ethernet WAN, pppoe-out1 for PPPoE, etc.). Set per-router in admin-ui
# rather than assuming everyone's the same.
:local rx [/interface get [find name=$wanInterface] rx-byte]
:local tx [/interface get [find name=$wanInterface] tx-byte]
:local uptimeVal [/system resource get uptime]

# --- Uplink(s) traffic -- main always, backup only if this router has one ---
:local uplinksJson ("{\"label\":\"main\",\"interface\":\"$wanInterface\",\"rx_bytes\":$rx,\"tx_bytes\":$tx}")
:if ($wanInterfaceBackup != "") do={
    :local rxBackup [/interface get [find name=$wanInterfaceBackup] rx-byte]
    :local txBackup [/interface get [find name=$wanInterfaceBackup] tx-byte]
    :set uplinksJson ($uplinksJson . \
        ",{\"label\":\"backup\",\"interface\":\"$wanInterfaceBackup\",\"rx_bytes\":$rxBackup,\"tx_bytes\":$txBackup}")
}

# --- Resource usage (CPU/RAM/disk) -- same fields Winbox's status bar shows ---
:local cpuLoad [/system resource get cpu-load]
:local totalMem [/system resource get total-memory]
:local freeMem [/system resource get free-memory]
:local usedMem ($totalMem - $freeMem)
:local totalHdd [/system resource get total-hdd-space]
:local freeHdd [/system resource get free-hdd-space]
:local usedHdd ($totalHdd - $freeHdd)

# --- Per-core CPU load -- the system-wide cpuLoad average above can look
# fine even when one core is individually maxed out (seen in practice).
:local coresJson ""
:foreach coreId in=[/system resource cpu find] do={
    :local coreName [/system resource cpu get $coreId cpu]
    :local coreLoad [/system resource cpu get $coreId load]
    :if ($coresJson != "") do={ :set coresJson ($coresJson . ",") }
    :set coresJson ($coresJson . "{\"core\":\"$coreName\",\"load_pct\":$coreLoad}")
}

# --- Ping targets: intentionally empty on this build -- see header ---
:local pingsJson ""

# --- DHCP pool utilization ---
# `ranges` can hold multiple comma-separated entries, each either a
# dash range ("10.0.0.10-10.0.0.200") or CIDR ("100.64.0.0/16") --
# summed to get the pool's real total_addresses instead of the 0
# placeholder this used to report.
:local dhcpJson ""
:foreach poolName in=[/ip pool find] do={
    :local pName [/ip pool get $poolName name]
    :local ranges [/ip pool get $poolName ranges]
    :local rangeList [:toarray $ranges]
    :local totalAddresses 0
    :foreach rangeItem in=$rangeList do={
        :local slashPos [:find $rangeItem "/"]
        :if ([:typeof $slashPos] != "nil") do={
            :local prefix [:tonum [:pick $rangeItem ($slashPos + 1) [:len $rangeItem]]]
            :local hostBits (32 - $prefix)
            :local blockSize 1
            :local j 0
            :while ($j < $hostBits) do={ :set blockSize ($blockSize * 2); :set j ($j + 1) }
            :set totalAddresses ($totalAddresses + $blockSize)
        } else={
            :local dashPos [:find $rangeItem "-"]
            :local startIp [:pick $rangeItem 0 $dashPos]
            :local endIp [:pick $rangeItem ($dashPos + 1) [:len $rangeItem]]
            :set totalAddresses ($totalAddresses + ([:toip $endIp] - [:toip $startIp] + 1))
        }
    }
    # Leases are tagged with a "server" (the dhcp-server name), not the
    # pool -- a bare `lease find where status="bound"` counts every bound
    # lease router-wide, not this pool's, which is why every pool used to
    # report the same total. Find which dhcp-server(s) actually hand out
    # from this pool first, then count only their leases.
    :local activeLeases 0
    :foreach dhcpServerId in=[/ip dhcp-server find where address-pool=$pName] do={
        :local serverName [/ip dhcp-server get $dhcpServerId name]
        :set activeLeases ($activeLeases + [:len [/ip dhcp-server lease find where status="bound" and server=$serverName]])
    }

    :if ($dhcpJson != "") do={ :set dhcpJson ($dhcpJson . ",") }
    :set dhcpJson ($dhcpJson . "{\"pool_name\":\"$pName\",\"total_addresses\":$totalAddresses,\"active_leases\":$activeLeases}")
}

:local payload ("{\"router_id\":\"$routerId\",\"rx_bytes\":$rx,\"tx_bytes\":$tx," . \
    "\"uptime\":\"$uptimeVal\",\"cpu_load_pct\":$cpuLoad," . \
    "\"ram_used_bytes\":$usedMem,\"ram_total_bytes\":$totalMem," . \
    "\"disk_used_bytes\":$usedHdd,\"disk_total_bytes\":$totalHdd," . \
    "\"pings\":[$pingsJson],\"dhcp_pools\":[$dhcpJson],\"uplinks\":[$uplinksJson]," . \
    "\"cpu_cores\":[$coresJson]}")

/tool fetch url=$url http-method=post \
    http-header-field="Content-Type: application/json,Authorization: Bearer $token" \
    http-data=$payload keep-result=no
