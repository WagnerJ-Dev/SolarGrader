"""Pure geometry / source helpers that need no network."""

import pytest
from shapely.geometry import box

from solargrader.config import Config
from solargrader.geometry import polygon_area_m2
from solargrader.solar import panels_on_plane
from solargrader.sources.buildings import _quadkey
from solargrader.sources.regions import county_bbox, resolve_targets
from solargrader.sources.tiles import _covering_cells


def test_polygon_area_positive_and_monotonic():
    small = polygon_area_m2(box(-76.0, 40.0, -75.999, 40.001))
    big = polygon_area_m2(box(-76.0, 40.0, -75.998, 40.002))
    assert small > 0
    assert big > small


def test_panels_on_plane_scales_with_area():
    cfg = Config()
    assert panels_on_plane(1.0, cfg) == 0           # tiny plane erodes to nothing
    forty = panels_on_plane(100.0, cfg)
    assert forty > 0
    assert panels_on_plane(200.0, cfg) > forty


def test_quadkey_known_value():
    # Harrisburg point at zoom 9 (verified against the MS partition scheme)
    assert _quadkey(-76.88, 40.27, 9) == "032010012"


def test_covering_cells_derives_utm_zone_from_bbox():
    cells, zone, epsg = _covering_cells((-76.93, 40.24, -76.83, 40.31))
    assert zone == 18
    assert epsg == 26918
    assert len(cells) > 0
    # Columbus, OH is UTM zone 17 — proves it isn't hardcoded.
    _, oh_zone, oh_epsg = _covering_cells((-83.02, 39.95, -82.98, 39.99))
    assert oh_zone == 17 and oh_epsg == 26917


def test_county_name_injection_is_rejected():
    with pytest.raises(ValueError):
        county_bbox("Dauphin' OR '1'='1", Config())


def test_resolve_targets_bbox_needs_no_network():
    targets = resolve_targets(Config(), bbox=(-77.0, 40.0, -76.0, 41.0))
    assert targets == [("bbox", (-77.0, 40.0, -76.0, 41.0), False)]
