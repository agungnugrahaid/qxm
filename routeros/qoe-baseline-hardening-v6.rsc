# qoe-baseline-hardening-v6.rsc (RouterOS v6)
#
# Automates the "default new client" baseline that used to be copy-pasted
# into the terminal by hand for every new customer router: SNMP, a
# non-default management port set, timezone/NTP, an RFC1918 + gmedia
# management-access allowlist, DNS-recursion/spam/port-scanner filters,
# RADIUS AAA, and a DNS-cache-flush scheduler.
#
# Identical to qoe-baseline-hardening-v7.rsc except the NTP block --
# RouterOS v7 replaced /system ntp client's primary-ntp/secondary-ntp
# properties with a separate servers sub-menu that doesn't exist on v6 at
# all. Kept as a fully separate file (not an in-script version branch)
# because RouterOS v6's script engine hard-fails at *run* time on
# unrecognized v7-only command paths even inside a branch that's never
# taken -- confirmed live, the same failure class already documented for
# /ping ... as-value in qoe-push-metrics-v6.rsc. If you're changing
# anything outside the NTP block, change it in both files.
#
# Deliberately NOT included, and left fully alone by this script:
#   - api / api-ssl service config -- owned by the existing onboarding
#     flow (admin-ui's router_form.html api-ssl script), which does the
#     opposite of the legacy baseline (prefers api-ssl, not plaintext
#     api) and would conflict with routers already on use_ssl=true.
#   - The legacy "backup-rsc-client" scheduler -- superseded by this
#     pilot's own qoe-push-firmware.rsc config-snapshot block (SFTP push
#     with a per-router token instead of one shared plaintext password).
#   - NAT (global src-nat + DNS redirect) -- many routers in this fleet
#     NAT from a loopback IP on an interface-less bridge, not a normal
#     WAN-bound address, so there's no safe generic way to automate this
#     across the fleet. Stays manual.
#
# Runs once a day via /system scheduler, same cadence as
# qoe-push-firmware -- this config doesn't change minute to minute.
#
# Idempotent by design: every `add` is preceded by an existence check
# keyed on the same fields the legacy config itself would naturally
# de-duplicate on (address-list membership, or a firewall rule's own
# match criteria / comment). That's what makes it safe to run against
# BOTH a brand new router (adds everything) and an existing router that
# already has some/all of this hand-applied (only adds what's missing,
# self-healing any future drift on top) -- without needing a separate
# "have we ever run this before" flag anywhere.

:local routerId [/system identity get name]
:local radiusSecret1 "RADIUS_SECRET_1_PLACEHOLDER"
:local radiusSecret2 "RADIUS_SECRET_2_PLACEHOLDER"

# --- SNMP ("A.Router" variant only -- this fleet has no radio/BTS device
# type, confirmed against the routers table). location uses this
# router's own identity name (already customer/site-descriptive) since
# there's no separate location field to pull from.
/snmp community set [find default=yes] name=client.public
/snmp set contact=support.jogja@gmedia.co.id enabled=yes location=$routerId trap-version=2

# --- Management port lockdown (api/api-ssl untouched -- see header).
# /ip service set is idempotent by nature, safe to always reapply.
/ip service set telnet disabled=yes port=5773
/ip service set ftp disabled=yes port=5771
/ip service set www disabled=yes port=5780
/ip service set ssh disabled=yes port=5772
/ip service set winbox port=5761

# --- Timezone
/system clock set time-zone-name=Asia/Jakarta

# --- NTP (v6: primary-ntp/secondary-ntp properties directly on the
# client -- confirmed live against several real v6 routers in this
# fleet, some of which already have these exact values hand-applied).
/system ntp client set enabled=yes primary-ntp=203.89.31.10 secondary-ntp=111.68.26.3

# --- RFC1918 address-list (fixed, universal -- no per-fleet variation)
:if ([:len [/ip firewall address-list find where list="rfc1918" and address="10.0.0.0/8"]] = 0) do={
    /ip firewall address-list add list=rfc1918 address=10.0.0.0/8
}
:if ([:len [/ip firewall address-list find where list="rfc1918" and address="172.16.0.0/12"]] = 0) do={
    /ip firewall address-list add list=rfc1918 address=172.16.0.0/12
}
:if ([:len [/ip firewall address-list find where list="rfc1918" and address="192.168.0.0/16"]] = 0) do={
    /ip firewall address-list add list=rfc1918 address=192.168.0.0/16
}

