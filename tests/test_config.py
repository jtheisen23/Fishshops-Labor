"""Tests for env-driven location config, incl. the GUID:Name format."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toast_reports.config import load_config  # noqa: E402


def test_guid_name_pairs(monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUIDS", "guid-a:Pacific Beach,guid-b:Encinitas,guid-c")
    cfg = load_config(config_path=None)  # no YAML; env only
    by_guid = {loc.guid: loc.name for loc in cfg.locations}
    assert by_guid["guid-a"] == "Pacific Beach"
    assert by_guid["guid-b"] == "Encinitas"
    # No name given -> placeholder that the live pull will replace via Toast.
    assert by_guid["guid-c"] == "Location 3"
