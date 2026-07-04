"""
LiDAR tile discovery, download, and best-quality collection selection (USGS 3DEP).

Primary discovery is the TNM products API (naming-agnostic, returns per-tile
download URLs). If that spatial query is down, a region-agnostic fallback uses the
3DEP index to find covering projects and decodes UTM-named tiles from their staged
directories. When multiple overlapping surveys cover a region, ``select_collections``
builds a best-quality mosaic so scores aren't corrupted by mixing vintages.
"""

from __future__ import annotations

import json
import math
import os
import re

import numpy as np
import requests
from pyproj import Transformer

from ..config import Config
from ..geometry import TilePoints
from ..util import with_retries

# USGS LPC filename with a UTM/MGRS grid tail: ..._<zz><MGRS-3-letters><EEENNN>.laz
_TILE_RE = re.compile(r"(USGS_LPC_[\w.\-]+?_(\d{2})[A-Z]{3}(\d{6}))\.laz")


# ── Primary discovery: TNM products API ───────────────────────────────────────

def find_all_tiles(bbox, cfg: Config, page: int = 50) -> list[dict]:
    """All LiDAR tiles covering bbox from USGS TNM (paginated, de-duped by URL)."""
    west, south, east, north = bbox
    tiles, offset = {}, 0
    while True:
        r = requests.get(cfg.tnm_url, params={
            "datasets": "Lidar Point Cloud (LPC)",
            "bbox": f"{west},{south},{east},{north}",
            "prodFormats": "LAZ", "max": page, "offset": offset,
        }, timeout=60)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        for it in items:
            if it.get("downloadURL"):
                tiles[it["downloadURL"]] = it
        offset += len(items)
        if not items or offset >= data.get("total", 0):
            break
    return list(tiles.values())


def tile_id_of(tile: dict) -> str:
    """Stable id for a tile = its LAZ filename."""
    return tile["downloadURL"].split("/")[-1].split("?")[0]


# ── Fallback discovery: 3DEP index → rockyweb staged directories ──────────────

