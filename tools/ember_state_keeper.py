"""Keep the gateway's latest reports as retained messages.

The gateway only sends its zone list and schedules when it connects. This
republishes every upload/<kind> report, retained, to state/<kind>/<key>
(key = zone mac, or "gateway"), so clients that connect later get the
full state at once. pyephember2's EphEmberLocal reads them.

Run it next to the broker, for example:

    python3 ember_state_keeper.py --host 127.0.0.1 --port 11883 \
        --username state --password secret

It needs paho-mqtt, and a broker login that may read +/+/upload/# and
write +/+/state/#.
"""
import argparse
import json
import paho.mqtt.client as mqtt

PARSER = argparse.ArgumentParser(description=__doc__.split("\n")[0])
PARSER.add_argument("--host", default="127.0.0.1")
PARSER.add_argument("--port", type=int, default=11883)
PARSER.add_argument("--username")
PARSER.add_argument("--password")
ARGS = PARSER.parse_args()
KINDS = {"zoneType", "pointdata", "programme", "thermostat", "commdata"}


def key_for(kind, data):
    if kind == "programme" and isinstance(data, list) and data:
        return str(data[0])
    if kind == "pointdata" and isinstance(data, dict) and data.get("mac"):
        return str(data["mac"])
    return "gateway"


def on_connect(client, userdata, flags, reason_code, properties=None):
    # paho 2 passes a ReasonCode, paho 1 an int
    if not getattr(reason_code, "is_failure", reason_code != 0):
        client.subscribe("+/+/upload/#", 1)
        print("state keeper connected", flush=True)


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) != 4 or parts[2] != "upload" or parts[3] not in KINDS:
        return
    try:
        message, _ = json.JSONDecoder().raw_decode(
            msg.payload.decode("utf-8", "replace"))
        data = message["data"]
    except (ValueError, KeyError, TypeError):
        return
    topic = "/".join(parts[:2] + ["state", parts[3], key_for(parts[3], data)])
    client.publish(topic, json.dumps(message), qos=1, retain=True)


try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "eph_state_keeper")
except AttributeError:  # paho-mqtt 1.x
    client = mqtt.Client("eph_state_keeper")
if ARGS.username:
    client.username_pw_set(ARGS.username, ARGS.password)
client.on_connect = on_connect
client.on_message = on_message
client.reconnect_delay_set(1, 30)
client.connect_async(ARGS.host, ARGS.port)
client.loop_forever(retry_first_connection=True)
