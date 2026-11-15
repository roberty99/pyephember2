PyEphEmber2
========================================

PyEphEmber2 is a Python module implementing an interface to the [EPH Control Systems Ember API](http://emberapp.ephcontrols.com/).  It allows a user to interact with their EPH heating system for the purposes of monitoring their heating system. This requires you to 
have the EPH Gateway to provide external internet access for your heating system.

Credit goes to ttroy50 who developed pyephember. This version was created as ttroy50 is no longer available to maintain pyephember. 



Example basic usage
-------------------

    >>> from pyephember2.pyephember2 import EphEmber
    >>> e = EphEmber('my@username.com', 'mypassword')
    >>> zones = [z for home in e.get_zones() for z in home["zones"]]
    >>> zoneid = zones[0]["zoneid"]
    >>> e.get_zone_temperature(zoneid)
    >>> e.set_zone_target_temperature(zoneid, 20.5)

Schedules are stored in the home's local time. The API does not say which
time zone that is, so the computer's local time zone is assumed. To set it
explicitly:

    >>> from zoneinfo import ZoneInfo
    >>> e = EphEmber('my@username.com', 'mypassword', tz=ZoneInfo("Europe/Dublin"))

Live updates
------------

The Ember gateway reports every change (target, mode, temperatures, boiler
state) as it happens, whether it was made by this library, the Ember app or
the thermostat. `start_listening` keeps a connection open, updates the cached
zones and calls back for each report (from a background thread):

    >>> def changed(zone):
    ...     print(zone["name"], zone_target_temperature(zone))
    >>> e.start_listening(changed)
    >>> e.stop_listening()

API
---

The API is a basic HTTPS API returning data in JSON format. For more details see [here](API.md)

Disclaimer: I have no connection with EPH Controls so cannot guarentee that these API calls will always be valid.