# --- gmedia-all-ip address-list -- the office/NOC CIDR allowlist gating
# the management-port and port-scanner rules below. Reuses the exact
# same CIDR set as .env's SFTP_ALLOWED_CIDRS (confirmed to be a superset
# of the legacy pasted list, including this pilot's own NOC egress range)
# so there's one allowlist to keep in sync, not two.
:local gmediaCidrs {GMEDIA_CIDR_ARRAY_PLACEHOLDER}
:foreach cidr in=$gmediaCidrs do={
    :if ([:len [/ip firewall address-list find where list="gmedia-all-ip" and address=$cidr]] = 0) do={
        /ip firewall address-list add list=gmedia-all-ip address=$cidr
    }
}

# --- DNS-recursion filter -- refuses to act as an open recursive
# resolver for anything outside rfc1918. Matched on each rule's own
# chain/protocol/port/action signature (not a made-up comment) so an
# already hand-applied rule (the input-chain ones were never commented
# in the legacy config) is still correctly recognized as present.
:if ([:len [/ip firewall filter find where chain="output" and protocol="udp" and src-port="53" and dst-address-list="!rfc1918" and action="drop"]] = 0) do={
    /ip firewall filter add action=drop chain=output comment="Filter DNS Recursive Router" dst-address-list=!rfc1918 protocol=udp src-port=53
}
:if ([:len [/ip firewall filter find where chain="input" and protocol="udp" and dst-port="53" and src-address-list="!rfc1918" and action="drop"]] = 0) do={
    /ip firewall filter add action=drop chain=input dst-port=53 protocol=udp src-address-list=!rfc1918
}
:if ([:len [/ip firewall filter find where chain="output" and protocol="tcp" and src-port="53" and dst-address-list="!rfc1918" and action="drop"]] = 0) do={
    /ip firewall filter add action=drop chain=output comment="Filter DNS Recursive Router" dst-address-list=!rfc1918 protocol=tcp src-port=53
}
:if ([:len [/ip firewall filter find where chain="input" and protocol="tcp" and dst-port="53" and src-address-list="!rfc1918" and action="drop"]] = 0) do={
    /ip firewall filter add action=drop chain=input dst-port=53 protocol=tcp src-address-list=!rfc1918
}

# --- Management-port access lockdown -- appeared twice (verbatim) in the
# legacy source under two different section headers; included once here.
:if ([:len [/ip firewall filter find where chain="input" and protocol="tcp" and dst-port="5761,5773,5772,5780,8728,8729,5771" and src-address-list="!gmedia-all-ip" and action="drop"]] = 0) do={
    /ip firewall filter add action=drop chain=input dst-port=5761,5773,5772,5780,8728,8729,5771 protocol=tcp src-address-list=!gmedia-all-ip comment="QOE-BASELINE: mgmt port lockdown"
}
:if ([:len [/ip firewall filter find where chain="input" and protocol="tcp" and dst-port="8291,8723,8722,8780,8728,8729" and src-address-list="!gmedia-all-ip" and action="drop"]] = 0) do={
    /ip firewall filter add action=drop chain=input dst-port=8291,8723,8722,8780,8728,8729 protocol=tcp src-address-list=!gmedia-all-ip comment="QOE-BASELINE: mgmt port lockdown (winbox alt)"
}

