# Flic Buttons over BLE (`flic_button`)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=michis0806&repository=ha_flic_buttons&category=integration)
[![Open your Home Assistant instance and start setting up this integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=flic_button)

Home Assistant custom integration that connects **Flic 2, Flic Duo and Flic Twist
buttons directly over Bluetooth LE** — no Flic Hub, no `flicd` daemon, no cloud.

[Deutsche Beschreibung weiter unten.](#deutsch)

## What this is

This is **not original work**. It packages the still-unmerged Home Assistant core
pull request [#182198](https://github.com/home-assistant/core/pull/182198) as a
custom integration so it can be used before it lands in a release, plus the fixes
listed in [CHANGELOG.md](CHANGELOG.md) — including a `SyntaxError` that prevents
the upstream branch from being imported at all.

The heavy lifting is done by [pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble),
the official BLE library from Shortcut Labs. Pairing is done at the application
layer (ECDH key exchange authenticated with Ed25519 signatures); the pairing ID and
key are stored in the config entry, so the button reconnects on its own after a
restart.

**When the upstream PR is merged, delete this integration and restart.** Core ships
the same domain `flic_button` and adopts the existing config entries, pairing keys
included. A custom component with the same domain permanently shadows the core one.

## What you get

One `event` entity per button, with these event types:

| Device | Events |
| --- | --- |
| Flic 2 | `up`, `down`, `click`, `double_click`, `hold` |
| Flic Duo | the above per button (big/small), plus `swipe_*` and `rotate_*` |
| Flic Twist | the above, plus `twist_increment`/`twist_decrement` and `push_twist_*`, or `rotate_*` and `selector_changed` in selector mode |

For the Twist, the mode is switchable in the integration options.

## Requirements

- Home Assistant **2026.9** or newer.
- A **connectable** Bluetooth adapter in range of the button: the host's own
  adapter, or an ESPHome Bluetooth proxy with `active: true`.

That second point is the one that bites. Flic 2 is connection-oriented — it does
not broadcast its events as advertisements. Proxies that only forward
advertisements are therefore useless for it, and that includes **every Shelly
device**: `aioshelly` registers its scanner with `can_connect=lambda: False`.

Each paired button also holds a BLE connection permanently. An ESP32 proxy offers
about three concurrent connections, so plan roughly three buttons per proxy, minus
whatever else connects through it.

## Installation

### HACS (custom repository)

Click the HACS badge above, or manually:

1. HACS → Integrations → ⋮ → *Custom repositories*
2. Add `https://github.com/michis0806/ha_flic_buttons` (category: Integration)
3. Install **Flic Buttons (BLE)** and restart Home Assistant.

### Manual

Copy `custom_components/flic_button/` into your `config/custom_components/` folder
and restart Home Assistant. The first start installs `pyflic-ble` and therefore
takes a little longer.

## Pairing

A button that is advertising shows up as a discovered device on its own. Otherwise:
Settings → Devices & services → *Add integration* → **Flic**, then push and hold the
button until it connects. The pairing window is 60 seconds.

A Flic can hold several pairings, so pairing with Home Assistant does not remove an
existing pairing with the Flic app or a hub.

If a button is factory reset or re-paired elsewhere, its stored credentials stop
working. Home Assistant then reports the entry as failed with a "pairing no longer
valid" message — remove the device and pair it again.

## Credits

- Upstream integration: [@joostlek](https://github.com/joostlek) and
  [50ButtonsEach](https://github.com/50ButtonsEach) in
  [home-assistant/core#182198](https://github.com/home-assistant/core/pull/182198)
- Library: [pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble)

Apache-2.0, inherited from Home Assistant core. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Disclaimer

Not affiliated with Shortcut Labs. Packaging of a pre-release branch — expect to
throw it away once the PR lands. Use at your own risk.

---

## Deutsch

Home-Assistant-Integration, die **Flic 2, Flic Duo und Flic Twist direkt per
Bluetooth LE** anbindet — ohne Flic Hub, ohne `flicd`, ohne Cloud.

Das ist **keine Eigenentwicklung**: Hier liegt der noch nicht gemergte Core-PR
[#182198](https://github.com/home-assistant/core/pull/182198) als Custom Integration,
damit er sich vor dem Release nutzen lässt — plus die Korrekturen aus
[CHANGELOG.md](CHANGELOG.md), darunter ein `SyntaxError`, mit dem sich der
Upstream-Branch gar nicht erst importieren lässt. Die eigentliche Arbeit macht
[pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble), die offizielle
BLE-Library von Shortcut Labs.

**Sobald der PR gemerged ist: Integration löschen und neu starten.** Der Core
bringt dieselbe Domain `flic_button` mit und übernimmt die vorhandenen Config
Entries samt Pairing-Keys. Ein gleichnamiges Custom Component verdeckt die
Core-Variante sonst dauerhaft.

### Voraussetzungen

- Home Assistant **2026.9** oder neuer.
- Ein **verbindungsfähiger** Bluetooth-Adapter in Reichweite: der Adapter des
  Hosts oder ein ESPHome-Proxy mit `active: true`.

Der zweite Punkt ist der entscheidende. Flic 2 ist verbindungsorientiert und sendet
seine Ereignisse nicht als Advertisements. Proxies, die nur Advertisements
weiterreichen, helfen deshalb nicht — und dazu gehören **alle Shelly-Geräte**:
`aioshelly` meldet seinen Scanner mit `can_connect=lambda: False` an.

Jeder gekoppelte Button hält außerdem dauerhaft eine BLE-Verbindung. Ein
ESP32-Proxy schafft etwa drei gleichzeitige Verbindungen — also grob drei Buttons
pro Proxy, abzüglich allem anderen, was darüber verbindet.

### Installation und Kopplung

Installation über HACS (Custom Repository) oder manuell nach
`config/custom_components/`, danach Neustart. Der erste Start dauert länger, weil
`pyflic-ble` nachinstalliert wird.

Ein sendender Button taucht von selbst als gefundenes Gerät auf. Sonst:
*Integration hinzufügen* → **Flic**, dann den Button gedrückt halten, bis er sich
verbindet. Das Kopplungsfenster beträgt 60 Sekunden.

Ein Flic kann mehrere Kopplungen speichern — die Kopplung mit der Flic-App oder
einem Hub bleibt also bestehen.

Wird ein Button zurückgesetzt oder anderswo neu gekoppelt, sind die gespeicherten
Zugangsdaten ungültig. Home Assistant meldet den Eintrag dann als fehlgeschlagen
("Kopplung nicht mehr gültig") — Gerät entfernen und neu koppeln.
