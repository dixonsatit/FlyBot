"""Pretend to be a StackChan: publish the sim scene to MQTT and print commands.

    python tools/fake_stackchan.py --host localhost   # with flybot.mqtt_bridge running
"""
import argparse
import json
import time

import paho.mqtt.client as mqtt

from flybot.controller import ControllerConfig
from flybot.sim import scene

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="localhost")
ap.add_argument("--port", type=int, default=1883)
ap.add_argument("--base-topic", default="stackchan")
args = ap.parse_args()

state = {"pan": 0.0}


def on_message(client, userdata, msg):
    cmd = json.loads(msg.payload)
    state["pan"] = cmd["servo"]["pan_angle"]
    print(json.dumps({k: cmd[k] for k in ("servo", "face", "audio")}))


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fake-stackchan")
client.on_message = on_message
client.connect(args.host, args.port)
client.subscribe(f"{args.base_topic}/command")
client.loop_start()
cfg, t0 = ControllerConfig(), time.monotonic()
try:
    while (t := time.monotonic() - t0) < 30:
        for kind, payload in scene(t, cfg, state["pan"]).items():
            client.publish(f"{args.base_topic}/sensor/{kind}", json.dumps(payload))
        time.sleep(1 / 30)
finally:
    client.loop_stop()
