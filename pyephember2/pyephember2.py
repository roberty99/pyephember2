"""
PyEphEmber interface implementation for https://ember.ephcontrols.com/
"""
# pylint: disable=consider-using-f-string

import base64
import datetime
import json
import time
import collections

from enum import Enum
from typing import OrderedDict

import requests
import paho.mqtt.client as mqtt


class ZoneMode(Enum):
    """
    Modes that a zone can be set too
    """

    # pylint: disable=invalid-name
    AUTO = 0
    ALL_DAY = 1
    ON = 2
    OFF = 3


def GetPointIndex(zone, pointIndex) -> int:
    assert isinstance(pointIndex, PointIndex)
    match pointIndex:
        case PointIndex.ADVANCE_ACTIVE:
            return 4
        case PointIndex.CURRENT_TEMP:
            return 5
        case PointIndex.TARGET_TEMP:
            match zone["deviceType"]:
                case 773:
                    return 12
                case _:
                    return 6
        case PointIndex.MODE:
            match zone['deviceType']:
                case 514 | 773:
                    return 11
                case 2 | 4:
                    return 7
                case _:
                    return 7
        case PointIndex.BOOST_HOURS:
            match zone["deviceType"]:
                case 514 | 773:
                    # Returns 0 if boost is OFF and 1 if ON
                    return 13
                case _:
                    return 8
        case PointIndex.BOOST_TIME:
            return 9
        case PointIndex.BOILER_STATE:
            return 10
        case PointIndex.BOOST_TEMP:
            return 14
        case PointIndex.CTR_15_ABAB:
            return 15
        case PointIndex.XXX_16_0000:
            return 16
        case PointIndex.CTR_17_ABAB:
            return 17
        case PointIndex.CTR_18_0AB7:
            return 18
        case _:
            RuntimeError('Unknown PointIndex:' + pointIndex)


class PointIndex(Enum):
    """
    Point indices for pointData returned by API
    """

    ADVANCE_ACTIVE = 4
    CURRENT_TEMP = 5
    TARGET_TEMP = 6
    MODE = 7
    BOOST_HOURS = 8
    BOOST_TIME = 9
    BOILER_STATE = 10
    BOOST_TEMP = 14
    CTR_15_ABAB = 15
    XXX_16_0000 = 16
    CTR_17_ABAB = 17
    CTR_18_0AB7 = 18


# """
# Named tuple to hold a command to write data to a zone
# """
ZoneCommand = collections.namedtuple('ZoneCommand', ['name', 'value', 'index'])


def zone_command_to_ints(zone, command):
    """
    Convert a ZoneCommand to an array of integers to send
    """
    type_data = {
        'SMALL_INT': {'id': 1, 'byte_len': 1},
        'TEMP_RO': {'id': 2, 'byte_len': 2},
        'TEMP_RW': {'id': 4, 'byte_len': 2},
        'TIMESTAMP': {'id': 5, 'byte_len': 4}
    }
    writable_command_types = {
        'ADVANCE_ACTIVE': 'SMALL_INT',
        'TARGET_TEMP': 'TEMP_RW',
        'MODE': 'SMALL_INT',
        'BOOST_HOURS': 'SMALL_INT',
        'BOOST_TIME': 'TIMESTAMP',
        'BOOST_TEMP': 'TEMP_RW'
    }
    if command.name not in writable_command_types:
        raise ValueError(
            "Cannot write to read-only value "
            "{}".format(command.name)
        )
    
    command_type = writable_command_types[command.name]

    if command.index is not None:
        command_index = command.index
    else:
        command_index = GetPointIndex(zone, PointIndex[command.name])

    # command header: [0, index, type_id]
    int_array = [0, command_index, type_data[command_type]['id']]

    # now encode and append the value
    send_value = command.value
    if command_type == 'TEMP_RW':
        # The thermostat uses tenths of a degree;
        # send_value is given in degrees, so we convert.
        send_value = int(10*send_value)
    elif command_type == 'TIMESTAMP':
        # send_value can be either an int representing a Unix timestamp,
        # or a datetime. Convert if a datetime.
        if isinstance(command.value, datetime.datetime):
            send_value = int(command.value.timestamp())

    for byte_value in send_value.to_bytes(
            type_data[command_type]['byte_len'], 'big'):
        int_array.append(int(byte_value))

    return int_array


