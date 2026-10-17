#!/bin/sh
# Build the robot's firmware and send it over WiFi through the bridge (instead of USB).
#   tools/ota.sh [pio env]      KCTX=<kube context> to use another cluster
# Needs kubectl access to namespace flybot; the robot must be online. Progress shows on the robot
# and on the dashboard's status tab; the new firmware's commit appears there after the restart.
set -eu
ENV=${1:-stackchan-official}
K="kubectl --context ${KCTX:-sith-k9s} -n flybot"
cd "$(dirname "$0")/../firmware/stackchan"
pio run -e "$ENV"
PW=$($K get secret flybot-secrets -o jsonpath='{.data.dashboard-password}' | base64 -d)
$K port-forward svc/flybot-dashboard 18080:8080 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 2
curl -sS -u "admin:$PW" -H 'Content-Type: application/octet-stream' \
  --data-binary @".pio/build/$ENV/firmware.bin" http://127.0.0.1:18080/api/firmware
echo
