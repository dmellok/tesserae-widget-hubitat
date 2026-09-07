"""hubitat_core, shared Hubitat Maker API connection + REST helpers.

No widget cell of its own; sibling hubitat_* widgets reach in via the
plugin registry and call ``get_devices`` / ``get_device`` / ``events`` /
``send_command`` / ``attribute_choices`` so all of them share one Maker
API URL, token, and TLS policy. Tesserae renders headlessly on push /
schedule, so a plain REST poll at render time fits; the hub's event
socket isn't needed.

Config lives in Settings → Widgets → Hubitat Core: the Maker API base
URL and access token. Both appear on the Maker API app page on the hub
(Apps → Maker API) as example URLs such as
``http://192.168.1.20/apps/api/12/devices?access_token=…``. The base
URL is everything before ``/devices``; pasting a whole example URL
works too, the path suffix and the token are split off.

Maker API shapes this module normalises (they differ per endpoint):

* ``GET /devices/all`` lists every exposed device with a flat
  ``attributes`` object (``{"temperature": "72.5", ...}``) and plain
  string ``capabilities`` / ``commands`` lists.
* ``GET /devices/<id>`` returns ``attributes`` as a list of
  ``{name, currentValue, dataType}`` and ``commands`` as either strings
  or ``{"command": ...}`` objects depending on hub firmware.
* ``GET /devices/<id>/events`` is the recent event log, newest first:
  ``{name, value, date, unit?, ...}``.
* ``GET /devices/<id>/<command>[/<value>]`` sends a command.

Every function that talks to the hub raises on missing config or HTTP
errors so each widget decides how to surface them (see ``coerce_error``).
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from flask import current_app

USER_AGENT = "tesserae/0.1 (+hubitat_core)"

# Path suffixes the Maker API app page lists in its example URLs. When
# the operator pastes a whole example, everything from the first of
# these onward is dropped to recover the base URL.
_EXAMPLE_SUFFIXES = ("/devices", "/modes", "/hsm", "/location", "/postURL")


def _settings() -> dict[str, Any]:
    store = current_app.config["SETTINGS_STORE"]
    section = store.get_section("plugins") or {}
    return section.get("hubitat_core") or {}


def parse_api_url(raw: Any) -> tuple[str, str]:
    """Split a pasted Maker API URL into ``(base_url, token)``.

    Accepts the bare base (``http://hub/apps/api/12``), the same with a
    trailing slash, or a full example URL copied off the Maker API page
    (``http://hub/apps/api/12/devices/all?access_token=abc``). The
    returned base has no trailing slash and no query; the token is the
    ``access_token`` query value when present, else ``""``."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text.rstrip("/"), ""
    token = ""
    if parts.query:
        values = parse_qs(parts.query).get("access_token") or []
        token = (values[0] if values else "").strip()
    path = parts.path or ""
    cut = len(path)
    for suffix in _EXAMPLE_SUFFIXES:
        idx = path.find(suffix)
        if idx != -1 and idx < cut:
            cut = idx
    path = path[:cut].rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), token


def api_url() -> str:
    """Configured Maker API base URL (``…/apps/api/<id>``), no trailing
    slash and no query string, or ``""``."""
    base, _token = parse_api_url(_settings().get("api_url"))
    return base


def token() -> str:
    """The Maker API access token. Stored as ``access_token_secret`` on
    disk per the settings_store secret convention. Falls back to a token
    embedded in a pasted example URL so one paste is enough."""
    s = _settings()
    explicit = (s.get("access_token_secret") or s.get("access_token") or "").strip()
    if explicit:
        return explicit
    _base, embedded = parse_api_url(s.get("api_url"))
    return embedded


def temperature_unit() -> str:
    """``"°F"`` or ``"°C"`` from the ``temperature_unit`` setting.

    Accepts ``F`` / ``C`` in any case, with or without the degree sign,
    or the words ``fahrenheit`` / ``celsius``. Defaults to Fahrenheit
    because that is the hub's own default scale."""
    raw = str(_settings().get("temperature_unit") or "").strip().lower().lstrip("°")
    if raw.startswith("c"):
        return "°C"
    return "°F"


def _verify_tls() -> bool:
    # Default on; only an explicit False opts out (self-signed HTTPS).
    return _settings().get("verify_tls", True) is not False


def _not_configured() -> RuntimeError:
    """The error every call raises when the hub can't be used, said
    precisely enough to act on. Distinguishes "never set up" from "the
    stored token can no longer be decrypted" (an encryption key change
    turns the ciphertext into an empty string)."""
    have_url = bool(api_url())
    try:
        store = current_app.config["SETTINGS_STORE"]
        unreadable = store.unreadable_secrets("plugins", "hubitat_core")
    except Exception:
        unreadable = set()
    if "access_token_secret" in unreadable:
        return RuntimeError(
            "Hubitat access token can no longer be decrypted (the encryption key "
            "changed); re-enter it in Settings -> Widgets -> Hubitat Core"
        )
    if have_url:
        return RuntimeError(
            "Hubitat access token is missing; add it in Settings -> Widgets -> Hubitat Core"
        )
    return RuntimeError("Hubitat is not configured")


def is_configured() -> bool:
    return bool(api_url() and token())


def _ssl_context() -> ssl.SSLContext | None:
    if _verify_tls():
        return None  # urllib uses the default verifying context
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }


