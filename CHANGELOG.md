# Changelog

All notable changes to this integration are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-20

Initial packaging of the upstream integration as a custom component.

### Added

- `flic_button` integration from [home-assistant/core#182198](https://github.com/home-assistant/core/pull/182198),
  branch `flic2`, commit `8308172e52d7f52db461bb6cd758372613819c3c`, on top of
  [pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble) 0.2.5.
- Event entities for Flic 2, Flic Duo and Flic Twist, paired and connected
  directly over BLE — no Flic Hub and no `flicd` service.
- German translation (`translations/de.json`).

### Fixed

- `config_flow.py`: `except TimeoutError, BleakError, FlicProtocolError:` is
  Python 2 syntax and raises a `SyntaxError` on import, which makes the upstream
  branch unusable as shipped. Parenthesized into a proper exception tuple.
- `__init__.py`: `FlicAuthenticationError` was not handled during setup.
  `FlicAuthenticationError` does not inherit from `FlicProtocolError`, so invalid
  pairing credentials (button factory reset or re-paired elsewhere) surfaced as an
  unhandled traceback. It is now reported as a `ConfigEntryError`, because
  retrying cannot fix it.

### Changed

- `manifest.json`: added the `version` key required for custom integrations,
  pointed `documentation`/`issue_tracker` at this repository, set `codeowners`
  to the fork maintainer and dropped `quality_scale` (Home Assistant forces
  `custom` for custom integrations anyway).
- `translations/en.json` generated from `strings.json` with all `[%key:…%]`
  references resolved — those are only expanded by the core build, not for
  custom integrations.
