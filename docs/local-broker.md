# Local broker

The Ember gateway (EMBER-PS) keeps an MQTT connection to the EPH cloud. It is
plain MQTT 3.1.1, not TLS, to `eu-base-mqtt.topband-cloud.com` port **11883**.
The Ember app and this library use the same host name on port 18883 (TLS).

The cloud is just a broker. The gateway publishes its reports and subscribes to
its commands, all under `<productId>/<uid>/`:

| Direction | Topics |
|---|---|
| gateway → | `upload/pointdata`, `upload/programme`, `upload/thermostat`, `upload/zoneType`, `upload/commdata`, `gettime` |
| → gateway | `download/pointdata` (commands), `download/programme`, `download/reply`, `download/update`, `downtime` |

`productId` and `uid` are in every zone returned by `get_zones()`.

If the gateway connects to a broker on your network instead, this library can
publish the same commands there (`local_broker=...`) and get the reports from
there.

## 1. Run a broker on port 11883

Any MQTT broker works. Mosquitto example, **local only** (the Ember app stops
working while the gateway is redirected):

```
listener 11883
# the gateway logs in with its own username/password; accept it
allow_anonymous true
```

Keep port 11883 reachable from your LAN only. To limit what other clients can
do, add a `password_file` and `acl_file` (then the gateway's own login has to be
in the password file, see below).

## 2. Keep the Ember app working (optional): bridge to the cloud

To keep the app working, the broker has to forward the gateway's traffic to the
cloud **as the gateway**, using the gateway's own MQTT login:

- client id: the gateway's `uid`
- username: `device/<WiFi ID>/<productId>` (the WiFi ID is on the gateway's label)
- password: the one the gateway sends. The only way to find it is to log the
  gateway's MQTT CONNECT packet once, while it is redirected to you.

```
connection eph-cloud
address eu-base-mqtt.topband-cloud.com:11883
bridge_protocol_version mqttv311
try_private false
notifications false
cleansession true
remote_clientid <uid>
remote_username device/<WiFi ID>/<productId>
remote_password <gateway password>
topic <productId>/<uid>/upload/# out 1
topic <productId>/<uid>/gettime out 0
topic <productId>/<uid>/downtime in 0
topic <productId>/<uid>/download/pointdata in 1
topic <productId>/<uid>/download/programme in 1
topic <productId>/<uid>/download/reply in 1
topic <productId>/<uid>/download/update in 1
```

Map only these topics. The cloud refuses wildcards such as `#`.

**Only start the bridge once the gateway is redirected.** The bridge uses the
gateway's client id, so while the real gateway is still connected to the cloud
the two keep disconnecting each other.

## 3. Redirect the gateway

Make `eu-base-mqtt.topband-cloud.com` resolve to your broker **for the gateway
only**. Redirecting it for the whole network would also send the Ember app to
your broker, on port 18883. For example, with AdGuard Home as your DHCP and DNS
server, give the gateway a fixed address and add a custom filtering rule:

```
||eu-base-mqtt.topband-cloud.com^$dnsrewrite=<broker IP>,client=<gateway IP>
```

The gateway has to get its DNS server from your DHCP server, so renew its lease
or power-cycle it. After a broker restart it can take a few minutes to
reconnect.

## 4. Use it

```python
from pyephember2.pyephember2 import EphEmber

e = EphEmber(user, password, local_broker={
    "host": "192.168.1.20", "port": 11883,
    "username": "me", "password": "secret",   # omit if anonymous
})
```

Commands (`set_zone_target_temperature`, `set_zone_mode`, ...) and
`start_listening` then use the local broker. Logging in and reading the zones
still use the cloud.

### Without the cloud: `EphEmberLocal`

`EphEmberLocal` uses only the local broker: no Ember account, no cloud.

```python
from pyephember2.pyephember2 import EphEmberLocal

e = EphEmberLocal({"host": "192.168.1.20", "username": "me",
                   "password": "secret"},
                  names={"0a1b2c3d4": "Radiators"},   # optional
                  tz=ZoneInfo("Europe/Dublin"))
e.get_zones()                 # same format as EphEmber
e.set_zone_target_temperature("0a1b2c3d4", 20)
e.start_listening(callback)   # every report, from a background thread
e.close()
```

- Zone ids are the zones' macs (as the gateway reports them).
- The gateway doesn't know the zone names. Pass `names={mac: name}`;
  otherwise hot water zones are called "Hot Water" and others "Heating",
  numbered if there are several.
- The gateway sends its zone list and schedules **only when it connects**.
  Run [`tools/ember_state_keeper.py`](../tools/ember_state_keeper.py) next to
  the broker. It republishes the latest reports as retained messages under
  `<productId>/<uid>/state/...`, so `EphEmberLocal` gets everything as soon as
  it connects. Without it, the zones appear the next time the gateway
  reconnects.
- **Without the bridge**, nobody answers the gateway's time requests
  (`gettime`), so it reconnects about every half hour and its clock is
  never set. Run the state keeper with `--answer-time` to answer them
  as the cloud does.
- `EphEmberLocal(...)` waits up to `wait` seconds (default 10) for every zone's
  readings and schedule.

With `EphEmberLocal` you can switch the bridge off. The Ember app then stops
working, but the heating can still be controlled.

To undo all this, remove the DNS rule and power-cycle the gateway. It goes back
to the cloud.
