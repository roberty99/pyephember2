PyEphEmber2
========================================

PyEphEmber2 is a Python module implementing an interface to the [EPH Control Systems Ember API](http://emberapp.ephcontrols.com/).  It allows a user to interact with their EPH heating system for the purposes of monitoring their heating system. This requires you to 
have the EPH Gateway to provide external internet access for your heating system.

Credit goes to ttroy50 who developed pyephember. This version was created as ttroy50 is no longer available to maintain pyephember. 



Example basic usage
-------------------

    >>> from pyephember2.pyephember2 import EphEmber
    >>> e = EphEmber('my@username.com', 'mypassword')
    >>> e.get_zone_temperature("MyZone")

API
---

The API is a basic HTTPS API returning data in JSON format. For more details see [here](API.md)

Disclaimer: I have no connection with EPH Controls so cannot guarentee that these API calls will always be valid.
