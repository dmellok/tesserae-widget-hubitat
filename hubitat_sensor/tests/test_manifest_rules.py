"""The options panel follows the shared rules.

A handful of options always visible, the rest revealed by `show_if` or
tucked under More options (`advanced`), advanced ones last, and
conditions that point at an earlier option.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = sorted(ROOT.glob("*/plugin.json"))
MAX_UP_FRONT = 5

# (always visible, under More options), in manifest order.
PANELS = {
    "hubitat_sensor/plugin.json": (
        ["entities", "show_trend", "show_sparkline", "show_title"],
        ["title", "overrides", "number_format", "show_unit", "show_names"],
    ),
}


def _options(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("cell_options", [])


def _up_front(opts: list[dict]) -> list[str]:
    return [o["name"] for o in opts if "show_if" not in o and not o.get("advanced")]


def _id(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_every_manifest_is_checked():
    assert len(MANIFESTS) == 2


@pytest.mark.parametrize("path", MANIFESTS, ids=_id)
def test_few_options_up_front(path):
    assert len(_up_front(_options(path))) <= MAX_UP_FRONT


@pytest.mark.parametrize("path", MANIFESTS, ids=_id)
def test_advanced_options_come_last(path):
    flags = [bool(o.get("advanced")) for o in _options(path)]
    assert flags == sorted(flags)


@pytest.mark.parametrize("path", MANIFESTS, ids=_id)
def test_show_if_points_at_an_earlier_option(path):
    seen: set[str] = set()
    for o in _options(path):
        for key in o.get("show_if") or {}:
            assert key in seen, (o["name"], key)
        seen.add(o["name"])


@pytest.mark.parametrize("path", MANIFESTS, ids=_id)
def test_labels_are_plain(path):
    for o in _options(path):
        texts = [o.get("label", ""), o.get("help", "")]
        texts += [
            c.get("label", "") for c in o.get("choices", []) if isinstance(c, dict)
        ]
        for text in texts:
            assert "\u2014" not in text, o["name"]
        assert "(" not in o.get("label", ""), o["name"]


@pytest.mark.parametrize("rel", sorted(PANELS))
def test_panel_shape(rel):
    opts = _options(ROOT / rel)
    up_front, advanced = PANELS[rel]
    assert _up_front(opts) == up_front
    assert [o["name"] for o in opts if o.get("advanced")] == advanced
