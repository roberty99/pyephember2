#!/usr/bin/env python3
"""
Report what an EPH Ember system tells the cloud, to help add support for it.

This only reads: it logs in, lists your homes and reads their zones. It
never changes anything on your heating. Your email, password, names, ids
and addresses are not printed, so the report is safe to post in a
GitHub issue.

Needs only Python 3.8 or newer, nothing to install:

    python3 ember_report.py

It asks for your EMBER app email and password (the password isn't shown
as you type).
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://eu-https.topband-cloud.com/ember-back/"

# Zone fields worth showing from the older zones/polling answer
POLLING_FIELDS = (
    "deviceType", "systemType", "ishotwater", "isonline", "mode",
    "currenttemperature", "targettemperature", "isboostactive",
    "isadvanceactive", "prefix",
)
SCHEDULE_FIELDS = ("startTime", "endTime", "time", "temperature",
                   "starttime", "endtime")


def call(path, data=None, token=None):
    """Return (http status, decoded JSON or None)."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    if token:
        headers["Authorization"] = token
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(
        API + path, data=body, headers=headers,
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, OSError, ValueError) as err:
        sys.exit("Cannot reach the EPH Ember cloud: {}".format(err))


def schedule(day):
    """A day's programmes without their ids."""
    return {
        key: {k: v for k, v in value.items() if k in SCHEDULE_FIELDS}
        for key, value in day.items()
        if key.startswith("p") and isinstance(value, dict)
    }


def zone_program_report(zones):
    report = []
    for number, zone in enumerate(zones, 1):
        days = sorted(zone.get("deviceDays") or [],
                      key=lambda day: day.get("dayType", 0))
        report.append({
            "zone": number,
            "deviceType": zone.get("deviceType"),
            "systemType": zone.get("systemType"),
            "isonline": zone.get("isonline"),
            "points": {
                str(point["pointIndex"]): point["value"]
                for point in zone.get("pointDataList") or []
            },
            "sunday_schedule": schedule(days[0]) if days else None,
        })
    return report


def polling_report(zones):
    report = []
    for number, zone in enumerate(zones, 1):
        entry = {"zone": number, "fields": sorted(zone)}
        entry.update({k: zone.get(k) for k in POLLING_FIELDS if k in zone})
        programmes = zone.get("programmes") or {}
        if isinstance(programmes.get("sunday"), dict):
            entry["sunday_schedule"] = schedule(programmes["sunday"])
        report.append(entry)
    return report


def main():
    email = os.environ.get("EMBER_EMAIL") or input("EMBER app email: ")
    password = (os.environ.get("EMBER_PASSWORD")
                or getpass.getpass("EMBER app password: "))

    status, login = call("appLogin/login",
                         {"userName": email, "password": password})
    if not login or login.get("status") != 0:
        sys.exit("Login failed (HTTP {}, status {}). Check the email and "
                 "password work in the EMBER app.".format(
                     status, login and login.get("status")))
    token = login["data"]["token"]

    status, homes = call("homes/list", token=token)
    if not homes or homes.get("status") != 0:
        sys.exit("Could not list homes (HTTP {}).".format(status))

    report = {"report": "pyephember2 ember_report 1", "homes": []}
    for number, home in enumerate(homes.get("data") or [], 1):
        gateway = {"gateWayId": home["gatewayid"]}
        entry = {
            "home": number,
            "deviceType": home.get("deviceType"),
            "sysTemType": home.get("sysTemType"),
            "zoneCount": home.get("zoneCount"),
            "has_productId": bool(home.get("productId")),
            "has_uid": bool(home.get("uid")),
        }
        for endpoint, describe in (
                ("homesVT/zoneProgram", zone_program_report),
                ("zones/polling", polling_report)):
            status, answer = call(endpoint, gateway, token)
            result = {"http": status}
            if answer is not None:
                result["status"] = answer.get("status")
                result["message"] = answer.get("message")
                if answer.get("status") == 0 and answer.get("data"):
                    result["zones"] = describe(answer["data"])
            entry[endpoint] = result
        report["homes"].append(entry)

    print()
    print("Copy everything between the lines into the GitHub issue:")
    print("-" * 60)
    print("```json")
    print(json.dumps(report, indent=1))
    print("```")
    print("-" * 60)


if __name__ == "__main__":
    main()
