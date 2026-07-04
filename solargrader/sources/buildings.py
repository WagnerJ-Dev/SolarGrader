"""
Building footprint sources, all returning ``list[Building]`` so scoring is identical
regardless of provider:

    "ms"      Microsoft Building Footprints — national, ODbL (the scale-out source)
    "county"  a county ArcGIS service (Chester County / PASDA by default)
    "osm"     OpenStreetMap via Overpass (handy for quick prototyping)
"""

from __future__ import annotations

import gzip
import json
import math
import os

import numpy as np
import requests
from shapely.geometry import Polygon

from ..config import Config
from ..geometry import polygon_area_m2
from ..models import Building

# ── OpenStreetMap (Overpass) ──────────────────────────────────────────────────

def get_buildings_from_osm(bbox, cfg: Config) -> list[Building]:
    """Building polygons from the Overpass API (prototyping source)."""
    west, south, east, north = bbox
    query = (f"[out:json][timeout:60];(way[\"building\"]"
             f"({south},{west},{north},{east}););out geom;")
    r = requests.post(cfg.overpass_url, data={"data": query},
                      headers={"User-Agent": cfg.http_user_agent}, timeout=90)
    r.raise_for_status()

    buildings = []
    for el in r.json().get("elements", []):
        if el["type"] != "way" or "geometry" not in el:
            continue
        coords = [(n["lon"], n["lat"]) for n in el["geometry"]]
        if len(coords) < 4:
            continue
        try:
            poly = Polygon(coords)
            if not poly.is_valid or poly.area == 0:
                continue
        except Exception:  # noqa: BLE001
            continue
        buildings.append(Building(id=el["id"], geometry=poly,
                                  lat=poly.centroid.y, lon=poly.centroid.x,
                                  footprint_m2=polygon_area_m2(poly)))
    print(f"  Found {len(buildings)} buildings (OSM).")
    return buildings


# ── County ArcGIS service (PASDA / Chester County by default) ─────────────────

def get_buildings_from_county(bbox, cfg: Config) -> list[Building]:
    """County building footprints via ArcGIS REST. An id-only query then batched
    geometry fetches keeps us robust to the service's 1000-record cap."""
    west, south, east, north = bbox
    geom = {"geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects"}
    print("  Querying county building footprints...")
    ids = requests.get(cfg.county_footprints_url,
                       params={**geom, "where": "1=1", "returnIdsOnly": "true",
                               "f": "json"}, timeout=90).json().get("objectIds") or []

    buildings = []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        resp = requests.post(cfg.county_footprints_url, data={
            "objectIds": ",".join(map(str, batch)), "outFields": "OBJECTID",
            "returnGeometry": "true", "outSR": "4326", "f": "json"}, timeout=90)
        resp.raise_for_status()
        for f in resp.json().get("features", []):
            rings = (f.get("geometry") or {}).get("rings")
            if not rings:
                continue
            try:
                poly = Polygon(rings[0])
                if not poly.is_valid or poly.area == 0:
                    continue
            except Exception:  # noqa: BLE001
                continue
            buildings.append(Building(id=int(f["attributes"]["OBJECTID"]), geometry=poly,
                                      lat=poly.centroid.y, lon=poly.centroid.x,
                                      footprint_m2=polygon_area_m2(poly)))
    print(f"  Found {len(buildings)} buildings (county).")
    return buildings


# ── Microsoft Building Footprints (national, quadkey-partitioned) ─────────────

def _quadkey(lon: float, lat: float, z: int) -> str:
    """Bing Maps quadkey for a lon/lat at zoom z (MS footprint partition scheme)."""
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    qk = ""
    for i in range(z, 0, -1):
        digit, mask = 0, 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        qk += str(digit)
    return qk


def _ms_link_for(qk: str, cfg: Config):
    """Download URL for a US quadkey in MS's dataset index (index cached on disk)."""
    os.makedirs(cfg.ms_cache_dir, exist_ok=True)
    links = os.path.join(cfg.ms_cache_dir, "dataset-links.csv")
    if not os.path.exists(links):
        print("  Fetching MS dataset index (one-time)...")
        r = requests.get(cfg.ms_dataset_links_url, timeout=180)
        r.raise_for_status()
        with open(links, "w") as f:
            f.write(r.text)
    with open(links) as f:
        for line in f:
            p = line.split(",")
            if len(p) >= 3 and p[0] == "UnitedStates" and p[1] == qk:
                return p[2]
    return None


# quadkey -> (centroids ndarray, list-of-rings); a quadkey covers a wide area and is
# reused across many tiles, so parsing it once per process is worth caching.
_MS_CACHE: dict = {}


def _load_ms_quadkey(qk: str, cfg: Config):
    """Download (once) and parse a quadkey's GeoJSONL into centroids + rings."""
    if qk in _MS_CACHE:
        return _MS_CACHE[qk]
    url = _ms_link_for(qk, cfg)
    if not url:
        _MS_CACHE[qk] = (np.empty((0, 2)), [])
        return _MS_CACHE[qk]
    path = os.path.join(cfg.ms_cache_dir, f"{qk}.csv.gz")
    if not os.path.exists(path):
        print(f"  Downloading MS footprints quadkey {qk} (once)...")
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    centroids, rings = [], []
    with gzip.open(path, "rt") as f:
        for line in f:
            try:
                coords = json.loads(line)["geometry"]["coordinates"][0]
            except Exception:  # noqa: BLE001
                continue
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))
            rings.append(coords)
    _MS_CACHE[qk] = (np.array(centroids), rings)
    print(f"  Loaded {len(rings):,} MS buildings for quadkey {qk}.")
    return _MS_CACHE[qk]


def get_buildings_from_ms(bbox, cfg: Config) -> list[Building]:
    """Microsoft Building Footprints within bbox (national / other-states source)."""
    w, s, e, n = bbox
    quadkeys = {_quadkey(lo, la, cfg.ms_zoom) for lo in (w, e) for la in (s, n)}
    buildings = []
    for qk in quadkeys:
        centroids, rings = _load_ms_quadkey(qk, cfg)
        if len(centroids) == 0:
            continue
        cx, cy = centroids[:, 0], centroids[:, 1]
        mask = (cx >= w) & (cx <= e) & (cy >= s) & (cy <= n)
        qk_int = int(qk)
        for idx in np.nonzero(mask)[0]:
            try:
                poly = Polygon(rings[idx])
                if not poly.is_valid or poly.area == 0:
                    continue
            except Exception:  # noqa: BLE001
                continue
            lon, lat = float(cx[idx]), float(cy[idx])
            buildings.append(Building(id=qk_int * 10_000_000 + int(idx), geometry=poly,
                                      lat=lat, lon=lon, footprint_m2=polygon_area_m2(poly)))
    print(f"  Found {len(buildings)} buildings (MS footprints).")
    return buildings


# ── Dispatcher ────────────────────────────────────────────────────────────────

_SOURCES = {
    "osm": get_buildings_from_osm,
    "county": get_buildings_from_county,
    "ms": get_buildings_from_ms,
}


def get_buildings(bbox, cfg: Config) -> list[Building]:
    """Fetch buildings from the source named by ``cfg.building_source``."""
    try:
        source = _SOURCES[cfg.building_source]
    except KeyError:
        raise ValueError(f"Unknown building_source {cfg.building_source!r}; "
                         f"choose one of {sorted(_SOURCES)}") from None
    return source(bbox, cfg)
