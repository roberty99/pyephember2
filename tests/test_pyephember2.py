"""Tests for pyephember2 using recorded (anonymised) API data."""

import base64
import copy
import datetime
import json
from pathlib import Path
from types import SimpleNamespace

from zoneinfo import ZoneInfo

import pytest
import requests

import pyephember2.pyephember2 as pe
from pyephember2.pyephember2 import EphEmber, ZoneCommand, ZoneMode

FIXTURES = Path(__file__).parent / "fixtures"
REAL_HTTP = EphEmber._http

# 2026-11-12 is a Thursday
THURSDAY_0930_UTC = int(
    datetime.datetime(2026, 11, 12, 9, 30, tzinfo=datetime.timezone.utc)
    .timestamp() * 1000
)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def load_fixture(name="ember_ps_zones.json"):
    data = json.loads((FIXTURES / name).read_text())
    # ids are anonymised to "X"; give each zone its own
    for number, zone in enumerate(data["data"]):
        zone["zoneid"] = "zone{}".format(number)
    return data


def make_ember(monkeypatch, zone_program, homes=None,
               tz=datetime.timezone.utc):
    """Build an EphEmber whose HTTP calls are answered from fixtures."""
    homes = homes or [{"gatewayid": "GW1", "name": "Home"}]
    calls = []

    def fake_http(self, endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint == "appLogin/login":
            return FakeResponse(
                {"status": 0, "data": {"token": "t", "refresh_token": "r"}}
            )
        if endpoint == "homes/list":
            return FakeResponse({"status": 0, "data": copy.deepcopy(homes)})
        if endpoint == "homesVT/zoneProgram":
            return FakeResponse(copy.deepcopy(zone_program))
        raise AssertionError("unexpected endpoint " + endpoint)

    monkeypatch.setattr(EphEmber, "_http", fake_http)
    ember = EphEmber("user", "pass", tz=tz)
    ember.calls = calls
    return ember


def zones_by_name(ember):
    return {z["name"]: z for home in ember.get_zones() for z in home["zones"]}


def set_point(zone_program, zone_name, index, value):
    for zone in zone_program["data"]:
        if zone["name"] == zone_name:
            for point in zone["pointDataList"]:
                if point["pointIndex"] == index:
                    point["value"] = str(value)
                    return
    raise KeyError((zone_name, index))


# --- reading values -------------------------------------------------------

def test_reads_ember_ps_zones(monkeypatch):
    zones = zones_by_name(make_ember(monkeypatch, load_fixture()))

    radiators = zones["Radiators"]
    assert pe.zone_current_temperature(radiators) == 20.0
    assert pe.zone_target_temperature(radiators) == 20.5
    assert pe.zone_mode(radiators) == ZoneMode.OFF
    assert pe.boiler_state(radiators) == 1
    assert not pe.zone_is_hotwater(radiators)

    hot_water = zones["Hot Water"]
    assert pe.zone_is_hotwater(hot_water)
    assert pe.zone_target_temperature(hot_water) == 60.0


@pytest.mark.parametrize(
    "value,expected",
    [(0, ZoneMode.AUTO), (1, ZoneMode.ALL_DAY), (2, ZoneMode.ON),
     (3, ZoneMode.OFF)],
)
def test_zone_mode_values(monkeypatch, value, expected):
    data = load_fixture()
    set_point(data, "Radiators", 7, value)
    zone = zones_by_name(make_ember(monkeypatch, data))["Radiators"]
    assert pe.zone_mode(zone) == expected


def test_auto_mode_with_unused_midnight_slots(monkeypatch):
    """Unused schedule slots come back as startTime/endTime 0 (midnight)."""
    data = load_fixture()
    set_point(data, "Radiators", 7, 0)  # AUTO
    data["timestamp"] = THURSDAY_0930_UTC
    zone = zones_by_name(make_ember(monkeypatch, data))["Radiators"]

    assert pe.zone_mode(zone) == ZoneMode.AUTO
    assert pe.zone_is_scheduled_on(zone) is False
    assert pe.zone_is_active(zone) is False


def test_missing_point_returns_none(monkeypatch):
    data = load_fixture()
    for zone in data["data"]:
        zone["pointDataList"] = [
            p for p in zone["pointDataList"] if p["pointIndex"] not in (5, 6)
        ]
    zone = zones_by_name(make_ember(monkeypatch, data))["Radiators"]
    assert pe.zone_current_temperature(zone) is None
    assert pe.zone_target_temperature(zone) is None


@pytest.mark.parametrize(
    "stime,expected",
    [
        (0, datetime.time(0, 0)),
        (3, datetime.time(0, 30)),
        (70, datetime.time(7, 0)),
        (173, datetime.time(17, 30)),
        (235, datetime.time(23, 50)),
        (240, datetime.time.max),
        (None, None),
    ],
)
def test_scheduletime_to_time(stime, expected):
    assert pe.scheduletime_to_time({"startTime": stime}, "startTime") == expected


def test_get_zone_names(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    assert sorted(ember.get_zone_names()) == ["Hot Water", "Radiators"]


def test_get_zone_unknown(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    with pytest.raises(RuntimeError):
        ember.get_zone("nope")


# --- writing commands ------------------------------------------------------

def test_zone_command_index_is_optional():
    assert ZoneCommand("BOOST_HOURS", 1).index is None


@pytest.mark.parametrize(
    "command,expected",
    [
        (ZoneCommand("TARGET_TEMP", 21.5), [0, 6, 4, 0, 215]),
        (ZoneCommand("MODE", 2), [0, 7, 1, 2]),
        (ZoneCommand("BOOST_HOURS", 1), [0, 8, 1, 1]),
        (ZoneCommand("ADVANCE_ACTIVE", 1), [0, 4, 1, 1]),
        (ZoneCommand("BOOST_TIME", 0x01020304), [0, 9, 5, 1, 2, 3, 4]),
    ],
)
def test_zone_command_to_ints(command, expected):
    assert pe.zone_command_to_ints({"deviceType": 2}, command) == expected


def test_zone_command_uses_device_specific_index():
    etrv = {"deviceType": 773}
    assert pe.zone_command_to_ints(etrv, ZoneCommand("TARGET_TEMP", 20))[1] == 12
    assert pe.zone_command_to_ints(etrv, ZoneCommand("MODE", 1))[1] == 11


def test_read_only_command_rejected():
    with pytest.raises(ValueError):
        pe.zone_command_to_ints({"deviceType": 2}, ZoneCommand("CURRENT_TEMP", 1))


def test_boost_and_advance_build_commands(monkeypatch):
    """These used to fail with 'missing 1 required positional argument'."""
    ember = make_ember(monkeypatch, load_fixture())
    zoneid = zones_by_name(ember)["Radiators"]["zoneid"]
    sent = []
    monkeypatch.setattr(
        ember.messenger, "send_zone_commands",
        lambda zone, cmds, *a, **k: sent.append(cmds) or True,
    )
    assert ember.activate_zone_boost(zoneid, boost_temperature=22, num_hours=1)
    assert ember.deactivate_zone_boost(zoneid)
    assert ember.set_zone_advance(zoneid, True)
    assert ember.set_zone_boost_temperature(zoneid, 22)
    for cmds in sent:
        cmds = cmds if isinstance(cmds, list) else [cmds]
        for cmd in cmds:
            pe.zone_command_to_ints({"deviceType": 2}, cmd)


def test_set_zone_mode_command(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    zoneid = zones_by_name(ember)["Radiators"]["zoneid"]
    sent = []
    monkeypatch.setattr(
        ember.messenger, "send_zone_commands",
        lambda zone, cmds, *a, **k: sent.append(cmds) or True,
    )
    ember.set_zone_mode(zoneid, ZoneMode.ON)
    assert pe.zone_command_to_ints({"deviceType": 2}, sent[0][0]) == [0, 7, 1, 2]


# --- errors and auth ---------------------------------------------------------

def test_network_error_is_runtime_error(monkeypatch):
    """Callers such as Home Assistant catch RuntimeError."""
    def boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("Host is unreachable")

    ember = make_ember(monkeypatch, load_fixture())
    monkeypatch.setattr(EphEmber, "_http", REAL_HTTP)
    monkeypatch.setattr(requests, "get", boom)
    ember.NextHomeUpdateDaytime = None
    with pytest.raises(pe.EphEmberError) as err:
        ember.get_zones()
    assert isinstance(err.value, RuntimeError)


def test_relogin_when_refresh_token_rejected(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    ember._login_data["last_refresh"] -= datetime.timedelta(hours=2)

    original = EphEmber._http

    def http(self, endpoint, **kwargs):
        if endpoint == "appLogin/refreshAccessToken":
            return FakeResponse({"status": 7, "data": None})
        return original(self, endpoint, **kwargs)

    monkeypatch.setattr(EphEmber, "_http", http)
    ember.calls.clear()
    assert ember._do_auth()
    assert ember.calls == ["appLogin/login"]


# --- schedules in local time ------------------------------------------------

DUBLIN = ZoneInfo("Europe/Dublin")
# 2026-07-15 is a Wednesday; 16:45 UTC is 17:45 Irish summer time
WEDNESDAY_1645_UTC = int(
    datetime.datetime(2026, 7, 15, 16, 45, tzinfo=datetime.timezone.utc)
    .timestamp() * 1000
)


def auto_hot_water(monkeypatch, timestamp, tz):
    data = load_fixture()
    set_point(data, "Hot Water", 7, 0)  # AUTO
    data["timestamp"] = timestamp
    return zones_by_name(make_ember(monkeypatch, data, tz=tz))["Hot Water"]


def test_schedule_uses_home_time_zone(monkeypatch):
    """The 17:30-19:30 slot is running at 17:45 Irish summer time."""
    zone = auto_hot_water(monkeypatch, WEDNESDAY_1645_UTC, DUBLIN)
    assert pe.getZoneTime(zone) == [datetime.time(17, 45), 3]
    assert pe.zone_is_scheduled_on(zone) is True


def test_schedule_in_utc_is_an_hour_out(monkeypatch):
    """What 0.4.13 did: 16:45 UTC is before the 17:30 slot starts."""
    zone = auto_hot_water(monkeypatch, WEDNESDAY_1645_UTC,
                          datetime.timezone.utc)
    assert pe.getZoneTime(zone) == [datetime.time(16, 45), 3]
    assert pe.zone_is_scheduled_on(zone) is False


def test_day_type_wraps_to_sunday():
    sunday = int(datetime.datetime(
        2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    zone = {"timestamp": sunday, "tzinfo": datetime.timezone.utc}
    assert pe.getZoneTime(zone)[1] == 0


def test_local_time_used_without_tz(monkeypatch):
    zone = {"timestamp": WEDNESDAY_1645_UTC}
    local = datetime.datetime.fromtimestamp(WEDNESDAY_1645_UTC / 1000)
    assert pe.getZoneTime(zone)[0] == datetime.time(local.hour, local.minute)


# --- online state ------------------------------------------------------------

def test_zone_is_online(monkeypatch):
    data = load_fixture()
    data["data"][1]["isonline"] = False
    ember = make_ember(monkeypatch, data)
    zones = zones_by_name(ember)
    assert pe.zone_is_online(zones["Hot Water"]) is True
    assert pe.zone_is_online(zones["Radiators"]) is False
    assert ember.is_zone_online(zones["Radiators"]["zoneid"]) is False
    assert pe.zone_is_online({}) is True


# --- cache updated after commands --------------------------------------------

def fake_sender(monkeypatch, ember, result=True):
    sent = []
    monkeypatch.setattr(
        ember.messenger, "send_zone_commands",
        lambda zone, cmds, *a, **k: sent.append(cmds) or result,
    )
    return sent


def test_command_updates_cached_zone(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    radiators = zones_by_name(ember)["Radiators"]
    zoneid = radiators["zoneid"]
    fake_sender(monkeypatch, ember)

    assert ember.set_zone_target_temperature(zoneid, 21.5)
    assert ember.set_zone_mode(zoneid, ZoneMode.ON)
    assert ember.activate_zone_boost(zoneid, boost_temperature=23, num_hours=2)

    # the same dict callers (e.g. Home Assistant) already hold is updated
    assert pe.zone_target_temperature(radiators) == 21.5
    assert pe.zone_mode(radiators) == ZoneMode.ON
    assert pe.zone_boost_hours(radiators) == 2
    assert pe.zone_boost_temperature(radiators) == 23.0

    # and the cloud is not asked again (it would still report the old values)
    ember.calls.clear()
    zones_by_name(ember)
    assert ember.calls == []


def test_cache_expires_after_settle_time(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    zoneid = zones_by_name(ember)["Radiators"]["zoneid"]
    fake_sender(monkeypatch, ember)
    ember.set_zone_target_temperature(zoneid, 21.5)

    ember.NextHomeUpdateDaytime = datetime.datetime.now() - datetime.timedelta(seconds=1)
    ember.calls.clear()
    radiators = zones_by_name(ember)["Radiators"]
    assert "homesVT/zoneProgram" in ember.calls
    assert pe.zone_target_temperature(radiators) == 20.5


def test_failed_command_leaves_cache_alone(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    radiators = zones_by_name(ember)["Radiators"]
    fake_sender(monkeypatch, ember, result=False)
    assert not ember.set_zone_target_temperature(radiators["zoneid"], 21.5)
    assert pe.zone_target_temperature(radiators) == 20.5


def test_apply_command_adds_missing_point():
    zone = {"deviceType": 2, "pointDataList": []}
    pe.zone_apply_commands(zone, ZoneCommand("TARGET_TEMP", 19))
    assert zone["pointDataList"] == [{"pointIndex": 6, "value": "190"}]


def test_temperature_rounding():
    """20.1 * 10 is 200.99999; it must be sent as 201."""
    assert pe.zone_command_to_ints(
        {"deviceType": 2}, ZoneCommand("TARGET_TEMP", 20.1)) == [0, 6, 4, 0, 201]


# --- MQTT ---------------------------------------------------------------------

class FakeMqttClient:
    """Stands in for paho's Client; connects (or refuses) on loop_start."""

    instances = []

    def __init__(self, *args, refuse=False, **kwargs):
        self.refuse = refuse
        self.connected = False
        self.loop_running = False
        self.published = []
        self.on_connect = None
        self.closed = False
        FakeMqttClient.instances.append(self)

    def tls_set(self):
        self.tls = True

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def connect(self, host, port):
        self.target = (host, port)

    def connect_async(self, host, port):
        self.target = (host, port)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delay = (min_delay, max_delay)

    def subscribe(self, topic, qos):
        self.subscribed = getattr(self, "subscribed", []) + [(topic, qos)]
        return 0, 1

    def loop_start(self):
        self.loop_running = True
        reason = FakeReason(self.refuse)
        self.connected = not self.refuse
        self.on_connect(self, None, None, reason, None)

    def loop_stop(self):
        self.loop_running = False

    def is_connected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def socket(self):
        return None

    def publish(self, topic, msg, qos):
        self.published.append((topic, json.loads(msg), qos))
        return FakePublish()


class FakeReason:
    def __init__(self, failure):
        self.is_failure = failure

    def __str__(self):
        return "Not authorized" if self.is_failure else "Success"


class FakePublish:
    def wait_for_publish(self, timeout):
        pass

    def is_published(self):
        return True


def test_mqtt_command_waits_for_connection_and_disconnects(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(pe.mqtt, "Client", FakeMqttClient)
    zone = zones_by_name(ember)["Radiators"]

    assert ember.set_zone_target_temperature(zone["zoneid"], 21)
    client = FakeMqttClient.instances[-1]
    topic, msg, qos = client.published[0]
    assert topic.endswith("/download/pointdata")
    assert msg["data"]["pointData"] == "AAYEANI="  # [0, 6, 4, 0, 210]
    assert not client.connected and not client.loop_running

    # sent like the Ember app: acknowledged, with the user ID and a serial
    # taken from the timestamp
    assert qos == 1
    assert msg["common"]["userId"] == "42"
    assert msg["common"]["serial"] == msg["common"]["timestamp"] % 1000000


def test_mqtt_serial_never_repeats(monkeypatch):
    """The gateway can ignore a repeated serial as a duplicate message."""
    ember = make_ember(monkeypatch, load_fixture())
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(pe.mqtt, "Client", FakeMqttClient)
    monkeypatch.setattr(pe.time, "time", lambda: 1794551000.123)
    zoneid = zones_by_name(ember)["Radiators"]["zoneid"]

    ember.set_zone_target_temperature(zoneid, 21.5)
    ember.set_zone_target_temperature(zoneid, 21)
    serials = [c.published[0][1]["common"]["serial"]
               for c in FakeMqttClient.instances]
    assert serials == [123, 124]


def test_mqtt_refused_raises(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    monkeypatch.setattr(
        pe.mqtt, "Client",
        lambda *a, **k: FakeMqttClient(refuse=True))
    zone = zones_by_name(ember)["Radiators"]

    with pytest.raises(pe.EphEmberError, match="Not authorized"):
        ember.set_zone_target_temperature(zone["zoneid"], 21)
    assert pe.zone_target_temperature(zone) == 20.5


def test_mqtt_connect_error_raises(monkeypatch):
    class Unreachable(FakeMqttClient):
        def connect(self, host, port):
            raise OSError("Network is unreachable")

    ember = make_ember(monkeypatch, load_fixture())
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    monkeypatch.setattr(pe.mqtt, "Client", Unreachable)
    zone = zones_by_name(ember)["Radiators"]
    with pytest.raises(pe.EphEmberError, match="unreachable"):
        ember.set_zone_target_temperature(zone["zoneid"], 21)


# --- live updates -------------------------------------------------------------

def report(mac, points, padding="\x00\x00"):
    """An upload/pointdata message as the gateway sends it."""
    raw = bytes(b for index, type_id, value in points
                for b in [0, index, type_id]
                + list(value.to_bytes(pe._POINT_TYPE_SIZES[type_id], "big")))
    return (json.dumps({
        "common": {"serial": 281, "productId": "P", "uid": "U",
                   "timestamp": 2086344762},
        "data": {"mac": mac, "pointData": base64.b64encode(raw).decode()},
    }) + padding).encode()


def test_parse_point_data():
    # recorded from an EMBER-PS gateway: target 21.5, then counters
    assert pe.parse_point_data("AAYEANcADwUTE6+vABAFvUAAAA==") == [
        (6, 215), (15, 320057263), (16, 3175088128)]


def test_parse_point_data_stops_at_unknown_type():
    raw = base64.b64encode(bytes([0, 6, 4, 0, 215, 0, 7, 9, 1])).decode()
    assert pe.parse_point_data(raw) == [(6, 215)]


def listening_ember(monkeypatch):
    data = load_fixture()
    for number, zone in enumerate(data["data"]):
        zone["mac"] = "mac{}".format(number)
        zone["productId"], zone["uid"] = "P", "U"
    ember = make_ember(monkeypatch, data)
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(pe.mqtt, "Client", FakeMqttClient)
    updates = []
    ember.start_listening(updates.append)
    return ember, FakeMqttClient.instances[-1], updates


def test_live_updates(monkeypatch):
    ember, client, updates = listening_ember(monkeypatch)
    assert client.subscribed == [("P/U/upload/pointdata", 1)]
    assert client.loop_running and ember.is_listening()

    radiators = zones_by_name(ember)["Radiators"]
    message = SimpleNamespace(payload=report(
        radiators["mac"], [(6, 4, 220), (5, 2, 188), (10, 1, 2)]))
    client.on_message(client, None, message)

    assert updates == [radiators]
    assert pe.zone_target_temperature(radiators) == 22.0
    assert pe.zone_current_temperature(radiators) == 18.8
    assert pe.boiler_state(radiators) == 2


def test_live_updates_ignore_unknown_and_bad_messages(monkeypatch):
    ember, client, updates = listening_ember(monkeypatch)
    for payload in (report("other", [(6, 4, 220)]), b"not json",
                    b'{"data": {}}'):
        client.on_message(client, None, SimpleNamespace(payload=payload))
    assert updates == []


def test_live_updates_reconnect_with_fresh_token(monkeypatch, caplog):
    ember, client, updates = listening_ember(monkeypatch)
    tokens = iter(["new", "newer"])
    monkeypatch.setattr(ember, "messenging_credentials",
                        lambda: {"user_id": "42", "token": next(tokens)})
    caplog.set_level("DEBUG", logger=pe.__name__)
    # paho reports a keep-alive timeout twice
    client.on_disconnect(client, None, None, FakeReason(True), None)
    client.on_disconnect(client, None, None, FakeReason(True), None)
    assert caplog.text.count("Ember live updates disconnected") == 1
    # every attempt still gets a fresh login, e.g. after a refused reconnect
    assert client.credentials == ("app/newer", "newer")


def test_stop_listening(monkeypatch):
    ember, client, updates = listening_ember(monkeypatch)
    ember.stop_listening()
    assert not client.loop_running and not ember.is_listening()
    # a disconnect caused by stopping does not log in again
    monkeypatch.setattr(ember, "messenging_credentials",
                        lambda: pytest.fail("should not refresh"))
    client.on_disconnect(client, None, None, FakeReason(False), None)


# --- local broker -------------------------------------------------------------

LOCAL = {"host": "192.168.1.20", "username": "me", "password": "pw"}


def test_local_broker_command(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    ember.local_broker = dict(LOCAL, port=11883)
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(pe.mqtt, "Client", FakeMqttClient)
    monkeypatch.setattr(ember, "messenging_credentials",
                        lambda: pytest.fail("no cloud MQTT login"))
    zone = zones_by_name(ember)["Radiators"]

    assert ember.set_zone_target_temperature(zone["zoneid"], 21)
    client = FakeMqttClient.instances[-1]
    assert client.target == ("192.168.1.20", 11883)
    assert client.credentials == ("me", "pw")
    assert not getattr(client, "tls", False)
    topic, msg, qos = client.published[0]
    assert topic.endswith("/download/pointdata")
    assert msg["common"]["userId"] == "42"


def test_local_broker_default_port(monkeypatch):
    monkeypatch.setattr(EphEmber, "_login", lambda self: True)
    ember = EphEmber("u", "p", local_broker={"host": "h"})
    assert ember.local_broker == {"host": "h", "port": 11883}


def test_local_broker_works_while_cloud_is_down(monkeypatch):
    ember = make_ember(monkeypatch, load_fixture())
    ember._user["user_id"] = "42"
    ember.local_broker = dict(LOCAL, port=11883)

    def cloud_down(*args, **kwargs):
        raise requests.ConnectionError("cloud down")

    monkeypatch.setattr(ember, "_do_auth", cloud_down)
    assert ember.cached_user_id() == "42"


def test_local_broker_listener_does_not_refresh_token(monkeypatch):
    data = load_fixture()
    for number, zone in enumerate(data["data"]):
        zone["mac"] = "mac{}".format(number)
        zone["productId"], zone["uid"] = "P", "U"
    ember = make_ember(monkeypatch, data)
    monkeypatch.setattr(ember, "_get_user_id", lambda force=False: "42")
    ember.local_broker = dict(LOCAL, port=11883)
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(pe.mqtt, "Client", FakeMqttClient)
    ember.start_listening(lambda zone: None)
    client = FakeMqttClient.instances[-1]
    assert client.target == ("192.168.1.20", 11883)
    assert client.subscribed == [("P/U/upload/pointdata", 1)]
    monkeypatch.setattr(ember, "messenging_credentials",
                        lambda: pytest.fail("should not refresh"))
    client.on_disconnect(client, None, None, FakeReason(True), None)
    assert client.credentials == ("me", "pw")


def test_user_id_failure_is_a_connection_error(monkeypatch):
    """A failed user lookup after a good login isn't a rejected password."""
    ember = make_ember(monkeypatch, load_fixture())
    original = EphEmber._http

    def http(self, endpoint, **kwargs):
        if endpoint == "user/selectUser":
            return FakeResponse({"status": 500, "data": None})
        return original(self, endpoint, **kwargs)

    monkeypatch.setattr(EphEmber, "_http", http)
    with pytest.raises(pe.EphEmberError, match="Cannot get user ID"):
        ember.messenging_credentials()


# --- cloud-free (EphEmberLocal) -----------------------------------------------

HW_POINTS = ("AAMBDAAEAQAABQIBbQAGBAJYAAcBAwAIAQAACQUAAAAAAAoBAQALAQAADQEB"
             "AA4EAlgADwUABQAFABAFAAAAAAARBQAAAAAAEgUABQAF")
RAD_POINTS = ("AAMBDQAEAQAABQIAwgAGBAC+AAcBAgAIAQAACQUAAAAAAAoBAQALAQAADQEB"
              "AA4EAL4ADwUTE6+vABAF3IIAAAARBQAAAAoAEgUABQAD")
HW_PROGRAMME = [[[70, 80], [173, 193], [193, 195]],
                [[0, 0], [0, 0], [194, 195]]] + [
                    [[70, 80], [173, 193], [193, 195]]] * 5


def local_message(topic, data, padding=b""):
    payload = json.dumps({"common": {"serial": 1}, "data": data}).encode()
    return SimpleNamespace(topic="P/U/" + topic, payload=payload + padding)


def local_ember(monkeypatch, **kwargs):
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(pe.mqtt, "Client", FakeMqttClient)
    ember = pe.EphEmberLocal({"host": "h", "username": "me",
                              "password": "pw"}, wait=0, **kwargs)
    return ember, FakeMqttClient.instances[-1]


def gateway_reports(client, order=("zoneType", "pointdata", "programme")):
    messages = {
        "zoneType": [local_message("state/zoneType/gateway", {"devices": [
            {"productId": "X", "mac": "hw", "DType": 4, "online": 1},
            {"productId": "X", "mac": "rad", "DType": 2, "online": 1}]})],
        "pointdata": [
            local_message("state/pointdata/hw",
                          {"mac": "hw", "pointData": HW_POINTS}),
            local_message("upload/pointdata",
                          {"mac": "rad", "pointData": RAD_POINTS}, b"\0\0")],
        "programme": [
            local_message("state/programme/hw", ["hw", HW_PROGRAMME]),
            local_message("state/programme/rad", ["rad", [[[0, 0]] * 3] * 7])],
    }
    for kind in order:
        for message in messages[kind]:
            client.on_message(client, None, message)


def test_local_connects_without_cloud(monkeypatch):
    monkeypatch.setattr(EphEmber, "_http",
                        lambda *a, **k: pytest.fail("no cloud"))
    ember, client = local_ember(monkeypatch)
    assert client.target == ("h", 11883)
    assert client.credentials == ("me", "pw")
    assert ("+/+/state/#", 1) in client.subscribed
    assert ("+/+/upload/#", 1) in client.subscribed
    assert ember.get_zones() == []


@pytest.mark.parametrize("order", [
    ("zoneType", "pointdata", "programme"),
    ("programme", "pointdata", "zoneType"),
])
def test_local_zones_from_gateway_reports(monkeypatch, order):
    ember, client = local_ember(
        monkeypatch, names={"rad": "Radiators"}, tz=ZoneInfo("Europe/Dublin"))
    gateway_reports(client, order)
    assert ember._ready.is_set()

    zones = zones_by_name(ember)
    assert set(zones) == {"Hot Water", "Radiators"}
    hot_water, radiators = zones["Hot Water"], zones["Radiators"]
    assert hot_water["zoneid"] == "hw" and hot_water["deviceType"] == 4
    assert (hot_water["productId"], hot_water["uid"]) == ("P", "U")
    assert pe.zone_target_temperature(hot_water) == 60.0
    assert pe.zone_current_temperature(hot_water) == 36.5
    assert pe.zone_target_temperature(radiators) == 19.0
    assert pe.zone_current_temperature(radiators) == 19.4
    assert pe.zone_mode(radiators) == ZoneMode.ON
    # Sunday's schedule, as the cloud would give it
    sunday = hot_water["days"][0]["programs"]
    assert [(p["startTime"], p["endTime"]) for p in sunday.values()] == [
        (70, 80), (173, 193), (193, 195)]
    assert ember.get_zone("rad") is radiators


def test_local_default_names(monkeypatch):
    ember, client = local_ember(monkeypatch)
    client.on_message(client, None, local_message(
        "upload/zoneType", {"devices": [
            {"mac": "a", "DType": 2}, {"mac": "b", "DType": 2},
            {"mac": "c", "DType": 4}]}))
    assert sorted(ember.get_zone_names()) == [
        "Heating", "Heating 2", "Hot Water"]


def test_local_not_ready_without_schedules(monkeypatch):
    ember, client = local_ember(monkeypatch)
    gateway_reports(client, ("zoneType", "pointdata"))
    assert not ember._ready.is_set()


def test_local_live_updates_and_online(monkeypatch):
    ember, client = local_ember(monkeypatch)
    gateway_reports(client)
    updates = []
    ember.start_listening(updates.append)
    assert ember.is_listening()
    client.on_message(client, None, local_message("upload/pointdata", {
        "mac": "rad", "pointData": base64.b64encode(
            bytes([0, 6, 4, 0, 215])).decode()}))
    radiators = ember.get_zone("rad")
    assert updates == [radiators]
    assert pe.zone_target_temperature(radiators) == 21.5
    client.on_message(client, None, local_message(
        "upload/thermostat", {"mac": ["rad"], "online": 0}))
    assert ember.is_zone_online("rad") is False
    # reports about the gateway itself or other kinds are ignored
    for message in (local_message("upload/pointdata", {
            "mac": "gw", "pointData": "AAIBRg=="}),
            local_message("upload/commdata", {"version": []}),
            SimpleNamespace(topic="P/U/upload/pointdata", payload=b"junk")):
        client.on_message(client, None, message)
    assert len(updates) == 2
    ember.stop_listening()
    assert not ember.is_listening()


def gateway_echoes(client):
    """Make the fake broker answer each command like the gateway does."""
    publish = client.publish

    def echo(topic, msg, qos):
        result = publish(topic, msg, qos)
        data = json.loads(msg)["data"]
        client.on_message(client, None, local_message(
            "upload/pointdata", data))
        return result

    client.publish = echo


def test_local_command_uses_open_connection(monkeypatch):
    ember, client = local_ember(monkeypatch)
    gateway_reports(client)
    gateway_echoes(client)
    assert ember.set_zone_target_temperature("rad", 20)
    topic, msg, qos = client.published[0]
    assert topic == "P/U/download/pointdata" and qos == 1
    assert msg["data"] == {"mac": "rad", "pointData": "AAYEAMg="}
    assert msg["common"]["userId"] is None
    assert client.connected  # not closed after the command
    assert pe.zone_target_temperature(ember.get_zone("rad")) == 20.0
    ember.close()
    assert not client.connected


def test_local_has_no_cloud_login(monkeypatch):
    ember, client = local_ember(monkeypatch)
    with pytest.raises(pe.EphEmberError):
        ember.messenging_credentials()


def test_local_keeps_cloud_zone_ids(monkeypatch):
    ember, client = local_ember(monkeypatch, zone_ids={"rad": "cloud-7"})
    gateway_reports(client)
    gateway_echoes(client)
    radiators = ember.get_zone("cloud-7")
    assert radiators["mac"] == "rad"
    assert ember.get_zone("hw")["zoneid"] == "hw"
    assert ember.set_zone_target_temperature("cloud-7", 20)
    assert client.published[0][1]["data"]["mac"] == "rad"


def test_local_command_not_confirmed(monkeypatch, caplog):
    ember, client = local_ember(monkeypatch)
    gateway_reports(client)
    ember.confirm_timeout = 0.05
    before = pe.zone_target_temperature(ember.get_zone("rad"))
    assert ember.set_zone_target_temperature("rad", 22) is False
    assert client.published  # sent, but the gateway never answered
    assert pe.zone_target_temperature(ember.get_zone("rad")) == before
    assert "did not confirm" in caplog.text
    assert ember._waiters == {}


def test_local_command_needs_connection(monkeypatch):
    ember, client = local_ember(monkeypatch)
    gateway_reports(client)
    client.connected = False
    assert ember.set_zone_target_temperature("rad", 22) is False
    assert client.published == []


def test_local_other_reports_do_not_confirm(monkeypatch):
    ember, client = local_ember(monkeypatch)
    gateway_reports(client)
    ember.confirm_timeout = 0.05
    publish = client.publish

    def other_point(topic, msg, qos):
        result = publish(topic, msg, qos)
        client.on_message(client, None, local_message("upload/pointdata", {
            "mac": "rad", "pointData": base64.b64encode(
                bytes([0, 5, 2, 0, 190])).decode()}))  # current temp only
        return result

    client.publish = other_point
    assert ember.set_zone_target_temperature("rad", 22) is False
