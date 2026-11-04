# Connection diagnostics

For a controlled connection test, temporarily enable **INFO** for
`custom_components.flic_button.client` using Home Assistant's `logger.set_level`
action. No protocol-level DEBUG logging is needed. Restore the previous level
after the test (`notset` if it previously inherited the default).

When enabled, each connection attempt gets a read-only observer, stopped on
success, failure, or cancellation. Once per second it compares:

- `bleak_connected`: the transport's reported connection state.
- `bluez_connected`: the system's `Connected` property, read independently of
  Bleak's cache.
- `services_resolved`: the system's `ServicesResolved` property.

Only state changes are logged. Timeout/error messages include the last sample
and whether a link or resolved services were observed during the attempt.
Notifications and the connection-parameter request have separate stage labels.

`bluez_connected=True` with `services_resolved=False` is evidence that the link
exists but service discovery is incomplete. Resolved services with a still
pending connect call point to a later transport-initialization problem. These
are observations, not a definitive root-cause diagnosis. One-second sampling can
miss brief transitions; `link_seen=False` does not prove a link never existed.

BlueZ is queried only for the selected backend's path and the exact button
address. Remote proxies, non-Linux systems, unknown backend layouts, and failed
reads report unavailable/unknown state, not a disconnected device. Backend-path
inspection is best-effort against the pinned Bleak and HA wrapper layout.

The observer does not scan, connect, disconnect devices, modify adapter settings,
or log device details, pairing keys, or GATT payloads. Existing connection
timeouts and retry behavior are unchanged.


## Flic 2/Duo session recovery

The local client supplements pyflic-ble 0.2.6 with tracked signed ping responses and
recovery when the button terminates its logical session while the BLE transport
still reports connected. Event initialization must be acknowledged before the
client publishes availability. Control writes share a lock with ordinary
protocol writes and are cancelled when the transport is replaced or unloaded.
This includes upstream event acknowledgements and Twist position resends. Idle
disconnect is disabled by upstream; the former local packet rewrite is removed.
Event counters, replay filtering and acknowledgements use the new upstream handlers.
Twist keeps its upstream protocol handling.

For firmware version 7 or newer, startup also sets our pairing's reconnect
advertising to the Android SDK defaults: LE 1M PHY, 40 ms initially, then
1022.5 ms for 24 hours after a connection timeout. Other pairings' settings are
preserved. Acknowledged settings are reused within the same button boot;
a button reboot or a new HA client causes them to be applied again. Older or
unknown firmware skips this optional feature. A supported button that does not
acknowledge configuration is disconnected and retried.

The official Android SDK uses **25** for `SET_ADV_PARAMETERS_RESPONSE`, although
the older protocol wiki lists 27. The implementation follows the SDK. Sources:
[SDK packets](https://github.com/50ButtonsEach/flic2lib-android/blob/master/flic2lib-android/src/main/java/io/flic/flic2libandroid/Packets.java),
[SDK session setup](https://github.com/50ButtonsEach/flic2lib-android/blob/master/flic2lib-android/src/main/java/io/flic/flic2libandroid/Flic2Button.java),
and [Flic 2 protocol](https://github.com/50ButtonsEach/flic2-documentation/wiki/Flic-2-Protocol-Specification).
Incoming signature verification retains the pinned library's existing behavior.

### Device validation still required

1. Establish a session with existing pairing data and confirm a fresh click.
2. Restart HA normally and check reconnection and subsequent click events.
3. Separately test an unexpected connection loss, followed by recovery without
   pressing the button, to exercise the timeout advertising configuration.
4. Leave the button idle for several hours and confirm the next click arrives.

Advertising after a connection timeout is distinct from advertising after a
clean disconnect. These changes do not guarantee unattended reconnection after
every shutdown; if necessary, one short press can wake advertising. Re-pairing
should not be required when the stored pairing is still valid. Offline tests
cover the protocol and lifecycle but cannot establish radio or adapter behavior.
