"""hubitat_sensor smoke: fetch() builds value blocks from one
get_devices call (monkeypatched, no network) for single + multiple
attributes, with trend/sparkline from the device event log."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_BUNDLE = Path(__file__).resolve().parent.parent.parent


def _load(name: str, folder: str):
    spec = importlib.util.spec_from_file_location(name, _BUNDLE / folder / "server.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_DEVICES = [
    {
        "id": "7",
        "label": "Lounge",
        "capabilities": ["TemperatureMeasurement", "Battery"],
        "attributes": {"temperature": "72.5", "humidity": "41", "battery": 88},
    },
    {
        "id": "3",
        "label": "Front door",
        "capabilities": ["ContactSensor"],
        "attributes": {"contact": "closed", "tamper": None},
    },
    {
        "id": "9",
        "name": "Generic switch",
        "capabilities": ["Switch"],
        "attributes": {"switch": "on"},
    },
]

_EVENTS = {
    "7": [
        {"name": "temperature", "value": "70", "unit": "", "ts": 1.0},
        {"name": "humidity", "value": "50", "unit": "", "ts": 1.5},
        {"name": "temperature", "value": "71", "unit": "", "ts": 2.0},
        {"name": "temperature", "value": "72.5", "unit": "", "ts": 3.0},
    ],
}


@pytest.fixture
def mods(monkeypatch):
    core = _load("hubitat_core_server_for_sensor", "hubitat_core")
    sensor = _load("hubitat_sensor_server", "hubitat_sensor")
    settings: dict[str, Any] = {
        "api_url": "http://hub/apps/api/1",
        "access_token_secret": "t",
        "temperature_unit": "F",
    }
    monkeypatch.setattr(core, "_settings", lambda: settings)
    monkeypatch.setattr(core, "get_devices", lambda: [core.normalise_device(d) for d in _DEVICES])
    monkeypatch.setattr(core, "events", lambda device_id, **_kw: _EVENTS.get(device_id, []))
    monkeypatch.setattr(sensor, "_core", lambda: core)
    return sensor, core


def test_empty_when_nothing_selected(mods) -> None:
    sensor, _ = mods
    assert sensor.fetch({"entities": "  "}, {}, ctx={})["empty"] is True
    assert sensor.fetch({"entities": []}, {}, ctx={})["empty"] is True


def test_missing_core_is_reported(mods, monkeypatch) -> None:
    sensor, _ = mods
    monkeypatch.setattr(sensor, "_core", lambda: None)
    out = sensor.fetch({"entities": ["7:temperature"]}, {}, ctx={})
    assert "Hubitat Core" in out["error"]


def test_single_attribute_is_a_stat_with_history(mods) -> None:
    sensor, _ = mods
    out = sensor.fetch({"entities": ["7:temperature"]}, {}, ctx={})
    assert out["title"] == "Lounge"
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["name"] == "Lounge"
    assert it["value"] == "72.5"
    assert it["unit"] == "°F"
    assert it["icon"] == "thermometer"
    assert it["unavailable"] is False
    # Only the temperature events feed the series, not the humidity one.
    assert it["sparkline"] == [70.0, 71.0, 72.5]
    assert it["trend"] == "up"


def test_multiple_attributes_list_and_disambiguate_names(mods) -> None:
    sensor, _ = mods
    out = sensor.fetch(
        {"entities": ["7:temperature", "7:battery", "3:contact", "9:switch", "3:tamper"]},
        {},
        ctx={},
    )
    assert out["title"] == "Sensors"
    names = [it["name"] for it in out["items"]]
    # Lounge and Front door contribute two rows each, so those carry the
    # attribute; the single-attribute device is named after itself.
    assert names[:2] == ["Lounge temperature", "Lounge battery"]
    assert names[2] == "Front door contact"
    assert names[3] == "Generic switch"  # no label → driver name
    assert names[4] == "Front door tamper"
    values = [it["value"] for it in out["items"]]
    assert values == ["72.5", "88", "closed", "on", "-"]
    icons = [it["icon"] for it in out["items"]]
    assert icons == ["thermometer", "battery-medium", "door", "toggle-right", "warning"]
    units = [it["unit"] for it in out["items"]]
    assert units == ["°F", "%", "", "", ""]
    assert out["items"][4]["unavailable"] is True
    # Text states never get a series; battery has no events → no keys.
    assert "sparkline" not in out["items"][2]
    assert "sparkline" not in out["items"][1]


def test_auto_name_avoids_doubled_label(mods) -> None:
    sensor, _ = mods
    assert sensor._auto_name("Lounge", "Temperature", 1) == "Lounge"
    assert sensor._auto_name("Lounge", "Temperature", 2) == "Lounge temperature"
    assert sensor._auto_name("Thermostat", "Thermostat mode", 3) == "Thermostat mode"
    assert sensor._auto_name("thermostat", "Thermostat operating state", 2) == (
        "Thermostat operating state"
    )


def test_unknown_device_and_legacy_string_input(mods) -> None:
    sensor, _ = mods
    out = sensor.fetch({"entities": "42:temperature, 7:humidity"}, {}, ctx={})
    ghost, hum = out["items"]
    assert ghost["unavailable"] is True
    assert ghost["value"] == "-"
    assert ghost["icon"] == "question"
    assert hum["value"] == "41"
    assert hum["icon"] == "drop"


def test_overrides_and_number_format(mods) -> None:
    sensor, _ = mods
    out = sensor.fetch(
        {
            "entities": ["7:temperature", "7:humidity"],
            "overrides": "7:temperature | Living room | fire | 0.00\n# comment\n7:humidity ||| 0",
            "number_format": "0.0",
            "show_unit": False,
            "show_trend": False,
            "show_sparkline": False,
            "show_title": False,
            "show_names": False,
        },
        {},
        ctx={},
    )
    temp, hum = out["items"]
    assert temp["name"] == "Living room"
    assert temp["icon"] == "fire"
    assert temp["value"] == "72.50"
    assert temp["unit"] == ""
    assert hum["value"] == "41"
    assert "trend" not in temp and "sparkline" not in temp
    assert out["show_title"] is False
    assert out["show_names"] is False


def test_hub_errors_surface_via_core(mods, monkeypatch) -> None:
    sensor, core = mods

    def boom():
        raise RuntimeError("Hubitat is not configured")

    monkeypatch.setattr(core, "get_devices", boom)
    out = sensor.fetch({"entities": ["7:temperature"]}, {}, ctx={})
    assert out["error"] == "RuntimeError: Hubitat is not configured"


def test_choices_delegate_to_core(mods, monkeypatch) -> None:
    sensor, core = mods
    monkeypatch.setattr(core, "attribute_choices", lambda *a, **k: [{"value": "7:x", "label": "X"}])
    assert sensor.choices("entity") == [{"value": "7:x", "label": "X"}]
    assert sensor.choices("other") == []


def test_format_value_rounds_and_passes_text(mods) -> None:
    sensor, _ = mods
    assert sensor._format_value("72.00") == "72"
    assert sensor._format_value("68.456") == "68.46"
    assert sensor._format_value("68.456", "0.0") == "68.5"
    assert sensor._format_value("open") == "open"
    assert sensor._trend([1, 1.5, 1.01]) == "flat"
    assert sensor._trend([5, 3]) == "down"
