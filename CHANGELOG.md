# Changelog

All notable changes to this integration are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Task-backed pairing progress for fresh pairing advertisements, BLE connection
  and authentication, with explicit German/English instructions for each phase.
- Fresh-reception checks and Flic 2/Duo Public-mode service/connection-flag
  filtering; Twist retains model-specific advertisement handling.
- Regression coverage for stale advertisements, wrong devices, duplicate
  submissions, timeouts and cancellation between or during pairing stages.

### Fixed

- Clarify English/German Flic 2 pairing instructions: hold during the attempt,
  submit the pairing form before waiting for a connection, and confirm success
  in HA rather than from the LED alone. Remove the misleading ten-second promise
  from setup text and distinguish advertising from pairing mode.

## [1.0.0] - 2026-09-20

First stable release as an independently maintained custom integration.
Requires Home Assistant 2026.9 or newer. Hardware testing has covered Flic 2;
Duo and Twist support is inherited and has not been hardware-tested here.

### Added

- Diagnostic battery-voltage and last-advertisement RSSI sensors on each device.
  RSSI includes the receiver source and reads HA's cache without extra BLE I/O.
- Estimated Flic 2 battery percentage using the manufacturer's voltage curve.
  Existing voltage statistics and entity IDs remain unchanged.
- Portable offline regression tests for connection/pairing cleanup, diagnostics,
  battery conversion and event filtering.
- English/German documentation for diagnostics, pairing and event automations.

### Fixed

- Bounded BLE connection/service discovery, notification setup and cleanup via
  a local client subclass for pinned pyflic-ble 0.2.5.
- Prevent competing automatic reconnects during initial pairing, serialize
  connection attempts and cancel an active attempt when its config flow closes.
- Clean up clients after setup/authentication failures and reject duplicate
  pairing submissions.

### Changed

- Continue independent maintenance with project-specific fixes and features;
  this is not a temporary test distribution for the Home Assistant core PR.
- Stop publishing raw `up`/`down` events to HA. Flic 2 exposes only `click`,
  `double_click` and `hold`; Duo/Twist retain their additional gestures.
  Automations depending on raw press/release events must be updated.
- Document battery estimates and cached RSSI limitations, including that neither
  is guaranteed to refresh on every click.

## [0.1.1] - 2026-09-20

### Fixed

- Config flow: a failed or abandoned pairing left a `FlicClient` reconnecting to
  the button forever. The cleanup called `client.disconnect()`, which neither
  cancels the reconnect task nor sets the client's stopped flag, so
  `_handle_disconnected()` kept rescheduling the loop. Every retry added another
  zombie client; together they occupied the only connectable adapter, so each new
  pairing attempt failed with `org.bluez.Error.InProgress`, "failed to discover
  services" or "no connection slot", while the button still flashed green because
  a zombie client was acknowledging its presses. Cleanup now calls
  `client.stop()`.

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
