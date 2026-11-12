# Changelog

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
