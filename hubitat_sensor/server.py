"""hubitat_sensor, one or more Hubitat device attributes as bold value
blocks.

Thin widget over hubitat_core. One ``get_devices`` call covers the whole
list, so the cell costs a single request no matter how many attributes
it shows (plus one cached event-log call per device for the trend and
sparkline). A single attribute renders as a hero number; several lay
out as a list (client-side).

Each selected entry is ``"<device id>:<attribute>"``, e.g. ``12:temperature``.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import current_app, has_app_context

# Event-log calls run in a small pool so a multi-device cell doesn't pay
# one hub round-trip per device in sequence. The per-call timeout stays
# inside the composer's per-widget hydration budget.
_HISTORY_TIMEOUT_S = 5
_HISTORY_MAX_WORKERS = 4

# Hubitat attribute name → Phosphor icon. Picked server-side so the
# client just renders ``ph-<icon>``. Everything else falls back to a
# capability-based guess, then a generic gauge.
_ATTRIBUTE_ICONS: dict[str, str] = {
    # Environmental
    "temperature": "thermometer",
    "humidity": "drop",
    "illuminance": "sun-dim",
    "ultravioletIndex": "sun",
    "pressure": "gauge",
    "carbonDioxide": "wind",
    "carbonMonoxide": "skull",
    "pm25": "circles-three-plus",
    "pm10": "circles-three-plus",
    "airQualityIndex": "wind",
    "smoke": "cloud",
    "water": "drop-half",
    "soilMoisture": "drop-half",
    # Power / electricity
    "battery": "battery-medium",
    "power": "lightning",
    "energy": "lightning",
    "voltage": "wave-sawtooth",
    "amperage": "wave-sine",
    "frequency": "wave-triangle",
    "powerSource": "plug",
    # Binary sensors
    "contact": "door",
    "motion": "person-simple-walk",
    "presence": "house",
    "acceleration": "vibrate",
    "shock": "warning",
    "tamper": "warning",
    "sound": "speaker-high",
    # Actuator state
    "switch": "toggle-right",
    "level": "sun-dim",
    "lock": "lock",
    "valve": "drop",
    "windowShade": "arrows-vertical",
    "position": "arrows-vertical",
    "door": "garage",
    "fanMode": "fan",
    "speed": "fan",
    # Climate
    "thermostatMode": "thermometer-simple",
    "thermostatOperatingState": "thermometer-simple",
    "coolingSetpoint": "snowflake",
    "heatingSetpoint": "fire",
    "thermostatSetpoint": "thermometer-simple",
    # Misc
    "rate": "drop",
    "steps": "footprints",
    "healthStatus": "heartbeat",
}

# Capability → icon fallback for attributes the table above doesn't name.
_CAPABILITY_ICONS: dict[str, str] = {
    "TemperatureMeasurement": "thermometer",
    "RelativeHumidityMeasurement": "drop",
    "IlluminanceMeasurement": "sun-dim",
    "ContactSensor": "door",
    "MotionSensor": "person-simple-walk",
    "PresenceSensor": "house",
    "Switch": "toggle-right",
    "SwitchLevel": "sun-dim",
    "Lock": "lock",
    "Battery": "battery-medium",
    "PowerMeter": "lightning",
    "EnergyMeter": "lightning",
    "Thermostat": "thermometer-simple",
    "WaterSensor": "drop-half",
    "SmokeDetector": "cloud",
    "Valve": "drop",
    "WindowShade": "arrows-vertical",
    "GarageDoorControl": "garage",
    "FanControl": "fan",
}

_UNAVAILABLE = {"", "null", "none", "unknown", "unavailable"}


_NUMBER_FORMAT = re.compile(r"0(?:\.(0+))?$")


def _apply_number_format(value: float, fmt: str) -> str | None:
    """Format ``value`` per a data-element number pattern: ``0`` /
    ``0.0`` / ``0.00`` fix the decimal places. Returns ``None`` when
    ``fmt`` isn't a recognised pattern, so the caller keeps its default
    formatting."""
    m = _NUMBER_FORMAT.fullmatch(fmt)
    if not m:
        return None
    decimals = len(m.group(1)) if m.group(1) else 0
    return f"{value:.{decimals}f}"


def _format_value(raw: str, fmt: str = "") -> str:
    """Format a numeric attribute value. With an explicit ``fmt`` the
    value is fixed to that many decimals; otherwise it's rounded to 2
    decimals with trailing zeros trimmed (72.00 → "72" / 68.40 →
    "68.4"). Non-numeric values pass through unchanged so a state like
    ``on`` or ``open`` keeps its spelling."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw
    if fmt:
        formatted = _apply_number_format(value, fmt)
        if formatted is not None:
            return formatted
    return f"{round(value, 2):g}"


