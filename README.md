# Flic Buttons over BLE (`flic_button`)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=michis0806&repository=ha_flic_buttons&category=integration)
[![Open your Home Assistant instance and start setting up this integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=flic_button)

Home Assistant custom integration that connects **Flic 2, Flic Duo and Flic Twist
buttons directly over Bluetooth LE** — no Flic Hub, no `flicd` daemon, no cloud.

[Deutsche Beschreibung weiter unten.](#deutsch)

## What this is

This is an **independently maintained custom integration**, based on the work in
Home Assistant core pull request
[#182198](https://github.com/home-assistant/core/pull/182198). It has its own
releases, fixes and features, listed in [CHANGELOG.md](CHANGELOG.md), and is not
intended as a temporary test distribution of the core PR. Credit for the original
integration and protocol library remains with their authors.

The heavy lifting is done by [pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble),
the official BLE library from Shortcut Labs. Pairing is done at the application
layer (ECDH key exchange authenticated with Ed25519 signatures); the pairing ID and
key are stored in the config entry, so the button reconnects on its own after a
restart.

This README describes `main`; published releases may not contain every change yet.
If a future HA release includes `flic_button`, check its migration instructions
before removing this custom integration. A custom component overrides the same
core domain; automatic migration of all local features is not guaranteed.

## What you get

One `event` entity per button, with these event types:

| Device | Events |
| --- | --- |
| Flic 2 | `click`, `double_click`, `hold` |
| Flic Duo | the above per button (big/small), plus `swipe_*` and `rotate_*` |
| Flic Twist | the above, plus `twist_increment`/`twist_decrement` and `push_twist_*`, or `rotate_*` and `selector_changed` in selector mode |

For the Twist, the mode is switchable in the integration options.

Raw `up` and `down` events are deliberately not published to Home Assistant,
reducing entity history noise. Internal button processing is unchanged. Existing
automations listening for these raw events must use a supported type instead.

### Diagnostic sensors

| Sensor | Devices | Meaning and updates |
| --- | --- | --- |
| Battery (estimated), % | Flic 2 | Voltage-based estimate using the official Flic 2 SDK curve |
| Battery voltage, V | All supported models | Last voltage read at startup or reconnection |
| Signal strength (last reception), dBm | All supported models | Advertisement RSSI from HA's cache, checked every minute |

Battery data is **not requested on every click**. The client reads voltage when
establishing or re-establishing its session. The percentage is a rough estimate,
especially between 50 and 100%, according to the
[official Flic SDK](https://github.com/50ButtonsEach/flic2lib-android/blob/master/flic2lib-android/src/main/java/io/flic/flic2libandroid/BatteryLevel.java).
Its curve maps 2.10 V to 0%, 2.44 V to 6%, 2.74 V to 18%, 2.90 V to 42% and
3.00 V or higher to 100%. Voltage remains a separate sensor; this curve is not
applied to Duo or Twist without a verified model-specific conversion.

RSSI is **not live connection strength**. The `source` attribute identifies the
receiver selected by HA, which can be a passive Shelly rather than the connected
adapter. A strong Shelly RSSI does not prove the connectable adapter is in range.
A connected button may stop advertising, leaving the cached RSSI unchanged.
If a click wakes a disconnected button, advertising and reconnection may refresh
both measurements, but clicking alone does not guarantee an update.

The sensors create no extra BLE connections or scans. Missing measurements are
`unknown`, not zero. Known values remain visible during disconnections in the
running integration; they are not restored across HA restarts.

### Automations: single, double and hold

Separate press sensors are unnecessary. In the automation editor, add an
**Event received** trigger, select your Flic event entity and choose `click`
(single), `double_click` (double) or `hold` (long press).

Example trigger for a double press; replace the entity ID with your own:

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id: event.living_room_flic_button
    options:
      event_type:
        - double_click
```

Add your desired action in the editor. This handles repeated identical presses
and ignores `unknown`/`unavailable` transitions. Do not trigger only on changes
of the `event_type` attribute: consecutive single presses share the same type.
See [HA's Event received guide](https://www.home-assistant.io/triggers/event.received/).

## Requirements

- Home Assistant **2026.9** or newer.
- A **connectable** Bluetooth adapter in range of the button: the host's own
  adapter, or a compatible active Bluetooth proxy, such as ESPHome with
  `active: true` under `bluetooth_proxy` (see the configuration below).

That second point is the one that bites. Flic 2 is connection-oriented — it does
not broadcast its events as advertisements. Proxies that only forward
advertisements cannot deliver its clicks. Shelly Bluetooth forwarding can supply
discovery and RSSI, but not the active connection required by the button.

The integration maintains a BLE connection for each button. Connection slots are
shared with other BLE devices; capacity depends on the adapter/proxy configuration.

### Bluetooth hardware matters

**Reliable Bluetooth hardware is a prerequisite, especially with several Flics.**
An adapter being detected by HA, discovering a button or connecting one Flic does
not prove it can maintain all required connections. Reconnect logic cannot
compensate for an unreliable adapter, driver or radio link.

- Choose the exact model/chip revision from Home Assistant's
  [known working high-performance adapters](https://www.home-assistant.io/integrations/bluetooth/#known-working-high-performance-adapters)
  and check its [unsupported list](https://www.home-assistant.io/integrations/bluetooth/#unsupported-adapters).
  A higher Bluetooth version number alone is not a compatibility guarantee.
- Position the adapter near the buttons. Use a short USB extension to move a USB
  adapter away from the host and interference, or place active proxies closer to
  the devices. Passive receivers do not add connection capacity.
- Budget **one persistent connection slot per Flic**, plus capacity for other BLE
  clients. ESP32 ESPHome proxies default to three slots; one such proxy alone
  cannot maintain four Flic sessions. Add another reachable active proxy if
  needed. See [ESPHome connection limits](https://esphome.io/components/bluetooth_proxy/#how-active-connections-work).

For ESPHome, active connection support is configured as:

```yaml
bluetooth_proxy:
  active: true
```

This is **not** the BLE tracker's active scanning setting. Receiving advertisements
and forwarding active GATT connections are separate capabilities.

In one tested setup, an ATS2851 USB adapter (`10d7:b012`) repeatedly failed to
maintain a fourth Flic connection while three worked. This is an observation about
that adapter/stack, **not a universal three-connection hardware limit**. Adding
active proxies enabled all four buttons to be available simultaneously; long-term
stability still needs verification.

If adding a button appears to disconnect another, check connection capacity,
the actual connected adapter and its range before resetting pairings. Compare
HA's connection diagnostics with the proxy's own counters; they may disagree.
Verify fresh clicks from every button, not just discovery or cached RSSI.

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

### After a Home Assistant restart

Already paired buttons reconnect in the background. An unreachable button does
not block integration setup; its event entity stays unavailable until the session
is ready. A fresh signal received by a **connectable** Bluetooth adapter wakes the
retry loop, so reloading the integration should not be necessary. If needed,
press the button briefly near that adapter; do not enter pairing mode again.
Battery values are read after the session connects; RSSI alone does not prove a
working connection. Verify a subsequent click before relying on automations.

### Flic 2: hold while pairing

1. Keep the button close to the **connectable adapter**, not just a Shelly receiver.
   Disconnect any phone or hub currently holding its Bluetooth connection.
2. Settings → Devices & services: configure the discovered Flic, or choose
   *Add integration* → **Flic**.
3. If a pairing form is shown, **submit it to start listening**, then press and
   **keep holding** the physical button (previously paired Flic 2: **at least six
   seconds**). Manual discovery can start this automatically without another form.
4. Follow the status in HA: **waiting for a fresh pairing signal → Bluetooth
   connection being established → Bluetooth connected, authentication in progress**.
   These are actual setup phases, not a countdown or a detected physical press.
5. As a practical setup procedure, keep holding through the attempt until
   **Home Assistant confirms successful setup**, then release. If HA reports an
   error, release the button and start a fresh attempt; do not hold indefinitely.
6. Press once briefly and verify that HA receives a `click` event.

The Flic 2 Public-mode service UUID is the pairing signal. Setup waits for a new
reception from a connectable receiver rather than trusting an old discovery entry;
it also rejects the manufacturer's "already connected" flag when available.
This signals a pairing attempt is possible, not that authentication will succeed.
Twist uses its own service and has generic advertisement/connection status text.
Setup scanning is bounded and stopped on success, timeout or cancellation.
See the [Flic protocol's advertising format](https://github.com/50ButtonsEach/flic2-documentation/wiki/Flic-2-Protocol-Specification#advertising).

The manufacturer specifies a long press to enter public mode, not a requirement
to hold until orange flashing stops. New pairings are accepted for **up to 30
seconds after entering public mode**; do not assume continued holding extends
this window. This differs from the integration's connection timeout. See the
[official Flic 2 overview](https://github.com/50ButtonsEach/flic2-documentation/wiki/Technical-Overview-and-Terminology).

Orange/yellow flashing indicates advertising without a connection; red can occur
when no pairing is stored. **Flashing stopping by itself is not proof of pairing.**
Green on a press indicates a Bluetooth connection,
**not necessarily successful HA authentication**. Use the setup result and actual
click events to confirm success.

A Flic can hold several pairings, so pairing with Home Assistant does not remove an
existing pairing with the Flic app or a hub.

Factory resets invalidate stored pairings; adding another app does not necessarily
do so. If credentials really become invalid, remove the failed entry and pair
again. Do not factory-reset merely to wake a button.

### Troubleshooting

- Discovery through a passive receiver does not prove the active adapter can
  reach the button. Check adapter placement first if connection attempts time out.
- Avoid parallel pairing attempts. The local client suppresses automatic
  reconnects during pairing and cleans up failed/cancelled attempts; normal
  runtime reconnection is retained.
- If other BLE clients are active, a temporary isolation test may help. Restore
  those clients afterwards; a successful isolated test alone is not proof of cause.
- After a restart, try one short press near the adapter before deleting any
  pairing. Code updates need a restart, not a new pairing.
- The local workaround has bounded connection/service-discovery and notification
  timeouts, but cannot fix unreachable or incompatible hardware.

## Development and tests

Offline tests use the real pinned Flic library with HA and BLE I/O test doubles;
they never connect to physical buttons.

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest -q tests
```

Hardware verification has covered a Flic 2 on HA 2026.9.3: pairing, reconnection,
press events and diagnostics. This does not imply equivalent hardware testing of
Duo/Twist or every adapter.

To publish a release, update the manifest version and the dated changelog entry,
then push the matching `vX.Y.Z` tag. The release workflow runs HACS, Hassfest and
the offline tests before publishing a GitHub release with the changelog notes.

## Credits

- Upstream integration: [@joostlek](https://github.com/joostlek) and
  [50ButtonsEach](https://github.com/50ButtonsEach) in
  [home-assistant/core#182198](https://github.com/home-assistant/core/pull/182198)
- Library: [pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble)

Apache-2.0, inherited from Home Assistant core. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
The adapted connection code includes the license shipped with pyflic-ble 0.2.5
as `custom_components/flic_button/LICENSE.pyflic-ble`.

## Disclaimer

Not affiliated with Shortcut Labs. Based on upstream integration work with local
fixes and additions. Use at your own risk.

---

## Deutsch

Home-Assistant-Integration, die **Flic 2, Flic Duo und Flic Twist direkt per
Bluetooth LE** anbindet — ohne Flic Hub, ohne `flicd`, ohne Cloud.

Dies ist eine **eigenständig gepflegte Custom Integration** auf Basis des Core-PRs
[#182198](https://github.com/home-assistant/core/pull/182198), mit eigenen Releases,
Korrekturen und Funktionen aus [CHANGELOG.md](CHANGELOG.md). Sie ist nicht als
vorübergehende Testversion des Core-PRs gedacht. Die ursprüngliche Integration
und die Protokollbibliothek stammen weiterhin von ihren jeweiligen Autoren.
Die Bluetooth-Kommunikation übernimmt
[pyflic-ble](https://github.com/50ButtonsEach/pyflic-ble), die offizielle
BLE-Library von Shortcut Labs.

Diese README beschreibt `main`; veröffentlichte Versionen können davon abweichen.
Falls HA später eine eigene `flic_button`-Integration ausliefert, zuerst deren
Migrationshinweise prüfen. Die Custom Integration verdeckt eine gleichnamige
Core-Integration; die automatische Übernahme aller Zusatzfunktionen ist nicht
zugesichert.

### Ereignisse und Sensoren

Flic 2 veröffentlicht nur **`click` (einfach), `double_click` (doppelt) und `hold`
(halten)**. `up`/`down` erscheinen nicht mehr als HA-Ereignisse; intern bleibt die
Verarbeitung erhalten. Alte Automationen mit `up`/`down` müssen angepasst werden.
Duo und Twist behalten ihre zusätzlichen Wisch-/Dreh- und Modusfunktionen.

Am selben Gerät erscheinen:

- **Batterie (geschätzt)** in %, nur für Flic 2, nach der offiziellen Flic-Kurve:
  ab 3,00 V = 100%, bei 2,90 V = 42%, bei 2,10 V = 0%. Besonders zwischen 50 und
  100% ist diese Schätzung ungenau.
- **Batteriespannung** in Volt. Sie wird beim Start und bei Wiederverbindung
  abgefragt, **nicht bei jedem Klick**. Die Spannungsentität bleibt erhalten.
- **Signalstärke (letzter Empfang)** in dBm, aus dem HA-Empfangscache einmal pro
  Minute gelesen. Dafür werden keine zusätzlichen BLE-Verbindungen aufgebaut.

Der RSSI-Wert kann von einem passiven Shelly stammen; die Kennung steht im
Attribut `source`. Er ist kein Live-Wert der aktiven Verbindung und kann länger
unverändert bleiben. Ein Klick kann indirekt neue Messwerte bringen, wenn er den
Button aufweckt und eine Wiederverbindung auslöst, garantiert das aber nicht.
Fehlende Werte sind „unbekannt“. Bekannte Werte bleiben bei Verbindungsabbruch
sichtbar, werden aber nicht über HA-Neustarts hinweg gespeichert.

### Automationen

Im Automationseditor **Auslöser hinzufügen → Flic-Event-Entität auswählen →
Ereignis empfangen** und `click`, `double_click` oder `hold` auswählen. Separate
Druck-Sensoren sind nicht nötig. Das YAML-Beispiel oben zeigt einen Doppelklick.

Nicht ausschließlich auf eine Änderung des Attributs `event_type` reagieren:
Zwei Einfachklicks hintereinander haben denselben Typ. „Ereignis empfangen“
verarbeitet beide und ignoriert reine „unbekannt“-/„nicht verfügbar“-Übergänge.

### Voraussetzungen

- Home Assistant **2026.9** oder neuer.
- Ein **verbindungsfähiger** Bluetooth-Adapter in Reichweite: der Adapter des
  Hosts oder ein kompatibler aktiver Bluetooth-Proxy, beispielsweise ESPHome mit
  `active: true` unter `bluetooth_proxy`.

Der zweite Punkt ist der entscheidende. Flic 2 ist verbindungsorientiert und sendet
seine Ereignisse nicht als Advertisements. Proxies, die nur Advertisements
weiterreichen, können daher keine Tastenklicks liefern. Shelly-Geräte können
Erkennung und RSSI beisteuern, aber keine aktive Flic-Verbindung herstellen.

Die Integration hält eine BLE-Verbindung pro Button. Die verfügbaren Plätze
werden mit anderen BLE-Geräten geteilt; ihre Anzahl hängt vom Adapter/Proxy ab.

#### Die Bluetooth-Hardware ist entscheidend

**Zuverlässige Bluetooth-Hardware ist eine Voraussetzung, besonders bei mehreren
Flics.** Dass HA den Adapter erkennt, Buttons findet oder einen einzelnen Flic
verbindet, beweist noch keinen stabilen Betrieb mit allen Geräten. Automatische
Wiederverbindung kann einen unzuverlässigen Adapter, Treiber oder Funkweg nicht
ausgleichen.

- Die genaue Modell-/Chipvariante anhand der
  [HA-Liste bewährter High-Performance-Adapter](https://www.home-assistant.io/integrations/bluetooth/#known-working-high-performance-adapters)
  auswählen und die [nicht unterstützten Adapter](https://www.home-assistant.io/integrations/bluetooth/#unsupported-adapters)
  beachten. Eine höhere Bluetooth-Versionsnummer allein ist kein Gütesiegel.
- Adapter nahe den Buttons platzieren. Eine kurze USB-Verlängerung schafft Abstand
  zum Rechner und Störquellen; aktive Proxys können näher an den Geräten stehen.
  Passive Empfänger schaffen keine zusätzlichen Verbindungsplätze.
- **Pro Flic einen dauerhaft belegten Verbindungsplatz** einplanen, zusätzlich zu
  anderen BLE-Geräten. ESP32-ESPHome-Proxys haben standardmäßig drei Plätze. Ein
  solcher Proxy allein kann daher nicht vier Flic-Verbindungen halten. Bei Bedarf
  einen weiteren erreichbaren aktiven Proxy ergänzen. Siehe die
  [ESPHome-Verbindungsgrenzen](https://esphome.io/components/bluetooth_proxy/#how-active-connections-work).

Bei ESPHome muss `active: true` unter **`bluetooth_proxy`** stehen; das
YAML-Beispiel im englischen Abschnitt zeigt die Konfiguration. Aktives Scannen
im BLE-Tracker ist etwas anderes und ermöglicht allein keine GATT-Verbindungen.

In einem getesteten Aufbau scheiterte ein ATS2851-USB-Adapter (`10d7:b012`)
wiederholt an einer vierten Flic-Verbindung, während drei funktionierten. Das ist
eine Beobachtung zu diesem Adapter/Software-Stack, **keine allgemeine
Drei-Verbindungen-Grenze des Chips**. Mit zusätzlichen aktiven Proxys waren alle
vier Buttons gleichzeitig verfügbar; die Langzeitstabilität ist noch zu prüfen.

Scheint ein neuer Button einen anderen zu verdrängen, zuerst Kapazität, den
tatsächlich verbundenen Adapter und dessen Reichweite prüfen, nicht sofort die
Kopplungen zurücksetzen. HA-Verbindungsdiagnose und Proxy-eigene Zähler können
voneinander abweichen. Frische Klicks aller Buttons prüfen; Erkennung und
zwischengespeicherter RSSI allein genügen nicht.

### Installation und Kopplung

Installation über HACS (Custom Repository) oder manuell nach
`config/custom_components/`, danach Neustart. Der erste Start dauert länger, weil
`pyflic-ble` nachinstalliert wird.

#### Nach einem Home-Assistant-Neustart

Nach einem HA-Neustart verbinden sich bereits gekoppelte Buttons im Hintergrund.
Ein nicht erreichbarer Button blockiert die Einrichtung nicht; seine Event-Entität
bleibt bis zur erfolgreichen Verbindung „nicht verfügbar“. Ein frisches Signal am
**verbindungsfähigen** Adapter weckt den Wiederholungsversuch. Bei Bedarf dort
einmal kurz drücken – weder neu koppeln noch die Integration neu laden.
Batteriewerte werden nach dem Verbindungsaufbau gelesen; RSSI allein beweist keine
funktionierende Verbindung. Anschließend einen weiteren Klick prüfen.

#### Flic 2: während des Koppelns gedrückt halten

1. Button nahe an den **verbindungsfähigen Adapter** legen. Eine bestehende
   Bluetooth-Verbindung zu einem Hub oder Smartphone vorher trennen.
2. Den gefundenen Flic konfigurieren oder *Integration hinzufügen* → **Flic**.
3. Falls das Kopplungsformular erscheint, **absenden, um die Suche zu starten**.
   Dann den physischen Button drücken und **gedrückt halten** (bereits gekoppelter
   Flic 2: **mindestens sechs Sekunden**). Bei manueller Suche kann dies ohne ein
   weiteres Formular automatisch starten.
4. Den Status in HA verfolgen: **Warten auf frisches Kopplungssignal →
   Bluetooth-Verbindung wird aufgebaut → Bluetooth verbunden, Kopplung wird
   geprüft**. Das sind tatsächliche Phasen, kein Countdown und keine direkte
   Erkennung des Tastendrucks.
5. Als praktische Vorgehensweise den Button während des Versuchs weiter halten,
   bis **Home Assistant die erfolgreiche Einrichtung bestätigt**, dann loslassen.
   Meldet HA einen Fehler, loslassen und einen neuen Versuch starten – nicht
   unbegrenzt weiter gedrückt halten.
6. Anschließend einmal kurz drücken und prüfen, ob HA ein `click`-Event empfängt.

Das Kopplungssignal ist die vom Flic 2 im Public-Modus gesendete Service-Kennung.
Die Einrichtung wartet auf einen frischen Empfang über einen verbindungsfähigen
Empfänger; ein alter Erkennungseintrag genügt nicht. Meldet der Button zusätzlich
„bereits verbunden“, wird das Signal nicht als bereit für die Verbindung gewertet.
Eine erfolgreiche Authentifizierung ist damit noch nicht garantiert. Für Twist
gelten eigene, allgemeinere Statusmeldungen. Der Setup-Scan ist zeitlich begrenzt
und wird bei Erfolg, Zeitüberschreitung oder Abbruch beendet.

Der Hersteller beschreibt den langen Druck zum Aktivieren des Kopplungsmodus,
nicht „halten, bis Orange ausgeht“ als notwendiges Kriterium. Dieser Modus bleibt
ab seiner Aktivierung bis zu **30 Sekunden** offen; weiteres Gedrückthalten ist
keine zugesicherte Verlängerung. Die Zeitlimits der Integration sind davon
unabhängig. Siehe die [offizielle Flic-2-Dokumentation](https://github.com/50ButtonsEach/flic2-documentation/wiki/Technical-Overview-and-Terminology).

Orange/gelb bedeutet Senden ohne Verbindung; Rot kann bei fehlenden Kopplungen
auftreten. **Dass das Blinken endet, beweist allein keine erfolgreiche Kopplung.**
Grün beim Klick zeigt eine Bluetooth-Verbindung, aber noch keine erfolgreiche
HA-Authentifizierung. Maßgeblich sind die HA-Erfolgsmeldung und empfangene Events.

Ein Flic kann mehrere Kopplungen speichern — die Kopplung mit der Flic-App oder
einem Hub bleibt also bestehen.

Ein Werksreset löscht Kopplungen; eine zusätzliche App-Kopplung tut das nicht
zwangsläufig. Ein bereits verbundener Hub oder ein Smartphone muss seine aktive
Verbindung für die Einrichtung freigeben.

Bei Aussetzern zuerst Reichweite des aktiven Adapters und konkurrierende
BLE-Verbindungen prüfen. Ein guter Shelly-RSSI beweist keine gute Verbindung zum
aktiven Adapter. Nach HA-Neustart gegebenenfalls kurz drücken, nicht sofort neu
koppeln oder zurücksetzen. Die lokale Verbindungskorrektur bereinigt fehlerhafte
Versuche und unterbindet parallele Reconnects beim Koppeln; Hardware- und
Reichweitenprobleme bleiben trotzdem möglich.
