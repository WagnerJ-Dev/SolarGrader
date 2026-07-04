"""
Pure geometry / coordinate helpers — no I/O, no heavy dependencies.

The pipeline mixes two coordinate systems: building footprints arrive in lon/lat
(EPSG:4326) while LiDAR tiles are in projected meters (a UTM zone). These functions
handle the reprojection and the clip of a loaded point cloud to one footprint.
"""

from __future__ import annotations

import math

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

from .config import Config

# A degree of latitude ≈ 111,320 m; a degree of longitude ≈ that × cos(lat).
_M_PER_DEG = 111320.0

# LiDAR point tuple: (structured array, X float view, Y float view). Loading a tile
# once and masking in RAM is far faster than re-reading the file per building.
TilePoints = tuple[np.ndarray, np.ndarray, np.ndarray]


def polygon_area_m2(poly: Polygon) -> float:
    """Approximate a lon/lat polygon's area in m² using a local equal-area scale."""
    lat = poly.centroid.y
    deg2_to_m2 = (_M_PER_DEG ** 2) * math.cos(math.radians(lat))
    return poly.area * deg2_to_m2


def transformer_to(tile_srs: str) -> Transformer:
    """Transformer from lon/lat (EPSG:4326) into a tile's projected CRS."""
    return Transformer.from_crs("EPSG:4326", tile_srs, always_xy=True)


def clip_lidar_to_building(tile_points: TilePoints, footprint_poly: Polygon,
                           transformer: Transformer, cfg: Config,
                           buffer_m: float = 0.5) -> np.ndarray | None:
    """Extract roof-ish LiDAR points inside one building footprint.

    The footprint (lon/lat) is reprojected into the tile CRS, a fast bounding-box
    mask narrows the candidates, then an exact point-in-polygon test selects the
    building's points. Building-classified returns (class 6) are preferred; failing
    that, points more than 2 m above the local minimum are used. Returns an (N, 3)
    array of [X, Y, Z] in tile meters, or ``None`` if too few points are found.
    """
    pts, X, Y = tile_points
    proj_poly = shapely_transform(transformer.transform, footprint_poly).buffer(buffer_m)

    minx, miny, maxx, maxy = proj_poly.bounds
    bbox_mask = (X >= minx) & (X <= maxx) & (Y >= miny) & (Y <= maxy)
    if not bbox_mask.any():
        return None

    sub = pts[bbox_mask]
    inside = shapely.contains_xy(proj_poly, sub["X"], sub["Y"])
    sub = sub[inside]
    if len(sub) == 0:
        return None

    if "Classification" in sub.dtype.names:
        roof_pts = sub[sub["Classification"] == 6]
        if len(roof_pts) < cfg.min_roof_points:
            z_min = sub["Z"].min()
            roof_pts = sub[sub["Z"] > z_min + 2.0]
    else:
        z_min = sub["Z"].min()
        roof_pts = sub[sub["Z"] > z_min + 2.0]

    if len(roof_pts) < cfg.min_roof_points:
        return None

    return np.column_stack([roof_pts["X"], roof_pts["Y"], roof_pts["Z"]])


def tile_latlon_bbox(tile_points: TilePoints, tile_srs: str) -> tuple[float, float, float, float]:
    """Derive a tile's lon/lat bbox (w, s, e, n) from its actual point extent."""
    _, X, Y = tile_points
    inv = Transformer.from_crs(tile_srs, "EPSG:4326", always_xy=True)
    xs = [X.min(), X.max(), X.min(), X.max()]
    ys = [Y.min(), Y.min(), Y.max(), Y.max()]
    lons, lats = inv.transform(xs, ys)
    return (min(lons), min(lats), max(lons), max(lats))
