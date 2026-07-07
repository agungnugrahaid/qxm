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

# --- Connection tracking table utilization -- a busy CGNAT/customer-heavy
# router can silently start dropping new connections once this fills,
# which looks like random "sites won't load" complaints, not an obvious
# outage. Same field names (max-entries/total-entries) confirmed present
# on both RouterOS 6 and 7.
:local conntrackMax [/ip firewall connection tracking get max-entries]
:local conntrackCount [/ip firewall connection tracking get total-entries]

# --- System health (temperature, fan, voltage, power) -- v6's
# `/system health print` is a fixed singleton (unlike v7's variable-length
# gauge list, see qoe-push-metrics-v7.rsc), read via `get <property>`.
# Not every board exposes every property (e.g. no fan sensor) -- wrapped
# defensively so a missing property doesn't kill the whole script; comes
# back as an empty string in that case, which is still valid JSON here
# since the value is quoted.
#
# voltage and power-consumption specifically are stored as scaled
# integers (decivolts/deciwatts) -- `print` inserts the decimal point for
# display, but the raw `get` accessor returns the unscaled integer
# (confirmed live: get returned 236 where print showed "23.6"). Rescaled
# by hand here since RouterOS script has no floating-point division.
:local hVoltageRaw ""
:local hCurrent ""
:local hTemp ""
:local hCpuTemp ""
:local hPowerRaw ""
:local hFanSpeed ""
:do { :set hVoltageRaw [/system health get voltage] } on-error={ }
:do { :set hCurrent [/system health get current] } on-error={ }
:do { :set hTemp [/system health get temperature] } on-error={ }
:do { :set hCpuTemp [/system health get cpu-temperature] } on-error={ }
:do { :set hPowerRaw [/system health get power-consumption] } on-error={ }
:do { :set hFanSpeed [/system health get fan1-speed] } on-error={ }

:local hVoltage ""
:if ($hVoltageRaw != "") do={ :set hVoltage (($hVoltageRaw / 10) . "." . ($hVoltageRaw - (($hVoltageRaw / 10) * 10))) }
:local hPower ""
:if ($hPowerRaw != "") do={ :set hPower (($hPowerRaw / 10) . "." . ($hPowerRaw - (($hPowerRaw / 10) * 10))) }

:local healthJson ("{\"name\":\"voltage\",\"value\":\"$hVoltage\",\"unit\":\"V\"}," . \
    "{\"name\":\"current\",\"value\":\"$hCurrent\",\"unit\":\"mA\"}," . \
    "{\"name\":\"temperature\",\"value\":\"$hTemp\",\"unit\":\"C\"}," . \
    "{\"name\":\"cpu-temperature\",\"value\":\"$hCpuTemp\",\"unit\":\"C\"}," . \
    "{\"name\":\"power-consumption\",\"value\":\"$hPower\",\"unit\":\"W\"}," . \
    "{\"name\":\"fan1-speed\",\"value\":\"$hFanSpeed\",\"unit\":\"RPM\"}")

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

# --- Physical interface health (ether + SFP/SFP+ ports) ---
# /interface/ethernet is its own subsystem -- it structurally can't return
# vlan/bridge/pppoe-in/pppoe-out interfaces (those live under separate
# subsystems), so no extra type filter is needed here. Sticks to fields
# confirmed present on both RouterOS 6 and 7 (v7 adds rx-error-events and
# tx-drop-packet, v6 doesn't have them at all) so one script works on
# both, matching every other block in this file.
:local ifacesJson ""
:foreach ifaceId in=[/interface ethernet find] do={
    :local ifName [/interface ethernet get $ifaceId name]
    :local ifRunning [/interface ethernet get $ifaceId running]
    :local ifDisabled [/interface ethernet get $ifaceId disabled]
    :local fcsErr [/interface ethernet get $ifaceId rx-fcs-error]
    :local rxTooShort [/interface ethernet get $ifaceId rx-too-short]
    :local rxTooLong [/interface ethernet get $ifaceId rx-too-long]
    :local rxOverflow [/interface ethernet get $ifaceId rx-overflow]
    :local txCollision [/interface ethernet get $ifaceId tx-collision]
    :local txLateCollision [/interface ethernet get $ifaceId tx-late-collision]
    :local txUnderrun [/interface ethernet get $ifaceId tx-underrun]

    # Link-down ports (running=false) return nil, not 0, for some of
    # these counters -- confirmed in practice, not every port, not every
    # field consistently. An interpolated nil produces an empty token
    # ("rx_overflow":,), which is invalid JSON and fails the whole push.
    :if ([:typeof $fcsErr] = "nil") do={ :set fcsErr 0 }
    :if ([:typeof $rxTooShort] = "nil") do={ :set rxTooShort 0 }
    :if ([:typeof $rxTooLong] = "nil") do={ :set rxTooLong 0 }
    :if ([:typeof $rxOverflow] = "nil") do={ :set rxOverflow 0 }
    :if ([:typeof $txCollision] = "nil") do={ :set txCollision 0 }
    :if ([:typeof $txLateCollision] = "nil") do={ :set txLateCollision 0 }
    :if ([:typeof $txUnderrun] = "nil") do={ :set txUnderrun 0 }

    :if ($ifacesJson != "") do={ :set ifacesJson ($ifacesJson . ",") }
    :set ifacesJson ($ifacesJson . "{\"interface\":\"$ifName\",\"running\":$ifRunning,\"disabled\":$ifDisabled," . \
        "\"rx_fcs_error\":$fcsErr,\"rx_too_short\":$rxTooShort,\"rx_too_long\":$rxTooLong,\"rx_overflow\":$rxOverflow," . \
        "\"tx_collision\":$txCollision,\"tx_late_collision\":$txLateCollision,\"tx_underrun\":$txUnderrun}")
}

:local payload ("{\"router_id\":\"$routerId\",\"rx_bytes\":$rx,\"tx_bytes\":$tx," . \
    "\"uptime\":\"$uptimeVal\",\"cpu_load_pct\":$cpuLoad," . \
    "\"ram_used_bytes\":$usedMem,\"ram_total_bytes\":$totalMem," . \
    "\"disk_used_bytes\":$usedHdd,\"disk_total_bytes\":$totalHdd," . \
    "\"conntrack_count\":$conntrackCount,\"conntrack_max\":$conntrackMax," . \
    "\"pings\":[$pingsJson],\"dhcp_pools\":[$dhcpJson],\"uplinks\":[$uplinksJson]," . \
    "\"cpu_cores\":[$coresJson],\"interfaces\":[$ifacesJson],\"health\":[$healthJson]}")

/tool fetch url=$url http-method=post \
    http-header-field="Content-Type: application/json,Authorization: Bearer $token" \
    http-data=$payload keep-result=no
