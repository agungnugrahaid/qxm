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

:local payload ("{\"router_id\":\"$routerId\",\"routeros_version\":\"$rosVersion\"," . \
    "\"current_firmware\":\"$currentFw\",\"upgrade_firmware\":\"$upgradeFw\"," . \
    "\"architecture\":\"$architecture\",\"board_name\":\"$boardName\"}")

/tool fetch url=$url http-method=post \
    http-header-field="Content-Type: application/json,Authorization: Bearer $token" \
    http-data=$payload keep-result=no

# --- Config snapshot -- `/export` masks secrets by default (PPP secrets,
# RADIUS shared keys, WiFi PSKs), and this script's policy (see
# deploy_lib.py) deliberately excludes "sensitive" so show-sensitive
# output isn't reachable even by accident.
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
:export compact file=$exportFile
:delay 2
:local exportFileName ($exportFile . ".rsc")
/tool fetch url=("sftp://" . $sftpUser . ":" . $sftpPassword . "@" . $sftpHost . ":" . $sftpPort . "/upload/" . $token . ".rsc") \
    src-path=$exportFileName upload=yes
/file remove [find name=$exportFileName]