def _covering_cells(bbox) -> tuple[dict, int, int]:
    """UTM 1 km cells covering bbox, in the bbox's own UTM zone. Returns
    {code -> (easting_km, northing_km)} with the zone and EPSG, where ``code`` is
    the 6-digit 'EEENNN' USGS embeds in tile filenames. Works in any UTM zone."""
    w, s, e, n = bbox
    zone = int(((w + e) / 2 + 180) // 6) + 1
    epsg = 26900 + zone                       # NAD83 / UTM zone <zz>N
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    xs, ys = fwd.transform([w, e, w, e], [s, s, n, n])
    cells = {}
    for ee in range(int(min(xs) // 1000), int(max(xs) // 1000) + 1):
        for nn in range(int(min(ys) // 1000), int(max(ys) // 1000) + 1):
            cells[f"{ee:03d}{nn % 1000:03d}"] = (ee, nn)
    return cells, zone, epsg


def _lidar_projects_covering(bbox, cfg: Config) -> list[tuple[str, str]]:
    """The USGS 3DEP index → [(project, staged LAZ dir URL)] for every lidar project
    whose extent intersects bbox, anywhere in the US."""
    w, s, e, n = bbox
    data = with_retries(lambda: requests.get(cfg.index_lpc_url, params={
        "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "outFields": "project,lpc_link", "returnGeometry": "false", "f": "json",
    }, timeout=60).json())
    out = []
    for f in data.get("features", []):
        a = f.get("attributes", {})
        link = (a.get("lpc_link") or "").rstrip("/")
        if link:
            out.append((a.get("project") or link.split("/")[-1], link))
    return out


def find_tiles_rockyweb(bbox, cfg: Config) -> list[dict]:
    """Region-agnostic fallback when TNM is down. Discovers covering projects via the
    3DEP index, browses each staged LAZ dir, and selects tiles whose UTM-named grid
    cell covers bbox. Non-UTM-named projects (state-plane, sequential ids) can't be
    decoded by name and are logged + skipped (fetch those via TNM)."""
    cells, zone, epsg = _covering_cells(bbox)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    tiles, seen = [], set()
    for project, laz_dir in _lidar_projects_covering(bbox, cfg):
        laz_url = f"{laz_dir}/LAZ/"
        try:
            listing = with_retries(lambda u=laz_url: requests.get(u, timeout=90).text)
        except Exception as ex:  # noqa: BLE001
            print(f"    {project}: LAZ dir unreachable ({type(ex).__name__}) — skip.")
            continue
        matched = 0
        for fn_stem, ztile, code in {m for m in _TILE_RE.findall(listing)}:
            if int(ztile) != zone or code not in cells or fn_stem in seen:
                continue
            seen.add(fn_stem)
            matched += 1
            e_km, n_km = cells[code]
            e0, n0 = e_km * 1000, n_km * 1000
            lons, lats = inv.transform([e0, e0 + 1000], [n0, n0 + 1000])
            tiles.append({
                "downloadURL": f"{laz_url}{fn_stem}.laz",
                "title": f"USGS Lidar Point Cloud {project} {fn_stem.split('_')[-1]}",
                "sizeInBytes": 0,
                "boundingBox": {"minX": min(lons), "maxX": max(lons),
                                "minY": min(lats), "maxY": max(lats)},
            })
        if matched:
            print(f"    {project}: {matched} covering tiles (UTM zone {zone}).")
        else:
            why = ("non-UTM tile naming — skip (use TNM for this one)"
                   if ".laz" in listing else "no LAZ found")
            print(f"    {project}: 0 covering tiles ({why}).")
    return tiles


# ── Best-quality collection mosaic ────────────────────────────────────────────

def _collection_of(tile: dict) -> str:
    """Collection = the staged '/Projects/<NAME>/' segment of the download URL."""
    m = re.search(r"/Projects/([^/]+)/", tile.get("downloadURL", ""))
    return m.group(1) if m else tile.get("title", "unknown")


def _tile_bbox(tile: dict):
    bb = tile.get("boundingBox") or {}
    if all(k in bb for k in ("minX", "minY", "maxX", "maxY")):
        return (bb["minX"], bb["minY"], bb["maxX"], bb["maxY"])
    return None


def _collection_density(ctiles: list[dict]) -> float:
    """Density proxy = median file bytes per km² (what actually drives RANSAC)."""
    vals = []
    for t in ctiles:
        bb, sz = _tile_bbox(t), (t.get("sizeInBytes") or 0)
        if not bb or sz <= 0:
            continue
        x0, y0, x1, y1 = bb
        lat = (y0 + y1) / 2
        area = abs((x1 - x0) * 111.32 * math.cos(math.radians(lat)) * (y1 - y0) * 110.54)
        if area > 0:
            vals.append(sz / area)
    return float(np.median(vals)) if vals else 0.0


def _rank_key(name: str, ctiles: list[dict]):
    """Rank collections by density, then recency, then QL hint (all descending)."""
    years = [int(m.group(1)) for t in ctiles
             for m in [re.search(r"(20\d\d)", t.get("publicationDate", ""))] if m]
    ql = 2 if "QL1" in name else (1 if "QL2" in name else 0)
    return (_collection_density(ctiles), max(years) if years else 0, ql)


def select_collections(tiles: list[dict], bbox, cell_deg: float = 0.01) -> list[dict]:
    """Choose a best-quality LiDAR mosaic: rank collections by density/recency, then
    greedily assign each ~1 km cell to the highest-ranked collection covering it,
    filling gaps with lower-ranked ones. Returns the tiles needed for that mosaic."""
    colls: dict = {}
    for t in tiles:
        colls.setdefault(_collection_of(t), []).append(t)

    if len(colls) == 1:
        name = next(iter(colls))
        print(f"  1 collection covers this region: {name} ({len(tiles)} tiles).")
        return tiles

    ranked = sorted(colls.items(), key=lambda kv: _rank_key(*kv), reverse=True)
    print(f"  {len(colls)} overlapping collections — ranking by density/recency:")
    for name, ct in ranked:
        print(f"    {name}: ~{_collection_density(ct) / 1e3:.0f} KB/km², {len(ct)} tiles")

    w, s, e, n = bbox
    xs, ys = np.arange(w, e, cell_deg), np.arange(s, n, cell_deg)
    cells = [(x + cell_deg / 2, y + cell_deg / 2) for x in xs for y in ys] \
        or [((w + e) / 2, (s + n) / 2)]
    covered = [False] * len(cells)
    chosen, chosen_urls, usage = [], set(), {}

    for name, ct in ranked:
        boxes = [(_tile_bbox(t), t) for t in ct]
        for ci, (cx, cy) in enumerate(cells):
            if covered[ci]:
                continue
            for bb, t in boxes:
                if bb and bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]:
                    covered[ci] = True
                    usage[name] = usage.get(name, 0) + 1
                    if t["downloadURL"] not in chosen_urls:
                        chosen_urls.add(t["downloadURL"])
                        chosen.append(t)
                    break

    total = len(cells)
    print("  Selected mosaic:")
    for name, _ct in ranked:
        if usage.get(name):
            print(f"    {name}: {100 * usage[name] / total:.0f}% of area")
    dropped = [name for name, _ct in ranked if not usage.get(name)]
    if dropped:
        print(f"    dropped (redundant / lower quality): {', '.join(dropped)}")
    return chosen


def discover_tiles(bbox, cfg: Config) -> list[dict]:
    """Full discovery for a region: TNM (with rockyweb fallback), then collection
    selection (or a forced ``preferred_collection``), honoring ``max_tiles``."""
    tiles = with_retries(find_all_tiles, bbox, cfg)
    if not tiles:
        print("  TNM returned 0 (spatial query down) — falling back to rockyweb...")
        tiles = with_retries(find_tiles_rockyweb, bbox, cfg)
        print(f"  rockyweb found {len(tiles)} tiles.")
    if not tiles:
        return []
    print(f"  {len(tiles)} tiles cover this region.")

    if cfg.preferred_collection:
        tiles = [t for t in tiles if cfg.preferred_collection in _collection_of(t)]
        print(f"  Forced collection '{cfg.preferred_collection}': {len(tiles)} tiles.")
    else:
        tiles = select_collections(tiles, bbox)

    if cfg.max_tiles is not None:
        tiles = tiles[:cfg.max_tiles]
        print(f"  max_tiles set — processing only the first {len(tiles)} this run.")
    return tiles


# ── Download + read ───────────────────────────────────────────────────────────

def download_tile(download_url: str, cache_dir: str) -> str:
    """Download a tile to ``cache_dir`` (streamed), skipping if already present."""
    os.makedirs(cache_dir, exist_ok=True)
    filename = download_url.split("/")[-1].split("?")[0]
    local_path = os.path.join(cache_dir, filename)
    if os.path.exists(local_path):
        return local_path

    r = requests.get(download_url, stream=True, timeout=600)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    return local_path


def get_tile_srs(tile_path: str) -> str:
    """Read the tile's spatial reference (WKT) from its LAS header."""
    import pdal  # lazy: PDAL is a system/conda dependency

    p = pdal.Pipeline(json.dumps({"pipeline": [
        {"type": "readers.las", "filename": tile_path.replace("\\", "/"), "count": 1}
    ]}))
    p.execute()
    md = p.metadata["metadata"]["readers.las"]
    return md.get("comp_spatialreference") or md.get("spatialreference")


def load_tile_points(tile_path: str) -> TilePoints:
    """Read the entire tile into memory once as (points, X, Y) float views. Reading
    once and masking per-building in RAM is ~100× faster than re-opening the file."""
    import pdal  # lazy

    p = pdal.Pipeline(json.dumps({"pipeline": [
        {"type": "readers.las", "filename": tile_path.replace("\\", "/")}
    ]}))
    p.execute()
    pts = p.arrays[0]
    return pts, np.asarray(pts["X"], dtype=float), np.asarray(pts["Y"], dtype=float)