def zone_is_active(zone):
    """
    Check if the zone is on.
    This is a bit of a hack as the new API doesn't have a currently
    active variable
    """
    if zone_is_scheduled_on(zone):
        return True
    # not sure how reliable the next tests are
    return zone_boost_hours(zone) > 0 or zone_advance_active(zone)


def zone_advance_active(zone):
    """
    Check if zone has advance active
    """
    match zone["deviceType"]:
        case 773:
            # Mode not supported
            return False
        case 514:
            # Need to fix, point index or value is not right
            return False
        case _:
            return zone_pointdata_value(zone, PointIndex.ADVANCE_ACTIVE) != 0


def boiler_state(zone):
    """
    Return the boiler state for a zone, as given by the API
    Probable interpretation:
    0 => FIXME, 1 => flame off, 2 => flame on
    """
    return zone_pointdata_value(zone, PointIndex.BOILER_STATE)

def lastKey(dict):
    return list(dict.keys())[-1]


def firstKey(dict):
    return list(dict.keys())[0]


def try_parse_int(value):
    try:
        return int(value), True
    except ValueError:
        return None, False

def scheduletime_to_time(dict, key_name):
    """
    Convert a schedule start/end time (an integer) to a Python time
    For example, x = 173 is converted to 17:30
    """
    if dict.get(key_name) is None:
        return None
    stime = dict[key_name]
    if stime is None:
        return None
    return datetime.time(int(str(stime)[:-1]), 10 * int(str(stime)[-1:]))

def getZoneTime(zone):
    tstamp = time.gmtime(zone["timestamp"] / 1000)
    ts_time = datetime.time(tstamp.tm_hour, tstamp.tm_min)
    ts_wday = tstamp.tm_wday + 1
    if ts_wday == 7:
        ts_wday = 0
    return [ts_time, ts_wday]


def zone_get_running_day(zone):
    todaysDay = zone["days"][getZoneTime(zone)[1]]
    return todaysDay

def zone_get_running_program(zone):
    mode = zone_mode(zone)
    ts_time = getZoneTime(zone)[0]

    todaysDay = zone_get_running_day(zone)
    if todaysDay is None:
        return None

    if mode == ZoneMode.AUTO:
        for key in todaysDay["programs"]:
            program = todaysDay["programs"][key]
            start_time = scheduletime_to_time(program, "startTime")
            end_time = scheduletime_to_time(program, "endTime")
            p_time = scheduletime_to_time(program, "time")
            if (
                start_time is not None
                and end_time is not None
                and start_time <= ts_time <= end_time
            ):
                return program
            elif p_time is not None and p_time >= ts_time:
                # some devices using different programm logic
                # P1 contains only activation time and target temp, need to find currently running program by searching previous programm.
                # Ex: Today is Day 2 9:00am, P1 in that day starts at 10am, current programm is last P from Day 1
                runningProgram = program["Prev"]
                return [runningProgram, program]
        # program not found in that day
        # last program active
        lastProg = todaysDay["programs"][lastKey(todaysDay["programs"])]

        if lastProg.get("time") is None:
            return lastProg
        else:
            return [lastProg, lastProg["Next"]]

    elif mode == ZoneMode.ALL_DAY:
        startProgram = todaysDay["programs"][firstKey(todaysDay["programs"])]
        endProgram = todaysDay["programs"][lastKey(todaysDay["programs"])]
        return [startProgram, endProgram]

    return None

def zone_is_scheduled_on(zone):
    """
    Check if zone is scheduled to be on
    """
    mode = zone_mode(zone)
    if mode == ZoneMode.OFF:
        return False

    if mode == ZoneMode.ON:
        return True

    ts_time = getZoneTime(zone)[0]

    if mode == ZoneMode.AUTO:
        runningPrograms = zone_get_running_program(zone)
        if runningPrograms is None:
            return False
        elif type(runningPrograms) is list:
            # some devices using different programm logic
            # P1 contains only activation time and target temp, need to find currently running program by searching previous programm.
            # Ex: Today is Day 2 9:00am, P1 in that day starts at 10am, current programm is last P from Day 1
            currentTemp = zone_current_temperature(zone)
            targetTemp = runningPrograms[0]["temperature"] / 10

            # Current program found, check if current temp ( minus offset 0.3->0.7 deg after temp was reached) < target temp
            # NB! Some devices like eTrv have settings to adjust turn on/off temperature offcet (not available in Ember app).
            if currentTemp + 0.3 < targetTemp:
                return True
            else:
                return False
        else:
            start_time = scheduletime_to_time(runningPrograms, "startTime")
            end_time = scheduletime_to_time(runningPrograms, "endTime")
            if (
                start_time is not None
                and end_time is not None
                and start_time <= ts_time <= end_time
            ):
                return True

    elif mode == ZoneMode.ALL_DAY:
        runningPrograms = zone_get_running_program(zone)
        first_start_time = scheduletime_to_time(runningPrograms[0], "startTime")
        last_end_time = scheduletime_to_time(runningPrograms[1], "endTime")
        if first_start_time is None or last_end_time is None:
            return False
        if first_start_time <= ts_time <= last_end_time:
            return True

    return False


