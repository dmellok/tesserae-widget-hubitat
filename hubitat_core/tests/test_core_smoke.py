"""hubitat_core smoke: URL parsing, config gating, payload normalisation,
event-log shaping, choices, error coercion. No network: ``request_json``
is monkeypatched and settings are stubbed, so no Flask app is needed."""

from __future__ import annotations

import importlib.util
import io
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent.parent


def _load_core():
    spec = importlib.util.spec_from_file_location("hubitat_core_server", _HERE / "server.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hubitat_core_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def core(monkeypatch):
    mod = _load_core()
    settings: dict[str, Any] = {}
    monkeypatch.setattr(mod, "_settings", lambda: settings)
    mod._invalidate_devices_cache()
    mod._events_cache.clear()
    mod._test_settings = settings  # type: ignore[attr-defined]
    return mod


def _configure(core, **extra: Any) -> None:
    core._test_settings.clear()
    core._test_settings.update(
        {"api_url": "http://hub.local/apps/api/12", "access_token_secret": "tok", **extra}
    )


# The two shapes Maker API returns for the same device.
_ALL_DEVICE = {
    "id": "7",
    "name": "Generic Zigbee Multisensor",
    "label": "Lounge",
    "type": "Zigbee Multi",
    "capabilities": ["TemperatureMeasurement", "Battery", "Sensor"],
    "attributes": {"temperature": "72.5", "humidity": "41", "battery": 88},
    "commands": ["refresh"],
}
_DETAIL_DEVICE = {
    "id": 7,
    "name": "Generic Zigbee Multisensor",
    "label": "Lounge",
    "capabilities": ["TemperatureMeasurement", {"attributes": [{"name": "temperature"}]}],
    "attributes": [
        {"name": "temperature", "currentValue": "72.5", "dataType": "NUMBER"},
        {"name": "battery", "currentValue": 88, "dataType": "NUMBER"},
    ],
    "commands": [{"command": "refresh"}, "configure"],
}


# -- URL parsing -------------------------------------------------------


def test_parse_api_url_accepts_bare_base(core) -> None:
    assert core.parse_api_url("http://192.168.1.20/apps/api/12/") == (
        "http://192.168.1.20/apps/api/12",
        "",
    )


def test_parse_api_url_strips_pasted_example_and_lifts_token(core) -> None:
    base, tok = core.parse_api_url(
        "http://192.168.1.20/apps/api/12/devices/all?access_token=abc-123"
    )
    assert base == "http://192.168.1.20/apps/api/12"
    assert tok == "abc-123"


def test_parse_api_url_handles_cloud_form(core) -> None:
    base, tok = core.parse_api_url(
        "https://cloud.hubitat.com/api/HUB-UUID/apps/12/devices?access_token=t"
    )
    assert base == "https://cloud.hubitat.com/api/HUB-UUID/apps/12"
    assert tok == "t"


def test_token_prefers_explicit_setting_then_pasted_url(core) -> None:
    _configure(core, api_url="http://hub/apps/api/1/devices?access_token=from-url")
    assert core.token() == "tok"
    core._test_settings.pop("access_token_secret")
    assert core.token() == "from-url"
    assert core.is_configured()


def test_temperature_unit_normalises(core) -> None:
    for raw, want in [("", "°F"), ("F", "°F"), ("°f", "°F"), ("c", "°C"), ("Celsius", "°C")]:
        _configure(core, temperature_unit=raw)
        assert core.temperature_unit() == want, raw
    _configure(core, temperature_unit="C")
    assert core.unit_for("temperature") == "°C"
    assert core.unit_for("heatingSetpoint") == "°C"
    assert core.unit_for("humidity") == "%"
    assert core.unit_for("contact") == ""


# -- config gating -----------------------------------------------------


def test_unconfigured_choices_guide_the_user(core) -> None:
    assert not core.is_configured()
    choices = core.attribute_choices()
    assert len(choices) == 1
    assert choices[0]["value"] == ""
    assert "Settings → Widgets → Hubitat Core" in choices[0]["label"]
    with pytest.raises(RuntimeError, match="not configured"):
        core.request_json("/devices/all")


def test_url_builder_appends_token(core) -> None:
    _configure(core)
    assert core._url("/devices/all") == "http://hub.local/apps/api/12/devices/all?access_token=tok"


# -- normalisation -----------------------------------------------------


def test_normalise_device_flattens_both_shapes(core) -> None:
    a = core.normalise_device(_ALL_DEVICE)
    b = core.normalise_device(_DETAIL_DEVICE)
    assert a["id"] == b["id"] == "7"
    assert a["label"] == b["label"] == "Lounge"
    assert a["attributes"]["temperature"] == b["attributes"]["temperature"] == "72.5"
    assert a["capabilities"][0] == "TemperatureMeasurement"
    assert b["capabilities"] == ["TemperatureMeasurement"]
    assert b["commands"] == ["refresh", "configure"]
    assert core.normalise_device("junk") == {}


def test_humanize_attribute(core) -> None:
    assert core.humanize_attribute("coolingSetpoint") == "Cooling setpoint"
    assert core.humanize_attribute("temperature") == "Temperature"
    assert core.humanize_attribute("pm25") == "Pm25"


def test_parse_event_date_handles_hubitat_offset(core) -> None:
    ts = core.parse_event_date("2024-03-09T18:44:20+0000")
    assert ts == 1710009860.0
    assert core.parse_event_date("2024-03-09T18:44:20+00:00") == ts
    assert core.parse_event_date("2024-03-09T18:44:20Z") == ts
    assert core.parse_event_date("not a date") is None
    assert core.parse_event_date("") is None


# -- devices + events --------------------------------------------------


def test_get_devices_caches_and_drops_idless_rows(core, monkeypatch) -> None:
    _configure(core)
    calls: list[str] = []

    def fake(path, **_kw):
        calls.append(path)
        return [_ALL_DEVICE, {"name": "no id"}, "junk"]

    monkeypatch.setattr(core, "request_json", fake)
    first = core.get_devices()
    second = core.get_devices()
    assert first is second
    assert calls == ["/devices/all"]
    assert [d["id"] for d in first] == ["7"]


def test_get_devices_caches_errors_briefly(core, monkeypatch) -> None:
    _configure(core)
    calls = 0

    def boom(path, **_kw):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(core, "request_json", boom)
    with pytest.raises(urllib.error.URLError):
        core.get_devices()
    with pytest.raises(urllib.error.URLError):
        core.get_devices()
    assert calls == 1


def test_events_filters_window_and_sorts_ascending(core, monkeypatch) -> None:
    _configure(core)
    now = time.time()

    def stamp(age_s: float) -> str:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(now - age_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")

    log = [
        {"name": "temperature", "value": "73", "date": stamp(60), "unit": "°F"},
        {"name": "temperature", "value": "71", "date": stamp(3600)},
        {"name": "temperature", "value": "60", "date": stamp(40 * 3600)},  # too old
        {"name": "battery", "value": "88", "date": "garbage"},  # unparsable date
        "junk",
    ]
    monkeypatch.setattr(core, "request_json", lambda path, **_kw: log)
    out = core.events("7", hours=24)
    assert [s["value"] for s in out] == ["71", "73"]
    assert out[1]["unit"] == "°F"
    assert out[0]["ts"] < out[1]["ts"]


def test_send_command_builds_path_and_invalidates_cache(core, monkeypatch) -> None:
    _configure(core)
    monkeypatch.setattr(core, "request_json", lambda path, **_kw: [_ALL_DEVICE])
    core.get_devices()
    assert core._devices_cache is not None

    seen: dict[str, str] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_open(req, timeout=0, context=None):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_open)
    out = core.send_command("7", "setLevel", 40)
    assert out == {"ok": True}
    assert seen["url"] == "http://hub.local/apps/api/12/devices/7/setLevel/40?access_token=tok"
    assert core._devices_cache is None


# -- choices -----------------------------------------------------------


def test_attribute_choices_pairs_devices_with_attributes(core, monkeypatch) -> None:
    _configure(core)
    switch = {
        "id": "3",
        "label": "Desk lamp",
        "capabilities": ["Switch"],
        "attributes": {"switch": "on"},
    }
    monkeypatch.setattr(core, "request_json", lambda path, **_kw: [_ALL_DEVICE, switch])
    choices = core.attribute_choices()
    assert [c["value"] for c in choices] == [
        "3:switch",
        "7:battery",
        "7:humidity",
        "7:temperature",
    ]
    assert choices[3]["label"] == "Lounge · Temperature"
    only_temp = core.attribute_choices(attributes=("temperature",))
    assert [c["value"] for c in only_temp] == ["7:temperature"]
    only_switches = core.attribute_choices(capabilities=("Switch",))
    assert [c["value"] for c in only_switches] == ["3:switch"]
    assert [c["value"] for c in core.device_choices()] == ["3", "7"]


def test_unreachable_choices_say_so(core, monkeypatch) -> None:
    _configure(core)

    def boom(path, **_kw):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr(core, "request_json", boom)
    assert core.attribute_choices()[0]["label"].startswith("Hubitat unreachable")
    assert core.device_choices()[0]["value"] == ""


# -- errors ------------------------------------------------------------


def test_coerce_error_is_actionable(core) -> None:
    denied = urllib.error.HTTPError("http://hub", 401, "Unauthorized", {}, io.BytesIO(b""))
    assert core.coerce_error(denied) == "Hubitat HTTP 401: access denied, check the access token"
    missing = urllib.error.HTTPError("http://hub", 404, "Not Found", {}, io.BytesIO(b""))
    assert "Maker API URL" in core.coerce_error(missing)
    assert core.coerce_error(urllib.error.URLError("timed out")) == "Hubitat unreachable: timed out"
    assert core.coerce_error(ValueError("bad")) == "ValueError: bad"