def _core() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get("hubitat_core")
    return plugin.server_module if plugin is not None else None


def choices(name: str) -> list[dict[str, str]]:
    """Device-attribute multi-select for the editor."""
    core = _core()
    if name == "entity" and core is not None:
        return core.attribute_choices()
    return []


def _entity_list(raw: Any) -> list[str]:
    """Accept the multiselect's list, or a comma/newline string."""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [tok for tok in re.split(r"[\s,]+", str(raw or "").strip()) if tok]


def _split_key(key: str) -> tuple[str, str]:
    """``"12:temperature"`` → ``("12", "temperature")``. A bare device id
    with no attribute yields an empty attribute name."""
    device_id, _sep, attribute = key.partition(":")
    return device_id.strip(), attribute.strip()


def _parse_overrides(raw: Any) -> dict[str, dict[str, str]]:
    """Parse the ``overrides`` textarea into
    ``{"<id>:<attr>": {name?, icon?, format?}}``.

    Format: one entry per line, pipe-separated
    ``<id>:<attr> | name | icon | format``. Any field may be empty (leave
    nothing between the pipes) to keep the auto value. Lines starting with
    ``#`` are comments; blank lines are skipped."""
    out: dict[str, dict[str, str]] = {}
    text = str(raw or "")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if not parts or not parts[0]:
            continue
        entry: dict[str, str] = {}
        if len(parts) > 1 and parts[1]:
            entry["name"] = parts[1]
        if len(parts) > 2 and parts[2]:
            entry["icon"] = parts[2]
        if len(parts) > 3 and parts[3]:
            entry["format"] = parts[3]
        if entry:
            out[parts[0]] = entry
    return out


def _auto_name(label: str, attribute_words: str, rows_for_device: int) -> str:
    """Row name: the device label alone when it contributes one row, else
    label + attribute (``Lounge humidity``). When the attribute already
    starts with the label (``Thermostat`` + ``Thermostat mode``) the
    attribute alone is used so the name doesn't read doubled."""
    if rows_for_device <= 1:
        return label
    words = attribute_words.strip()
    if words.lower().startswith(label.strip().lower()):
        return words
    return f"{label} {words.lower()}"


def _icon_for(attribute: str, capabilities: list[str]) -> str:
    if attribute in _ATTRIBUTE_ICONS:
        return _ATTRIBUTE_ICONS[attribute]
    for cap in capabilities:
        if cap in _CAPABILITY_ICONS:
            return _CAPABILITY_ICONS[cap]
    return "gauge"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _downsample(values: list[float], cap: int = 24) -> list[float]:
    n = len(values)
    if n <= cap:
        return values
    step = (n - 1) / (cap - 1)
    return [values[round(i * step)] for i in range(cap)]


def _trend(values: list[float]) -> str:
    """up / down / flat based on the window's first sample → last
    sample, treating moves smaller than 5% of the range as flat so a
    noisy sensor doesn't show a "trend" that's just jitter."""
    if len(values) < 2:
        return "flat"
    delta = values[-1] - values[0]
    span = (max(values) - min(values)) or 1
    if abs(delta) < span * 0.05:
        return "flat"
    return "up" if delta > 0 else "down"