def zone_name(zone):
    """
    Get zone name
    """
    return zone["name"]


def zone_is_boost_active(zone):
    """
    Is the boost active for the zone
    """
    return zone_boost_hours(zone) > 0


def zone_boost_hours(zone):
    """
    Return zone boost hours
    """
    return zone_pointdata_value(zone, PointIndex.BOOST_HOURS)


def zone_boost_timestamp(zone):
    """
    Return zone boost hours
    """
    return zone_pointdata_value(zone, PointIndex.BOOST_TIME)


def zone_temperature(zone, label):
    """
    Return temperature (float) from the PointIndex value for label (str)
    """
    if zone["deviceType"] == 773:
        # in auto mode need to find program target temp.
        if zone_mode(zone) == ZoneMode.AUTO and label == PointIndex.TARGET_TEMP:
            programs = zone_get_running_program(zone)
            if programs is not None:
                return programs[0]["temperature"] / 10
            else:
                return None
        else:
            return zone_pointdata_value(zone, PointIndex(label)) / 10
    else:
        return zone_pointdata_value(zone, PointIndex(label)) / 10

def zone_target_temperature(zone):
    """
    Get target temperature for this zone
    """
    return zone_temperature(zone, PointIndex.TARGET_TEMP)

def zone_boost_temperature(zone):
    """
    Get target temperature for this zone
    """
    return zone_temperature(zone, PointIndex.BOOST_TEMP)


def zone_current_temperature(zone):
    """
    Get current temperature for this zone
    """
    return zone_temperature(zone, PointIndex.CURRENT_TEMP)


def zone_pointdata_value(zone, pointIndex):
    """
    Get value of given index for this zone, as an integer
    index can be either an integer index, or a string label
    from the PointIndex enum: 'ADVANCE_ACTIVE', 'CURRENT_TEMP', etc
    """
    # pylint: disable=unsubscriptable-object
    index = GetPointIndex(zone, pointIndex)

    for datum in zone['pointDataList']:
        if datum['pointIndex'] == index:
            return int(datum['value'])

    return None


def zone_mode(zone):
    """
    Get mode for this zone
    Default settings based on next known devices
    deviceTypes 2 | 4:
    AUTO = 0
    ALL_DAY = 1
    ON = 2
    OFF = 3

    deviceTypes 773:
    AUTO = 0
    ON/Manual = 1
    BOOST = 0 ? Could be another point index
    OFF = 4

    deviceTypes 514:
    AUTO = 0
    ADVANCE = 0 ? Could be another point index
    ALL_DAY = 9
    ON/Manual = 10
    BOOST = 0 ? Could be another point index
    OFF = 4
    """

    modeValue = zone_pointdata_value(zone, PointIndex.MODE)
    match modeValue:
        case 0:
            return ZoneMode.AUTO
        case 1 | 9:
            match zone["deviceType"]:
                case 773:
                    return ZoneMode.ON
                case _:
                    return ZoneMode.ALL_DAY
        case 2 | 10:
            return ZoneMode.ON
        case 3 | 4:
            return ZoneMode.OFF

def get_zone_mode_value(zone, mode) -> int:
    if mode == ZoneMode.AUTO:
        return 0

    match zone['deviceType']:
        case 773:
            match mode:
                case ZoneMode.ON:
                    return 1
                case ZoneMode.OFF:
                    return 4
        case 514:
            match mode:
                case ZoneMode.ALL_DAY:
                    return 9
                case ZoneMode.ON:
                    return 10
                case ZoneMode.OFF:
                    return 4
        case _:
            match mode:
                case ZoneMode.ALL_DAY:
                    return 1
                case ZoneMode.ON:
                    return 2
                case ZoneMode.OFF:
                    return 3