# --- Spam / infected-host filters (SMTP + SMB)
:if ([:len [/ip firewall filter find where comment="Detect and add-list SMTP virus or spammers"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=spammer address-list-timeout=1d chain=forward comment="Detect and add-list SMTP virus or spammers" connection-limit=30,32 disabled=no dst-port=25 limit=50,5:packet protocol=tcp
}
:if ([:len [/ip firewall filter find where comment="BLOCK SPAMMERS OR INFECTED USERS" and dst-port="25"]] = 0) do={
    /ip firewall filter add action=drop chain=forward comment="BLOCK SPAMMERS OR INFECTED USERS" disabled=no dst-port=25 protocol=tcp src-address-list=spammer
}
:if ([:len [/ip firewall filter find where comment="Detect and add-list SMB virus or spammers"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=spammer-smb address-list-timeout=1d chain=forward comment="Detect and add-list SMB virus or spammers" connection-limit=30,32 disabled=no dst-port=445 limit=50,5:packet protocol=tcp
}
:if ([:len [/ip firewall filter find where comment="BLOCK SPAMMERS OR INFECTED USERS" and dst-port="445"]] = 0) do={
    /ip firewall filter add action=drop chain=forward comment="BLOCK SPAMMERS OR INFECTED USERS" disabled=no dst-port=445 protocol=tcp src-address-list=spammer-smb dst-address-list=!rfc1918
}

# --- Port-scanner detection ("allow-access" in the legacy source, which
# was never actually defined anywhere in it, is treated as gmedia-all-ip
# per the same reasoning as the management-port rules above).
:if ([:len [/ip firewall filter find where comment="Add TCP Port Scanners to List"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="Add TCP Port Scanners to List" protocol=tcp psd=21,3s,3,1 src-address-list=!gmedia-all-ip
}
:if ([:len [/ip firewall filter find where comment="TCP FIN Stealth scan"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="TCP FIN Stealth scan" protocol=tcp src-address-list=!gmedia-all-ip tcp-flags=fin,!syn,!rst,!psh,!ack,!urg
}
:if ([:len [/ip firewall filter find where comment="TCP SYN/FIN scan"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="TCP SYN/FIN scan" protocol=tcp src-address-list=!gmedia-all-ip tcp-flags=fin,syn
}
:if ([:len [/ip firewall filter find where comment="TCP SYN/RST scan"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="TCP SYN/RST scan" protocol=tcp src-address-list=!gmedia-all-ip tcp-flags=syn,rst
}
:if ([:len [/ip firewall filter find where comment="TCP FIN/PSH/URG scan"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="TCP FIN/PSH/URG scan" protocol=tcp src-address-list=!gmedia-all-ip tcp-flags=fin,psh,urg,!syn,!rst,!ack
}
:if ([:len [/ip firewall filter find where comment="ALL/ALL TCP Scan"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="ALL/ALL TCP Scan" protocol=tcp src-address-list=!gmedia-all-ip tcp-flags=fin,syn,rst,psh,ack,urg
}
:if ([:len [/ip firewall filter find where comment="TCP NULL scan"]] = 0) do={
    /ip firewall filter add action=add-src-to-address-list address-list=port_scanners address-list-timeout=2w chain=input comment="TCP NULL scan" protocol=tcp src-address-list=!gmedia-all-ip tcp-flags=!fin,!syn,!rst,!psh,!ack,!urg
}
:if ([:len [/ip firewall filter find where comment="Drop All Port Scanners"]] = 0) do={
    /ip firewall filter add action=drop chain=input comment="Drop All Port Scanners" src-address-list=port_scanners
}

# --- RADIUS AAA -- matched on address (not the whole rule), same guard
# style as the address-lists above. use-radius is a plain `set`, safe to
# always reapply.
/user aaa set use-radius=yes
:if ([:len [/radius find where address="112.78.33.172"]] = 0) do={
    /radius add address=112.78.33.172 secret=$radiusSecret1 service=login
}
:if ([:len [/radius find where address="119.2.52.27"]] = 0) do={
    /radius add address=119.2.52.27 secret=$radiusSecret2 service=login
}

# --- DNS cache flush -- low-risk, unrelated to security hardening but
# part of the same legacy default-config block, kept as-is (just at a
# longer interval than the legacy 10m). Corrected in place (not just
# added-if-missing) so a router that already has the old 10m entry from
# an earlier run gets updated to 1h too.
:local dnsSched [/system scheduler find where name="auto-clear-cache-dns"]
:if ([:len $dnsSched] = 0) do={
    /system scheduler add interval=1h name=auto-clear-cache-dns on-event="/ip dns cache flush"
} else={
    /system scheduler set $dnsSched interval=1h
}

:log info "qoe-baseline-hardening applied"
