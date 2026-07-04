"""
Domain data models — lightweight ``@dataclass`` records passed between pipeline
stages. These replace opaque dicts so signatures are self-documenting and typed,
without adding behaviour-heavy classes.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Polygon


@dataclass
class Building:
    """A building footprint from any source (OSM / county / Microsoft).

    ``id`` is a stable, source-unique integer (kept in the DB as ``osm_id`` for
    backward compatibility). ``geometry`` is in lon/lat (EPSG:4326).
    """
    id: int
    geometry: Polygon
    lat: float
    lon: float
    footprint_m2: float


@dataclass
class RoofPlane:
    """One planar roof face fitted from LiDAR."""
    tilt_deg: float          # 0 = flat, 90 = vertical
    azimuth_deg: float       # compass bearing the plane faces (0=N, 90=E, 180=S)
    area_m2: float           # true tilted surface area
    point_count: int


@dataclass
class SystemResult:
    """Energy model output for one roof: a residential (capped) system and the
    maximum-roof system, plus the shading loss backed out from the skyline."""
    res_kwh: float
    res_kw: float
    res_panels: int
    max_kwh: float
    max_kw: float
    max_panels: int
    shade_loss_pct: float
    best_plane: RoofPlane


@dataclass
class ScoredHome:
    """A fully scored building — the row persisted to the ``homes`` table."""
    building: Building
    usable_area_m2: float
    primary_tilt_deg: float
    primary_azimuth_deg: float
    plane_count: int
    res_kwh: float
    res_kw: float
    res_panels: int
    max_kwh: float
    max_kw: float
    max_panels: int
    potential_grade: str
    shade_loss_pct: float
    grade: str
    score: int