class EphMessenger:
    """
    MQTT interface to the EphEmber API
    """

    def _zone_command_b64(self, zone, cmd, stop_mqtt=True, timeout=1):
        """
        Send a base64-encoded MQTT command to a zone
        Returns true if the command was published within the timeout
        """
        product_id = zone["productId"]
        uid = zone["uid"]

        msg = json.dumps(
            {
                "common": {
                    "serial": 7870,
                    "productId": product_id,
                    "uid": uid,
                    "timestamp": str(int(1000*time.time()))
                },
                "data": {
                    "mac": zone['mac'],
                    "pointData": cmd
                }
            }
        )

        started_locally = False
        if not self.client or not self.client.is_connected():
            started_locally = True
            self.start()

        pub = self.client.publish(
            "/".join([product_id, uid, "download/pointdata"]), msg, 0
        )
        pub.wait_for_publish(timeout=timeout)

        if started_locally and stop_mqtt:
            self.stop()

        return pub.is_published()

    # Public interface

    def start(self, callbacks=None, loop_start=False):
        """
        Start MQTT client
        """
        credentials = self.parent.messenging_credentials()
        self.client_id = '{}_{}'.format(
            credentials['user_id'], str(int(1000*time.time()))
        )
        token = credentials['token']

        mclient = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, self.client_id)
        mclient.tls_set()
        self.client = mclient

        user_name = "app/{}".format(token)
        mclient.username_pw_set(user_name, token)

        if callbacks is not None:
            for key in callbacks.keys():
                setattr(mclient, key, callbacks[key])

        mclient.connect(self.api_url, self.api_port)

        if loop_start:
            mclient.loop_start()

        return mclient

    def stop(self):
        """
        Disconnect MQTT client if connected
        """
        if not self.client:
            return False
        if self.client.is_connected():
            self.client.disconnect()
        return True

    def send_zone_commands(self, zone, commands, stop_mqtt=True, timeout=1):
        """
        Bundles the given array of ZoneCommand objects
        to a single MQTT command and sends to the named zone.

        If a single ZoneCommand is given, send just that.

        Returns true if the bundled command was published within the timeout.

        For example, to set target temperature to 19:

          send_zone_command("Zone_name", ZoneCommand('TARGET_TEMP', 19))

        """
        def ints_to_b64_cmd(int_array):
            """
            Convert an array of integers to a byte array and
            return its base64 string in ascii
            """
            return base64.b64encode(bytes(int_array)).decode("ascii")

        if isinstance(commands, ZoneCommand):
            commands = [commands]

        ints_cmd = [x for cmd in commands for x in zone_command_to_ints(zone, cmd)]

        return self._zone_command_b64(
            zone, ints_to_b64_cmd(ints_cmd), stop_mqtt, timeout
        )

    def __init__(self, parent):
        self.api_url = 'eu-base-mqtt.topband-cloud.com'
        self.api_port = 18883

        self.client = None
        self.client_id = None

        self.parent = parent


