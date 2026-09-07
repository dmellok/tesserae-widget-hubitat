# Hubitat widgets for Tesserae

Live device attributes from a [Hubitat Elevation](https://hubitat.com) hub on your e-ink panel, for [Tesserae](https://github.com/dmellok/tesserae), the e-ink dashboard companion.

Bundle of two plugins:

- **Hubitat Core** (`hubitat_core`), the shared hub connection. No cell of its own; the widgets read from it.
- **Hubitat, Sensor** (`hubitat_sensor`), one or more device attributes as bold value blocks. A single attribute is a hero number with a trend arrow and 24h sparkline; several become a list with per-row trend and hairline sparkline.

## Install

Settings → Widgets → Browse community widgets → Install. Restart when prompted.

## Set up the hub side

The bundle talks to Hubitat's built-in **Maker API** app over plain local HTTP. No cloud account and no extra software on the hub.

1. On the hub, open **Apps → Add Built-In App → Maker API**.
2. Tick the devices you want on the panel under *Select Devices*, then press *Done*.
3. Open the Maker API app again. It lists example URLs like
   `http://192.168.1.20/apps/api/12/devices?access_token=…`
4. In Tesserae, open **Settings → Widgets → Hubitat Core** and paste:
   - **Maker API base URL**: everything before `/devices`, e.g. `http://192.168.1.20/apps/api/12`. Pasting a whole example URL also works; the path suffix and token are split off.
   - **Access token**: the `access_token` value.
   - **Temperature unit**: `F` or `C`, matching *Settings → Hub Details → Temperature Scale* on the hub. Maker API sends values without units, so this is how the widget knows which to print.

If Tesserae runs away from the hub's network, the cloud form works too: `https://cloud.hubitat.com/api/<hub id>/apps/12` with the cloud access token from the same app page.

## Using the sensor widget

Drop a **Hubitat, Sensor** cell and tick attributes in the *Device attributes* list. Each entry is one attribute of one device (`Lounge · Temperature`, `Front Door · Contact`). Only devices exposed in the Maker API app appear; if the list shows a guidance line instead, the hub is not configured or not reachable.

Numeric attributes get a trend arrow and a 24h sparkline built from the hub's recent event log for that device. Text states (`open`, `on`, `active`) show as they are. Names, icons and number format can be overridden per attribute.

Icons follow the attribute name: temperature → thermometer, humidity → drop, contact → door, motion → walker, switch → toggle, battery → battery, and so on. Anything unrecognised falls back to a guess from the device's capabilities, then a gauge.

## Notes

- The hub keeps a fixed number of events per device, so a very chatty sensor's sparkline may cover less than 24 hours.
- Hubitat units aren't sent with values. Common attributes (humidity, battery, level → %, power → W, energy → kWh, illuminance → lx) are mapped by name; temperature-shaped attributes follow the temperature unit setting.
- Hubitat Core also exposes `send_command` and `device_choices` helpers for future widgets that act on devices (switches, dimmers, locks).

## License

AGPL-3.0-or-later. See [LICENSE](./LICENSE).
