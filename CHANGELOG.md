# Changelog

## Unreleased

- Commands (set temperature, mode, boost, advance) now wait for the Ember
  MQTT broker to accept the connection before sending, and always
  disconnect afterwards. Before, every command left an open TLS connection
  behind, and a rejected token failed silently. Connection failures and
  refusals raise `EphEmberError`.
- After a command is sent, the cached zone is updated with the new values
  and the cache is kept for `command_settle_seconds` (default 30), so
  callers see the change at once instead of the old values the Ember cloud
  keeps reporting for several seconds.
- Schedules are compared with the time in the home's time zone instead of
  UTC (they were an hour out during summer time). Pass `tz=` to `EphEmber`
  (e.g. `zoneinfo.ZoneInfo("Europe/Dublin")`); by default the computer's
  local time zone is used.
- New `zone_is_online(zone)` / `EphEmber.is_zone_online(zoneid)`, from the
  zone's `isonline` field.
- Temperatures are rounded to the nearest tenth before sending (20.1 was
  sent as 20.0).
- `boost_timestamp()` and `is_target_temperature_reached()` return `None`
  instead of raising when the values are missing.

## 0.4.13

- Fix boost, boost temperature and advance commands failing with
  "missing 1 required positional argument: 'index'" (thanks @UtzR, #2).
  `ZoneCommand`'s `index` is now optional.
- Fix crash reading schedules with slots at midnight or before 01:00,
  e.g. unused slots returned as `startTime: 0` (thanks @mchugh19, #1).
- Network failures now raise `EphEmberError` (a `RuntimeError`) instead of
  raw `requests` exceptions, so callers that catch `RuntimeError` handle
  the Ember cloud being unreachable.
- Log in again automatically when the refresh token is rejected, instead
  of failing with "Unable to login" until restarted.
- Missing temperature points return `None` instead of raising `TypeError`.
- `get_zone_names()` returns zone names (it previously failed).
- Unknown point indices raise `ValueError` instead of silently returning `None`.
- Replace deprecated `datetime.utcnow()`.
- Add tests (recorded EMBER-PS data) and run them on GitHub Actions.
