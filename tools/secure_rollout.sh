#!/bin/bash
# One-shot roll-out of the separate logins (dashboard / bridge / robot) on sith-k9s, with the robot
# on USB so it gets its new password in the same go. Nothing is changed unless the robot is
# plugged in: rotating the broker's passwords without flashing would lock the robot out.
#
#   tools/secure_rollout.sh            # home WiFi keeps using the Mac relay
#   tools/secure_rollout.sh --public   # home WiFi goes through wss://stackchan.kkh.go.th (needs
#                                      # firmware/stackchan/certs/robot.crt from Cloudflare, mTLS)
set -euo pipefail
cd "$(dirname "$0")/.."
K="kubectl --context sith-k9s -n flybot"
CFG=firmware/stackchan/src/flybot_config.h
PUBLIC=0; [ "${1:-}" = "--public" ] && PUBLIC=1

ls /dev/cu.usbmodem* >/dev/null 2>&1 || { echo "robot is not on USB: nothing changed"; exit 1; }
VERSION=$(git tag --sort=-v:refname | head -1); VERSION=${VERSION#v}
echo "image ghcr.io/dixonsatit/flybot:$VERSION"

if [ $PUBLIC = 1 ]; then
  [ -f firmware/stackchan/certs/robot.crt ] || { echo "save Cloudflare's client certificate as firmware/stackchan/certs/robot.crt first"; exit 1; }
  python3 tools/make_robot_cert.py
  sed -i '' -e 's|^#define WIFI2_MQTT_HOST .*|#define WIFI2_MQTT_HOST "wss://stackchan.kkh.go.th/mqtt"|' \
            -e 's|^#define WIFI2_MQTT_PORT .*|#define WIFI2_MQTT_PORT 443|' \
            -e 's|^#define WIFI2_VOICE_URL .*|#define WIFI2_VOICE_URL "https://stackchan.kkh.go.th/api/voice"|' "$CFG"
fi

# 1. new passwords (never printed): robot, bridge, dashboard
gen() { openssl rand -base64 30 | tr -d '/+=' | cut -c1-"$1"; }
ROBOT=$(gen 28); BRIDGE=$(gen 28); DASH=$(gen 20)
grep -q '^#define MQTT_PASSWORD' "$CFG"
python3 - "$ROBOT" "$CFG" <<'EOF'
import pathlib, re, sys
p = pathlib.Path(sys.argv[2]); s, n = re.subn(r'(?m)^#define MQTT_PASSWORD\s+".*"', '#define MQTT_PASSWORD "%s"' % sys.argv[1], p.read_text())
assert n == 1; p.write_text(s)
EOF
( cd firmware/stackchan && pio run -e stackchan-official 2>&1 | grep -E "error|SUCCESS|FAILED" )   # build before touching the cluster

# 2. cluster: secrets, two MQTT users + ACL, the bridge's logins, the image
$K patch secret flybot-secrets -p "{\"stringData\":{\"mqtt-password\":\"$ROBOT\",\"bridge-mqtt-password\":\"$BRIDGE\",\"dashboard-password\":\"$DASH\"}}" >/dev/null
$K patch configmap flybot-config --type merge -p '{"data":{"FLYBOT_USERNAME":"flybot","FLYBOT_DASHBOARD_USER":"admin"}}' >/dev/null
$K apply -f deploy/k8s/mosquitto.yaml -f deploy/k8s/ingress.yaml
$K rollout restart deploy/mosquitto >/dev/null && $K rollout status deploy/mosquitto --timeout=180s | tail -1
$K patch deploy flybot-bridge --type strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"bridge","env":[
  {"name":"FLYBOT_PASSWORD","valueFrom":{"secretKeyRef":{"name":"flybot-secrets","key":"bridge-mqtt-password"}}},
  {"name":"FLYBOT_DASHBOARD_PASSWORD","valueFrom":{"secretKeyRef":{"name":"flybot-secrets","key":"dashboard-password"}}},
  {"name":"FLYBOT_ROBOT_PASSWORD","valueFrom":{"secretKeyRef":{"name":"flybot-secrets","key":"mqtt-password"}}}]}]}}}}' >/dev/null
$K set image deploy/flybot-bridge bridge=ghcr.io/dixonsatit/flybot:"$VERSION" >/dev/null
$K rollout status deploy/flybot-bridge --timeout=420s | tail -1

# 3. robot: flash the firmware built above
( cd firmware/stackchan && pio run -e stackchan-official -t upload 2>&1 | grep -E "error|SUCCESS|FAILED" )

# 4. checks
sleep 25
echo "--- robot on the broker:"; $K logs deploy/mosquitto --since=2m | grep -E "stackchan-" | tail -2 || true
echo "--- bridge:"; $K logs deploy/flybot-bridge --since=3m | grep -E "Connected to|refused|self-test" | tail -3 || true
code() { curl -s -m 10 -o /dev/null -w "%{http_code}" "$@"; }
echo "dashboard, new login:        $(code -u "admin:$DASH" http://192.168.3.17:31880/api/state)   (want 200)"
echo "dashboard, robot's password: $(code -u "stackchan:$ROBOT" http://192.168.3.17:31880/api/state)   (want 401)"
echo "robot path, robot password:  $(code -u "stackchan:$ROBOT" http://192.168.3.17:31880/api/filler/search)   (want 200)"
echo "MQTT over WebSocket node port 32769 closed: $(nc -z -w 3 192.168.3.17 32769 2>/dev/null && echo NO || echo yes)"
echo
echo "Dashboard login is user 'admin'. Copy the password to the clipboard with:"
echo "  kubectl --context sith-k9s -n flybot get secret flybot-secrets -o jsonpath='{.data.dashboard-password}' | base64 -d | pbcopy"
