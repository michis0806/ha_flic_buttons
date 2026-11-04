# Changelog

All notable changes to this integration are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.2] - 2026-11-05

### Fixed

- Require pyflic-ble >= 0.2.6 instead of pinning the shared Core dependency.
  This satisfies Hassfest's shared-dependency validation and allows Home Assistant
  to select a compatible newer version. Offline regression tests remain pinned
  to 0.2.6 as the supported minimum.

## [1.1.1] - 2026-11-04

### Changed

- Upgrade the runtime and test dependency to pyflic-ble 0.2.6 for upstream
  event acknowledgements, reconnect event counters and old queued-event filtering.
- Remove the obsolete local idle-disconnect packet rewrite; upstream now sends
  the correct value 511. Retain bounded connection stages, reconnect advertising,
  strict event initialization and tracked session-control recovery.

### Fixed

- Include upstream acknowledgement and Twist position-write tasks in local
  transport cleanup, preventing pending writes from surviving a disconnect.
- Detect optional Duo initialization boot IDs from response length, matching
  upstream handling of inconsistent opcode labels.
- Add integration-level regressions for signed acknowledgements, event resumption,
  old-event filtering and cancellation. Radio/reboot validation remains pending.

## [1.1.0] - 2026-10-24

### Added

- Opt-in connection diagnostics for Bleak and BlueZ connection/service state,
  with bounded, read-only observations and setup instructions.

### Fixed

- Wake reconnect attempts from fresh Bluetooth cache receptions even when
  identical advertisements are deduplicated before callbacks.

- Configure Flic 2/Duo reconnect advertising using the official SDK's intervals
  and 24-hour window, for firmware version 7 or newer. Wait for response opcode
  25 and reapply after a button reboot or Home Assistant client recreation.
- Answer Flic 2/Duo ping requests with signed responses. Recover terminated
  logical sessions even if the BLE link stays connected, and cancel pending
  session-control work before replacing the transport or unloading.
- Require an acknowledged, complete event initialization before marking a
  session available; close and retry failed startup attempts.
- Add offline protocol/lifecycle regressions against pinned pyflic-ble 0.2.5.
  Hardware restart and long-running connection tests remain to be performed.

## [1.0.2] - 2026-09-20

### Fixed

- Disable automatic idle disconnection for Flic 2/Duo with the protocol value
  `511`, correcting `pyflic-ble` 0.2.5's init-event value `0` before packet signing.
  Preserve adjacent event-queue fields, pairing packets and Twist behavior.
- Add packet-level regressions for the real upstream handlers, authentication
  signatures and malformed layouts. A live smoke test with two Flic 2 buttons
  showed over 12 minutes of uninterrupted HA availability after the last clicks,
  with both BLE links still connected. Initial connection retries still occurred.

## [1.0.1] - 2026-09-20

### Added

- Task-backed pairing progress for fresh pairing advertisements, BLE connection
  and authentication, with explicit German/English instructions for each phase.
- Fresh-reception checks and Flic 2/Duo Public-mode service/connection-flag
  filtering; Twist retains model-specific advertisement handling.
- Regression coverage for stale advertisements, wrong devices, duplicate
  submissions, timeouts and cancellation between or during pairing stages.

### Fixed

- Load event/sensor platforms independently of button reachability at HA startup.
  Listen for connectable Bluetooth signals and reconnect in the background without
  requiring an integration reload. Update firmware metadata after connection.
- Close incomplete runtime sessions so an open BLE link cannot suppress retries
  after authentication/event initialization fails; await retry cancellation on
  unload to prevent overlapping clients. Cover offline startup, wake-up and
  cancellation with regression tests.
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