def _attach_history(
    core: Any,
    pending: list[tuple[dict[str, Any], str, str]],
    *,
    show_trend: bool,
    show_sparkline: bool,
) -> None:
    """Fetch the 24 h event log once per device (in parallel) and attach
    trend/sparkline to each (item, device_id, attribute) in place.
    hubitat_core caches the results, so only the first preview cycle in
    a couple of minutes pays the round-trips."""
    app = current_app._get_current_object() if has_app_context() else None

    def one(device_id: str) -> list[dict[str, Any]]:
        # Worker threads don't inherit the request's app context, but
        # hubitat_core reads its settings through current_app.
        try:
            if app is None:
                return core.events(device_id, hours=24, timeout=_HISTORY_TIMEOUT_S)
            with app.app_context():
                return core.events(device_id, hours=24, timeout=_HISTORY_TIMEOUT_S)
        except Exception:
            return []

    device_ids = sorted({device_id for _item, device_id, _attr in pending})
    if len(device_ids) == 1:
        results = [one(device_ids[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(_HISTORY_MAX_WORKERS, len(device_ids))) as pool:
            results = list(pool.map(one, device_ids))
    log_by_device = dict(zip(device_ids, results, strict=True))
    for item, device_id, attribute in pending:
        samples = [s for s in log_by_device.get(device_id, []) if s.get("name") == attribute]
        values = [v for v in (_to_float(s.get("value")) for s in samples) if v is not None]
        if len(values) >= 2:
            if show_trend:
                item["trend"] = _trend(values)
            if show_sparkline:
                item["sparkline"] = [round(v, 2) for v in _downsample(values, cap=24)]


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings, ctx
    core = _core()
    if core is None:
        return {"error": "Install the Hubitat Core plugin to use this widget."}

    wanted = _entity_list(options.get("entities"))
    title = (options.get("title") or "").strip()
    if not wanted:
        return {"empty": True, "title": title}

    try:
        devices = core.get_devices()
    except Exception as err:
        return {"error": core.coerce_error(err)}

    by_id = {str(d.get("id")): d for d in devices}
    show_unit = options.get("show_unit", True)
    show_trend = options.get("show_trend") is not False
    show_sparkline = options.get("show_sparkline") is not False
    number_format = str(options.get("number_format") or "").strip()
    overrides = _parse_overrides(options.get("overrides"))

    # A device that contributes several attributes gets the attribute
    # folded into each row's name so the rows stay distinguishable;
    # a device with one attribute is just named after the device.
    per_device: dict[str, int] = {}
    for key in wanted:
        device_id, _attr = _split_key(key)
        per_device[device_id] = per_device.get(device_id, 0) + 1

    items: list[dict[str, Any]] = []
    needs_history: list[tuple[dict[str, Any], str, str]] = []
    for key in wanted:
        device_id, attribute = _split_key(key)
        ov = overrides.get(key) or {}
        dev = by_id.get(device_id)
        if dev is None or not attribute:
            items.append(
                {
                    "name": ov.get("name") or key,
                    "value": "-",
                    "unit": "",
                    "icon": ov.get("icon") or "question",
                    "unavailable": True,
                }
            )
            continue
        label = core.device_label(dev)
        auto_name = _auto_name(label, core.humanize_attribute(attribute), per_device[device_id])
        raw_value = (dev.get("attributes") or {}).get(attribute)
        raw = "" if raw_value is None else str(raw_value)
        is_unavailable = raw.strip().lower() in _UNAVAILABLE
        item = {
            "name": ov.get("name") or auto_name,
            "value": "-"
            if is_unavailable
            else _format_value(raw, ov.get("format") or number_format),
            "unit": core.unit_for(attribute) if show_unit and not is_unavailable else "",
            "icon": ov.get("icon") or _icon_for(attribute, dev.get("capabilities") or []),
            "unavailable": is_unavailable,
        }
        # Trend + sparkline only for numeric, available attributes. Skip
        # the event-log call for text states or missing values.
        if (show_trend or show_sparkline) and not is_unavailable and _to_float(raw) is not None:
            needs_history.append((item, device_id, attribute))
        items.append(item)

    if needs_history:
        _attach_history(core, needs_history, show_trend=show_trend, show_sparkline=show_sparkline)

    # Title precedence: explicit override → the single row's name → generic.
    if not title:
        title = items[0]["name"] if len(items) == 1 else "Sensors"
    return {
        "title": title,
        "items": items,
        "show_title": options.get("show_title") is not False,
        "show_names": options.get("show_names") is not False,
    }