class EphEmber:
    """
    Interacts with a EphEmber thermostat via API.
    Example usage: t = EphEmber('me@somewhere.com', 'mypasswd')
                   t.get_zone_temperature('myzone') # Get temperature
    """

    # pylint: disable=too-many-public-methods

    def _http(self, endpoint, *, method=requests.post, headers=None,
              send_token=False, data=None, timeout=10):
        """
        Send a request to the http API endpoint
        method should be requests.get or requests.post
        """
        if not headers:
            headers = {}

        if send_token:
            if not self._do_auth():
                raise RuntimeError("Unable to login")
            headers["Authorization"] = self._login_data["data"]["token"]

        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        url = "{}{}".format(self.http_api_base, endpoint)

        if data and isinstance(data, dict):
            data = json.dumps(data)

        response = method(url, data=data, headers=headers, timeout=timeout)

        if response.status_code != 200:
            raise RuntimeError(
                "{} response code".format(response.status_code)
            )

        return response

    def _requires_refresh_token(self):
        """
        Check if a refresh of the token is needed
        """
        expires_on = self._login_data["last_refresh"] + \
            datetime.timedelta(seconds=self._refresh_token_validity_seconds)
        refresh = datetime.datetime.utcnow() + datetime.timedelta(seconds=30)
        return expires_on < refresh

    def _request_token(self, force=False):
        """
        Request a new auth token
        """
        if self._login_data is None:
            raise RuntimeError("Don't have a token to refresh")

        if not force:
            if not self._requires_refresh_token():
                # no need to refresh as token is valid
                return True

        response = self._http(
            "appLogin/refreshAccessToken",
            method=requests.get,
            headers={'Authorization':
                     self._login_data['data']['refresh_token']}
        )

        refresh_data = response.json()

        if 'token' not in refresh_data.get('data', {}):
            return False

        self._login_data['data'] = refresh_data['data']
        self._login_data['last_refresh'] = datetime.datetime.utcnow()

        return True

    def _login(self):
        """
        Login using username / password and get the first auth token
        """
        self._login_data = None

        response = self._http(
            "appLogin/login",
            data={
                'userName': self._user['username'],
                'password': self._user['password']
            }
        )

        self._login_data = response.json()
        if self._login_data['status'] != 0:
            self._login_data = None
            return False
        self._login_data["last_refresh"] = datetime.datetime.utcnow()

        if ('data' in self._login_data
                and 'token' in self._login_data['data']):
            return True

        self._login_data = None
        return False

    def _do_auth(self):
        """
        Do authentication to the system (if required)
        """
        if self._login_data is None:
            return self._login()

        return self._request_token()

    def _get_user_details(self):
        """
        Get user details [user/selectUser]
        """
        response = self._http(
            "user/selectUser", method=requests.get,
            send_token=True
        )
        user_details = response.json()
        if user_details['status'] != 0:
            return {}
        return user_details

    def _get_user_id(self, force=False):
        """
        Get user ID
        """
        if not force and self._user['user_id']:
            return self._user['user_id']

        user_details = self._get_user_details()
        data = user_details.get('data', {})
        if 'id' not in data:
            raise RuntimeError("Cannot get user ID")
        self._user['user_id'] = str(data['id'])
        return self._user['user_id']

    def _get_first_gateway_id(self):
        """
        Get the first gatewayid associated with the account
        """
        if not self._homes:
            raise RuntimeError("Cannot get gateway id from list of homes.")
        return self._homes[0]['gatewayid']

    def _set_zone_target_temperature(self, zone, target_temperature):
        return self.messenger.send_zone_commands(
            zone,
            ZoneCommand('TARGET_TEMP', target_temperature, GetPointIndex(zone, PointIndex.TARGET_TEMP))
        )

    def _set_zone_boost_temperature(self, zone, target_temperature):
        return self.messenger.send_zone_commands(
            zone,
            ZoneCommand('BOOST_TEMP', target_temperature)
        )

    def _set_zone_advance(self, zone, advance=True):
        if advance:
            advance = 1
        else:
            advance = 0
        return self.messenger.send_zone_commands(
            zone,
            ZoneCommand('ADVANCE_ACTIVE', advance)
        )

    def _set_zone_boost(self, zone, boost_temperature, num_hours, timestamp=0):
        """
        Internal method to set zone boost

        num_hours should be 0, 1, 2 or 3

        If boost_temperature is not None, send that

        If timestamp is 0 (or omitted), use current timestamp

        If timestamp is None, do not send timestamp at all.
        (maybe results in permanent boost?)
        """
        cmds = [ZoneCommand('BOOST_HOURS', num_hours)]
        if boost_temperature is not None:
            cmds.append(ZoneCommand('BOOST_TEMP', boost_temperature))
        if timestamp is not None:
            if timestamp == 0:
                timestamp = int(datetime.datetime.now().timestamp())
            cmds.append(ZoneCommand('BOOST_TIME', timestamp))
        return self.messenger.send_zone_commands(zone, cmds)

    def _set_zone_mode(self, zone, mode_num, index):
        return self.messenger.send_zone_commands(
            zone, ZoneCommand('MODE', mode_num, index)
        )

    # Public interface

    def messenging_credentials(self):
        """
        Credentials required by EphMessenger
        """
        if not self._do_auth():
            raise RuntimeError("Unable to login")

        return {
            'user_id': self._get_user_id(),
            'token': self._login_data["data"]["token"]
        }

    def list_homes(self):
        """
        List the homes available for this user
        """
        response = self._http(
            "homes/list", method=requests.get, send_token=True
        )
        homes = response.json()
        status = homes.get('status', 1)
        if status != 0:
            raise RuntimeError("Error getting home: {}".format(status))

        return homes.get("data", [])

    def get_home_details(self, gateway_id=None, force=False):
        """
        Get the details about a home (API call: homes/detail)
        If no gateway_id is passed, the first gateway found is used.
        """
        if self._home_details and not force:
            return self._home_details

        if gateway_id is None:
            if not self._homes:
                self._homes = self.list_homes()
            gateway_id = self._get_first_gateway_id()

        response = self._http(
            "homes/detail", send_token=True,
            data={"gateWayId": gateway_id}
        )

        home_details = response.json()

        status = home_details.get('status', 1)
        if status != 0:
            raise RuntimeError(
                "Error getting details from home: {}".format(status))

        if "data" not in home_details or "homes" not in home_details["data"]:
            raise RuntimeError(
                "Error getting details from home: no home data found")

        self._home_details = home_details['data']

        return home_details["data"]

    def lastKey(dict):
        return list(dict.keys())[-1]

    def firstKey(dict):
        return list(dict.keys())[0]
    
    # ["homes"]
    def get_homes(self):
        """
        Get the data about a home (API call: homesVT/zoneProgram).
        """

        if (
            self.NextHomeUpdateDaytime is None
            or datetime.datetime.now() > self.NextHomeUpdateDaytime
        ):
            self._homes = self.list_homes()
        else:
            return self._homes

        for home in self._homes:
            home["zones"] = []
            gateway_id = home["gatewayid"]

            response = self._http(
                "homesVT/zoneProgram", send_token=True, data={"gateWayId": gateway_id}
            )

            homezones = response.json()

            status = homezones.get("status", 1)
            if status != 0:
                raise RuntimeError("Error getting zones from home: {}".format(status))

            if "data" not in homezones:
                raise RuntimeError("Error getting zones from home: no data found")
            if "timestamp" not in homezones:
                raise RuntimeError("Error getting zones from home: no timestamp found")

            for zone in homezones["data"]:
                # build programs
                zone["days"] = {}
                prevProgramm = None
                for day in sorted(
                    zone["deviceDays"], key=lambda x: x["dayType"], reverse=False
                ):
                    day["programs"] = {}
                    keys = day.keys()
                    for key in keys:
                        if key.startswith("p"):
                            tryGetId = try_parse_int(key[1:])
                            if tryGetId[1]:
                                programm = day[key]
                                if programm is not None:
                                    if prevProgramm is not None:
                                        programm["Prev"] = prevProgramm
                                    programm["Count"] = tryGetId[0]
                                    prevProgramm = programm
                                    day["programs"][tryGetId[0]] = programm
                    zone["days"][day["dayType"]] = day
                # reverse loop to connect all Prev programs
                lastProgramm = None
                firstProgramm = None
                for day in OrderedDict(sorted(zone["days"].items(), reverse=True)):
                    if lastProgramm is not None:
                        firstProgramm = zone["days"][day]["programs"][
                            lastKey(zone["days"][day]["programs"])
                        ]
                        lastProgramm["Prev"] = firstProgramm
                    lastProgramm = zone["days"][day]["programs"][
                        firstKey(zone["days"][day]["programs"])
                    ]

                lastProgramm["Prev"] = firstProgramm

                firstDayPrograms = zone["days"][firstKey(zone["days"])]["programs"]
                firstProgram = firstDayPrograms[firstKey(firstDayPrograms)]
                nextProgram = firstProgram
                for day in OrderedDict(sorted(zone["days"].items(), reverse=True)):
                    orderedProgs = OrderedDict(
                        sorted(zone["days"][day]["programs"].items(), reverse=True)
                    )
                    for progNum in orderedProgs:
                        program = zone["days"][day]["programs"][progNum]
                        program["Next"] = nextProgram
                        nextProgram = program

                zone["timestamp"] = homezones["timestamp"]
                home["zones"].append(zone)

        self.NextHomeUpdateDaytime = datetime.datetime.now() + datetime.timedelta(
            seconds=10
        )
        return self._homes

    def get_zones(self):
        """
        Get all zones
        """
        home_data = self.get_homes()
        if not home_data:
            return []

        return home_data

    def get_zone_names(self):
        """
        Get the name of all zones
        """
        zone_names = []
        for zone in self.get_zones():
            zone_names.append(zone['name'])

        return zone_names

    def get_zone(self, zoneid):
        """
        Get the information about a particular zone
        """
        for home in self.get_zones():
            for zone in home['zones']:
                if zoneid == zone['zoneid']:
                    return zone

        raise RuntimeError("Unknown zone: %s" % zoneid)

    def is_zone_active(self, zoneid):
        """
        Check if a zone is active
        """
        zone = self.get_zone(zoneid)
        return zone_is_active(zone)

    def is_zone_boiler_on(self, zoneid):
        """
        Check if the named zone's boiler is on and burning fuel (experimental)
        """
        zone = self.get_zone(zoneid)
        return boiler_state(zone) == 2

    def get_zone_temperature(self, zoneid):
        """
        Get the temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_current_temperature(zone)

    def get_zone_target_temperature(self, zoneid):
        """
        Get the temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_target_temperature(zone)

    def get_zone_boost_temperature(self, zoneid):
        """
        Get the boost target temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_boost_temperature(zone)

    def is_boost_active(self, zoneid):
        """
        Check if boost is active for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_is_boost_active(zone)

    def boost_hours(self, zoneid):
        """
        Get the boost duration for a zone, in hours
        """
        zone = self.get_zone(zoneid)
        return zone_boost_hours(zone)

    def boost_timestamp(self, zoneid):
        """
        Get the timestamp recorded for the boost
        """
        zone = self.get_zone(zoneid)
        return datetime.datetime.fromtimestamp(zone_boost_timestamp(zone))

    def is_target_temperature_reached(self, zoneid):
        """
        Check if a zone temperature has reached the target temperature
        """
        zone = self.get_zone(zoneid)
        return zone_current_temperature(zone) >= zone_target_temperature(zone)

    def set_zone_target_temperature(self, zoneid, target_temperature):
        """
        Set the target temperature for a named zone
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_target_temperature(
            zone, target_temperature
        )

    def set_zone_boost_temperature(self, zoneid, target_temperature):
        """
        Set the boost target temperature for a named zone
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_boost_temperature(
            zone, target_temperature
        )

    def set_zone_advance(self, zoneid, advance_state=True):
        """
        Set the advance state for a named zone
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_advance(
            zone, advance_state
        )

    def activate_zone_boost(self, zoneid, boost_temperature=None,
                            num_hours=1, timestamp=0):
        """
        Turn on boost for a named zone

        If boost_temperature is not None, send that

        If timestamp is 0 (or omitted), use current timestamp

        If timestamp is None, do not send timestamp at all.
        (maybe results in permanent boost?)

        """
        return self._set_zone_boost(
            self.get_zone(zoneid), boost_temperature,
            num_hours, timestamp=timestamp
        )

    def deactivate_zone_boost(self, zone):
        """
        Turn off boost for a named zone
        """
        return self.activate_zone_boost(zone, num_hours=0, timestamp=None)

    def set_zone_mode(self, zoneid, mode):
        """
        Set the mode by using the name of the zone
        Supported zones are available in the enum ZoneMode
        """

        assert isinstance(mode, ZoneMode)

        zone = self.get_zone(zoneid)
        modevalue = get_zone_mode_value(zone, mode)
        modeindex = GetPointIndex(zone, PointIndex.MODE)

        return self._set_zone_mode(
            self.get_zone(zoneid), modevalue, modeindex
        )

    def get_zone_mode(self, zoneid):
        """
        Get the mode for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_mode(zone)

    def reset_login(self):
        """
        reset the login data to force a re-login
        """
        self._login_data = None

    # Ctor
    def __init__(self, username, password, cache_home=False):
        """Performs login and save session cookie."""

        if cache_home:
            raise RuntimeError("cache_home not implemented")

        self._login_data = None
        self._user = {
            'user_id': None,
            'username': username,
            'password': password
        }

        # This is the list of homes / gateways associated with the account.
        self._homes = None

        self._home_details = None

        self.NextHomeUpdateDaytime = None

        self._refresh_token_validity_seconds = 1800

        self.http_api_base = 'https://eu-https.topband-cloud.com/ember-back/'

        self.messenger = EphMessenger(self)

        if not self._login():
            raise RuntimeError("Unable to login.")