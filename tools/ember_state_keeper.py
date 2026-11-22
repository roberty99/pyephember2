"""Keep the gateway's latest reports as retained messages.

The gateway only sends its zone list and schedules when it connects. This
republishes every upload/<kind> report, retained, to state/<kind>/<key>
(key = zone mac, or "gateway"), so clients that connect later get the
full state at once. pyephember2's EphEmberLocal reads them. Point
reports are merged per zone, because the gateway often reports only the
points that changed.

Run it next to the broker, for example:

    python3 ember_state_keeper.py --host 127.0.0.1 --port 11883 \
        --username state --password secret

It needs paho-mqtt, and a broker login that may read +/+/upload/# and
+/+/state/#, and write +/+/state/#.

With --answer-time it also answers the gateway's time requests (gettime ->
downtime), as the cloud does. Use it when the broker does not bridge to the
cloud: otherwise the gateway keeps reconnecting (about every half hour)
and its clock is never set. It then also needs to read +/+/gettime and
write +/+/downtime.
"""
import argparse
import base64
import json
import time
import paho.mqtt.client as mqtt

PARSER = argparse.ArgumentParser(description=__doc__.split("\n")[0])
PARSER.add_argument("--host", default="127.0.0.1")
PARSER.add_argument("--port", type=int, default=11883)
PARSER.add_argument("--username")
PARSER.add_argument("--password")
PARSER.add_argument("--answer-time", action="store_true",
                    help="answer gettime (only without a cloud bridge)")
ARGS = PARSER.parse_args()
KINDS = {"zoneType", "pointdata", "programme", "thermostat", "commdata"}

# The gateway often reports only the points that changed, so keep every
# zone's full set of points: {(productId, uid, mac): {index: raw point}}
POINTS = {}
SIZES = {1: 1, 2: 2, 4: 2, 5: 4}  # point type -> value size in bytes


def split_points(point_data):
    """base64 pointData -> {index: raw bytes of that point}"""
    raw = base64.b64decode(point_data)
    points, position = {}, 0
    while position + 3 <= len(raw):
        size = SIZES.get(raw[position + 2])
        if size is None or position + 3 + size > len(raw):
            break  # unknown type: the rest cannot be located
        points[raw[position + 1]] = raw[position:position + 3 + size]
        position += 3 + size
    return points


def merge_points(key, data):
    """Merge a pointdata report into the zone's full set; return it."""
    merged = POINTS.setdefault(key, {})
    merged.update(split_points(data["pointData"]))
    points = b"".join(merged[index] for index in sorted(merged))
    return dict(data, pointData=base64.b64encode(points).decode("ascii"))


def key_for(kind, data):
    if kind == "programme" and isinstance(data, list) and data:
        return str(data[0])
    if kind == "pointdata" and isinstance(data, dict) and data.get("mac"):
        return str(data["mac"])
    return "gateway"


def answer_time(client, product_id, uid, payload):
    """Reply to gettime like the cloud does (downtime, time in ms)."""
    try:
        serial = json.JSONDecoder().raw_decode(
            payload.decode("utf-8", "replace"))[0]["common"]["serial"]
    except (ValueError, KeyError, TypeError):
        serial = 1
    now = int(time.time() * 1000)
    reply = {"common": {"uid": uid, "productId": product_id,
                        "serial": str(serial), "timestamp": now},
             "data": {"timestamp": now}}
    client.publish("{}/{}/downtime".format(product_id, uid),
                   json.dumps(reply), qos=0)
    print("answered gettime", flush=True)


def on_connect(client, userdata, flags, reason_code, properties=None):
    # paho 2 passes a ReasonCode, paho 1 an int
    if not getattr(reason_code, "is_failure", reason_code != 0):
        # our own retained copies first, to carry on merging after a restart
        client.subscribe("+/+/state/pointdata/#", 1)
        client.subscribe("+/+/upload/#", 1)
        if ARGS.answer_time:
            client.subscribe("+/+/gettime", 0)
        print("state keeper connected", flush=True)


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) == 3 and parts[2] == "gettime":
        answer_time(client, parts[0], parts[1], msg.payload)
        return
    if len(parts) == 5 and parts[2:4] == ["state", "pointdata"]:
        try:
            data = json.loads(msg.payload)["data"]
            key = (parts[0], parts[1], parts[4])
            if key not in POINTS:
                POINTS[key] = split_points(data["pointData"])
        except (ValueError, KeyError, TypeError):
            pass
        return
    if len(parts) != 4 or parts[2] != "upload" or parts[3] not in KINDS:
        return
    try:
        message, _ = json.JSONDecoder().raw_decode(
            msg.payload.decode("utf-8", "replace"))
        data = message["data"]
    except (ValueError, KeyError, TypeError):
        return
    key = key_for(parts[3], data)
    if parts[3] == "pointdata":
        try:
            message = dict(message, data=merge_points(
                (parts[0], parts[1], key), data))
        except (ValueError, TypeError):
            return
    topic = "/".join(parts[:2] + ["state", parts[3], key])
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
