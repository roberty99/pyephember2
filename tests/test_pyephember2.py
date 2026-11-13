"""Tests for pyephember2 using recorded (anonymised) API data."""

import copy
import datetime
import json
from pathlib import Path

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
        pass

    def username_pw_set(self, user, password):
        pass

    def connect(self, host, port):
        pass

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
