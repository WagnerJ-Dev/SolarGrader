"""Config derived quantities and overrides."""

import pytest

from solargrader.config import Config


def test_derived_quantities():
    c = Config(panel_watts=400, panel_width_m=1.05, panel_height_m=1.74)
    assert c.panel_area_m2 == pytest.approx(1.827, abs=1e-3)
    assert c.module_efficiency == pytest.approx(0.2189, abs=1e-3)
    assert c.residential_panel_cap == 25


def test_overrides_only_touch_named_fields():
    c = Config(building_source="ms", n_workers=8)
    assert c.building_source == "ms"
    assert c.n_workers == 8
    assert c.db_path == "solar_grader.duckdb"   # untouched default