def _url(path: str, params: dict[str, Any] | None = None) -> str:
    query = {"access_token": token()}
    if params:
        query.update({k: v for k, v in params.items() if v is not None})
    return f"{api_url()}{path}?{urlencode(query)}"


def request_json(path: str, *, params: dict[str, Any] | None = None, timeout: int = 10) -> Any:
    """GET ``<api_url><path>?access_token=…`` and return the parsed JSON
    body. ``path`` must start with '/'. Raises on missing config or HTTP
    errors so each widget can decide how to surface them."""
    if not is_configured():
        raise _not_configured()
    req = urllib.request.Request(_url(path, params), headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        raw = resp.read().decode("utf-8")
    if not raw.strip():
        return None
    return json.loads(raw)


# -- normalisation -----------------------------------------------------


def _attribute_map(raw: Any) -> dict[str, Any]:
    """``/devices/all`` gives ``{name: value}``; ``/devices/<id>`` gives
    ``[{name, currentValue, ...}]``. Return the flat form either way."""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    out: dict[str, Any] = {}
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if name:
                out[name] = entry.get("currentValue")
    return out


def _string_list(raw: Any, key: str) -> list[str]:
    """Capabilities and commands arrive as plain strings, or as objects
    (``{"command": "on"}`` / ``{"attributes": [...]}``). Keep the strings
    plus any object carrying ``key``."""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and entry.get(key):
            out.append(str(entry[key]))
    return out


def normalise_device(raw: Any) -> dict[str, Any]:
    """One device in a shape widgets can rely on, whichever endpoint it
    came from: ``{id, label, name, type, room, capabilities, attributes,
    commands}`` with ``attributes`` flat and ``id`` a string."""
    if not isinstance(raw, dict):
        return {}
    return {
        "id": str(raw.get("id") or ""),
        "label": str(raw.get("label") or raw.get("name") or ""),
        "name": str(raw.get("name") or ""),
        "type": str(raw.get("type") or ""),
        "room": str(raw.get("room") or ""),
        "capabilities": _string_list(raw.get("capabilities"), "capability"),
        "attributes": _attribute_map(raw.get("attributes")),
        "commands": _string_list(raw.get("commands"), "command"),
    }


def device_label(dev: dict[str, Any]) -> str:
    """Best label for a device: its label, else its driver name, else the id."""
    return str(dev.get("label") or dev.get("name") or f"Device {dev.get('id', '')}").strip()


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def humanize_attribute(name: str) -> str:
    """``coolingSetpoint`` → ``Cooling setpoint``; ``pm25`` stays ``pm25``."""
    text = _CAMEL.sub(" ", str(name or "")).replace("_", " ").strip().lower()
    return text[:1].upper() + text[1:] if text else ""


# Attribute name → unit. Maker API device payloads carry no units, so
# these come from the attribute vocabulary Hubitat drivers share.
# Temperature-shaped attributes follow the hub's scale setting.
_TEMPERATURE_ATTRS = frozenset(
    {"temperature", "coolingSetpoint", "heatingSetpoint", "thermostatSetpoint"}
)
_UNITS: dict[str, str] = {
    "humidity": "%",
    "battery": "%",
    "level": "%",
    "illuminance": "lx",
    "power": "W",
    "energy": "kWh",
    "voltage": "V",
    "amperage": "A",
    "frequency": "Hz",
    "carbonDioxide": "ppm",
    "carbonMonoxide": "ppm",
    "pm25": "µg/m³",
    "pm10": "µg/m³",
    "rate": "L/min",
}


def unit_for(attribute: str) -> str:
    if attribute in _TEMPERATURE_ATTRS:
        return temperature_unit()
    return _UNITS.get(attribute, "")


_DATE_OFFSET = re.compile(r"^(.*[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)([+-]\d{2})(\d{2})$")


def parse_event_date(raw: Any) -> float | None:
    """Hubitat stamps events like ``2024-03-09T18:44:20+0000`` (no colon
    in the offset). Return a POSIX timestamp, or ``None`` when the text
    isn't a date."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    m = _DATE_OFFSET.match(text)
    if m:
        text = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# -- caches ------------------------------------------------------------

# Short-TTL caches. The page editor hydrates every cell on each preview
# cycle and resolves an attribute dropdown per hubitat_* widget on load;
# without caching each of those costs a full ``/devices/all`` dump. One
# dump per few seconds is fresh enough for a preview or a panel push, so
# all callers inside the window share one request. Errors are cached for
# the same window so an unreachable hub fails fast once instead of
# stacking sequential timeouts. Event history is cached longer: a 24 h
# sparkline's shape doesn't change within a couple of minutes. Both
# caches key on (api_url, token) so a settings change invalidates them.
_DEVICES_TTL_S = 5.0
_EVENTS_TTL_S = 120.0
_EVENTS_CACHE_MAX = 256

_devices_lock = threading.Lock()
_devices_cache: tuple[tuple[str, str], float, list[dict[str, Any]] | Exception] | None = None
_events_lock = threading.Lock()
_events_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


def _invalidate_devices_cache() -> None:
    """Drop the cached device dump. Called after every command so a touch
    action's re-render reads the post-command state."""
    global _devices_cache
    with _devices_lock:
        _devices_cache = None


def get_devices() -> list[dict[str, Any]]:
    """Every device the Maker API exposes, normalised (see
    ``normalise_device``). Served from a shared short-TTL cache; callers
    must treat the returned list as read-only. Holding the lock across
    the fetch is deliberate: concurrent cell hydrations queue behind one
    request and then all hit the cache."""
    global _devices_cache
    key = (api_url(), token())
    with _devices_lock:
        if _devices_cache is not None:
            cached_key, ts, value = _devices_cache
            if cached_key == key and time.monotonic() - ts < _DEVICES_TTL_S:
                if isinstance(value, Exception):
                    raise value
                return value
        try:
            data = request_json("/devices/all")
        except Exception as err:
            _devices_cache = (key, time.monotonic(), err)
            raise
        devices = [d for d in (normalise_device(x) for x in (data or [])) if d.get("id")]
        _devices_cache = (key, time.monotonic(), devices)
        return devices


def get_device(device_id: str) -> dict[str, Any]:
    """One device's full detail, normalised. Not cached."""
    data = request_json(f"/devices/{quote(str(device_id))}")
    return normalise_device(data)


def events(device_id: str, *, hours: int = 24, timeout: int = 12) -> list[dict[str, Any]]:
    """Recent events for one device over the last ``hours``, oldest
    first, each as ``{name, value, unit, ts}`` (``ts`` a POSIX
    timestamp). The hub keeps a fixed number of events per device, so a
    chatty sensor covers less wall-clock than a quiet one. Served from a
    per-device TTL cache (errors are not cached)."""
    key = (api_url(), token(), str(device_id))
    with _events_lock:
        hit = _events_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _EVENTS_TTL_S:
            return hit[1]
    data = request_json(f"/devices/{quote(str(device_id))}/events", timeout=timeout)
    cutoff = time.time() - hours * 3600
    samples: list[dict[str, Any]] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        ts = parse_event_date(entry.get("date"))
        if ts is None or ts < cutoff:
            continue
        samples.append(
            {
                "name": str(entry.get("name") or ""),
                "value": entry.get("value"),
                "unit": str(entry.get("unit") or ""),
                "ts": ts,
            }
        )
    samples.sort(key=lambda s: s["ts"])
    with _events_lock:
        if len(_events_cache) >= _EVENTS_CACHE_MAX:
            _events_cache.clear()
        _events_cache[key] = (time.monotonic(), samples)
    return samples


def send_command(device_id: str, command: str, value: Any = None, *, timeout: int = 10) -> Any:
    """Send ``command`` (``on`` / ``off`` / ``setLevel`` …) to a device,
    with an optional secondary value (``/devices/12/setLevel/40``).
    Returns whatever the hub echoes back (usually the device detail).
    Invalidates the device cache so the next render sees the result."""
    if not is_configured():
        raise _not_configured()
    path = f"/devices/{quote(str(device_id))}/{quote(str(command))}"
    if value is not None and str(value) != "":
        path += f"/{quote(str(value))}"
    req = urllib.request.Request(_url(path), headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        raw = resp.read().decode("utf-8")
    _invalidate_devices_cache()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


# -- editor choices ----------------------------------------------------

_GUIDE_UNCONFIGURED = "Set the Maker API URL + access token in Settings → Widgets → Hubitat Core"
_GUIDE_UNREACHABLE = "Hubitat unreachable, check the Maker API URL + access token"


def _guidance(label: str) -> list[dict[str, str]]:
    return [{"value": "", "label": label}]


def attribute_choices(
    attributes: tuple[str, ...] | None = None,
    capabilities: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    """Powers an editor dropdown of device attributes
    (``choices_from: "entity"`` on the sensor widget).

    One entry per (device, attribute) pair as ``{value: "<id>:<attr>",
    label: "<device> · <Attribute>"}``, optionally filtered to the given
    attribute names or to devices with one of the given capabilities.
    When the hub isn't configured or is unreachable, returns a single
    guidance option instead of an empty dropdown so the editor explains
    itself."""
    if not is_configured():
        return _guidance(_GUIDE_UNCONFIGURED)
    try:
        devices = get_devices()
    except Exception:
        return _guidance(_GUIDE_UNREACHABLE)
    out: list[dict[str, str]] = []
    for dev in devices:
        if capabilities and not set(dev.get("capabilities") or []) & set(capabilities):
            continue
        label = device_label(dev)
        for attr in sorted(dev.get("attributes") or {}):
            if attributes and attr not in attributes:
                continue
            out.append(
                {"value": f"{dev['id']}:{attr}", "label": f"{label} · {humanize_attribute(attr)}"}
            )
    out.sort(key=lambda c: c["label"].lower())
    return out


def device_choices(capabilities: tuple[str, ...] | None = None) -> list[dict[str, str]]:
    """Whole-device dropdown (``{value: "<id>", label: "<device>"}``),
    optionally filtered to devices with one of the given capabilities.
    Same guidance behaviour as ``attribute_choices``."""
    if not is_configured():
        return _guidance(_GUIDE_UNCONFIGURED)
    try:
        devices = get_devices()
    except Exception:
        return _guidance(_GUIDE_UNREACHABLE)
    out = [
        {"value": dev["id"], "label": device_label(dev)}
        for dev in devices
        if not capabilities or set(dev.get("capabilities") or []) & set(capabilities)
    ]
    out.sort(key=lambda c: c["label"].lower())
    return out


def coerce_error(err: Exception) -> str:
    """Friendly one-line error for widgets to surface."""
    if isinstance(err, urllib.error.HTTPError):
        reason = err.reason
        if err.code in (401, 403):
            reason = "access denied, check the access token"
        elif err.code == 404:
            reason = "not found, check the Maker API URL (…/apps/api/<app id>) and that the device is exposed"
        return f"Hubitat HTTP {err.code}: {reason}"
    if isinstance(err, urllib.error.URLError):
        return f"Hubitat unreachable: {err.reason}"
    return f"{type(err).__name__}: {err}"
