"""Tests for pyephember2 using recorded (anonymised) API data."""

import copy
import datetime
import json
from pathlib import Path

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
    return json.loads((FIXTURES / name).read_text())


def make_ember(monkeypatch, zone_program, homes=None):
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
    ember = EphEmber("user", "pass")
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
    assert pe.zone_command_to_ints({"deviceType": 2}, sent[0]) == [0, 7, 1, 2]


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
