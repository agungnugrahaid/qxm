# qoe-push-metrics-v7.rsc -- for RouterOS v7+ only.
#
# Runs every ~5 minutes via /system scheduler. Pushes interface counters,
# ping-path results (Google + Cloudflare DNS), and DHCP pool utilization to
# the central ingestion API.
#
# Uses `/ping ... as-value` to capture structured ping results. Confirmed
# working on 7.20.4. On at least one RouterOS 6.49.8 (long-term) box,
# `as-value` isn't recognized by the script parser at all -- not a runtime
# error, a hard parse failure that kills the whole script. deploy_lib.py
# auto-selects qoe-push-metrics-v6.rsc (no ping block) for major version 6
# instead of this file -- see routeros/README.md.

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

# --- System health (temperature, fan, PSU) -- v7's `/system health print`
# returns a variable-length list of named "gauges" (differs by hardware:
# per-component temps, fan speeds/state, PSU state), unlike v6's fixed
# singleton properties (see qoe-push-metrics-v6.rsc). No `gauges` subcommand
# exists -- confirmed live, "no such command prefix" -- plain
# `/system health print` already is the gauge-list format on v7.
:local healthJson ""
:foreach hId in=[/system health find] do={
    :local gName [/system health get $hId name]
    :local gValue [/system health get $hId value]
    :local gUnit [/system health get $hId type]
    :if ($healthJson != "") do={ :set healthJson ($healthJson . ",") }
    :set healthJson ($healthJson . "{\"name\":\"$gName\",\"value\":\"$gValue\",\"unit\":\"$gUnit\"}")
}

# --- Per-core CPU load -- the system-wide cpuLoad average above can look
# fine even when one core is individually maxed out (seen in practice).
:local coresJson ""
:foreach coreId in=[/system resource cpu find] do={
    :local coreName [/system resource cpu get $coreId cpu]
    :local coreLoad [/system resource cpu get $coreId load]
    :if ($coresJson != "") do={ :set coresJson ($coresJson . ",") }
    :set coresJson ($coresJson . "{\"core\":\"$coreName\",\"load_pct\":$coreLoad}")
}

# --- Ping targets ---
# google-dns/cloudflare-dns ping fixed IPs directly -- no DNS involved,
# pure reachability. google.com/facebook.com resolve first (:resolve is a
# single lightweight DNS lookup, no meaningful router load) and ping the
# resolved IP, reporting that real IP as target_host rather than the
# domain name -- this is what actually proves DNS resolution is working,
# not just general internet reachability. If resolution fails, it falls
# back to pinging the domain name directly (which /ping can still resolve
# itself); either way the :do/on-error below reports total failure as
# 100% loss instead of crashing the whole push.
:local googleComIp "google.com"
:do { :set googleComIp [:resolve "google.com"] } on-error={ }
:local facebookComIp "facebook.com"
:do { :set facebookComIp [:resolve "facebook.com"] } on-error={ }

:local pingTargetsName {"google-dns"; "cloudflare-dns"; "google.com"; "facebook.com"}
:local pingTargetsHost {"8.8.8.8"; "1.1.1.1"; $googleComIp; $facebookComIp}
:local pingsJson ""

:for i from=0 to=([:len $pingTargetsName] - 1) do={
    :local tName ($pingTargetsName->$i)
    :local tHost ($pingTargetsHost->$i)

    :local sent 0
    :local received 0
    :local rttSum 0
    :local rttMin -1
    :local rttMax -1
    # Jitter here is the mean absolute difference between consecutive RTT
    # samples *within this one burst* (all 5 pings already hit the same
    # resolved IP -- $tHost was resolved once above, not re-resolved per
    # packet), so this is immune to a domain target's DNS resolving to a
    # different edge server between separate 5-minute polls. That's a
    # real, different phenomenon (worth seeing on the latency trend
    # graph) but isn't what "jitter" means, and would corrupt a jitter
    # figure computed across polls instead of within one.
    :local jitterSum 0
    :local jitterCount 0
    :local prevRtt -1

    :local results
    :do {
        :set results [/ping address=$tHost count=5 as-value]
    } on-error={ }
    :foreach r in=$results do={
        :set sent ($sent + 1)
        :if ([:typeof ($r->"time")] = "time") do={
            :set received ($received + 1)
            :local rttMs (($r->"time") / 1ms)
            :set rttSum ($rttSum + $rttMs)
            :if ($rttMin = -1 or $rttMs < $rttMin) do={ :set rttMin $rttMs }
            :if ($rttMax = -1 or $rttMs > $rttMax) do={ :set rttMax $rttMs }
            :if ($prevRtt != -1) do={
                :local diff ($rttMs - $prevRtt)
                :if ($diff < 0) do={ :set diff (0 - $diff) }
                :set jitterSum ($jitterSum + $diff)
                :set jitterCount ($jitterCount + 1)
            }
            :set prevRtt $rttMs
        }
    }

    :local lossPct 100
    :local rttAvg 0
    :local jitterAvg 0
    :if ($sent > 0) do={ :set lossPct (100 * ($sent - $received) / $sent) }
    :if ($received > 0) do={ :set rttAvg ($rttSum / $received) }
    :if ($jitterCount > 0) do={ :set jitterAvg ($jitterSum / $jitterCount) }
    :if ($rttMin = -1) do={ :set rttMin 0 }
    :if ($rttMax = -1) do={ :set rttMax 0 }

    :if ($pingsJson != "") do={ :set pingsJson ($pingsJson . ",") }
    :set pingsJson ($pingsJson . "{\"target_name\":\"$tName\",\"target_host\":\"$tHost\"," . \
        "\"rtt_min_ms\":$rttMin,\"rtt_avg_ms\":$rttAvg,\"rtt_max_ms\":$rttMax,\"packet_loss_pct\":$lossPct," . \
        "\"jitter_ms\":$jitterAvg}")
}

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
