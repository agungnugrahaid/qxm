# qoe-push-firmware.rsc
#
# Runs once a day via /system scheduler -- firmware rarely changes, so this
# doesn't need the 5-minute cadence the metrics push uses. Also pushes a
# daily config snapshot (`/export compact`) on the same cadence, since
# config doesn't change every 5 minutes either.

:local url "https://monitor.yourisp.com/ingest/firmware"
:local token "PER_ROUTER_AUTH_TOKEN"
:local sftpHost "SFTP_HOST_PLACEHOLDER"
:local sftpPort "SFTP_PORT_PLACEHOLDER"
:local sftpUser "SFTP_USER_PLACEHOLDER"
:local sftpPassword "SFTP_PASSWORD_PLACEHOLDER"
:local routerId [/system identity get name]

:local rosVersion [/system resource get version]
:local currentFw [/system routerboard get current-firmware]
:local upgradeFw [/system routerboard get upgrade-firmware]
:local architecture [/system resource get architecture-name]
:local boardName [/system resource get board-name]

# --- RouterOS package update check -- distinct from the routerboard
# bootloader firmware above (current-firmware/upgrade-firmware, which
# almost always match each other and don't tell you if a newer *RouterOS
# version* exists). check-for-updates is async against MikroTik's update
# servers, hence the :delay before reading the result. Confirmed live
# across the fleet: works and reports real channel/latest-version/status
# on routers with real internet access; on at least one router with a
# restricted network path, status comes back "ERROR: no internet
# connection" instead -- surfaced as-is rather than silently blank, since
# that's itself useful information.
:local updateChannel ""
:local updateLatest ""
:local updateStatus ""
:do {
    /system package update check-for-updates
    :delay 3
    :set updateChannel [/system package update get channel]
    :set updateStatus [/system package update get status]
    :do { :set updateLatest [/system package update get latest-version] } on-error={ }
} on-error={ }

:local payload ("{\"router_id\":\"$routerId\",\"routeros_version\":\"$rosVersion\"," . \
    "\"current_firmware\":\"$currentFw\",\"upgrade_firmware\":\"$upgradeFw\"," . \
    "\"architecture\":\"$architecture\",\"board_name\":\"$boardName\"," . \
    "\"update_channel\":\"$updateChannel\",\"latest_routeros_version\":\"$updateLatest\"," . \
    "\"update_status\":\"$updateStatus\"}")

/tool fetch url=$url http-method=post \
    http-header-field="Content-Type: application/json,Authorization: Bearer $token" \
    http-data=$payload keep-result=no

# --- Config snapshot -- exported with show-sensitive so PPPoE/hotspot/WiFi
# passwords and RADIUS shared keys are included in plaintext. This makes the
# snapshot useful as a real recovery artefact: paste the config onto a
# replacement router (minus the /user section) and it comes up fully
# configured. The data lands in router_config_snapshots in TimescaleDB,
# which is the same private DB that already holds router admin passwords --
# no change to the threat model.
#
# The scheduler entry deploying this script must carry the "sensitive"
# policy flag (see deploy_lib.py's SCRIPT_POLICY) otherwise RouterOS
# silently ignores show-sensitive and falls back to masked output.
#
# Pushed via SFTP, not HTTP: `/file get ... contents` silently returns
# nil (not truncated -- nothing) above some size threshold -- confirmed
# working at ~33KB, confirmed failing at ~81-220KB on real fleet routers.
# SFTP uploads the file directly from flash without ever materializing
# it into a script variable, so it isn't subject to that cap. Uploaded
# as "$token.rsc" -- all routers share one SFTP account (see
# docker-compose.yml's `sftp` service), so the filename itself is what
# ties the upload back to this specific router (matches the same
# auth_token already used for the HTTP pushes above).
:local exportFile ("qoe-config-" . $routerId)
:export compact show-sensitive file=$exportFile
:delay 2
:local exportFileName ($exportFile . ".rsc")
/tool fetch url=("sftp://" . $sftpUser . ":" . $sftpPassword . "@" . $sftpHost . ":" . $sftpPort . "/upload/" . $token . ".rsc") \
    src-path=$exportFileName upload=yes
/file remove [find name=$exportFileName]
