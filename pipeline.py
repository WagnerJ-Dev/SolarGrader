"""
Solar Grader — Streaming Multi-Tile Pipeline
Processes every LiDAR tile covering a region, one at a time, accumulating scored
homes into DuckDB. Streaming + tile deletion keeps disk bounded; a tiles_done
table makes the run resumable (re-running skips finished tiles).

Reuses the VALIDATED scoring functions from test_pipeline.py unchanged — only the
orchestration (tile discovery, accumulate, resume, cleanup) is new here.

Run with (targets accumulate into one DB; every run is resumable):
    source .venv/bin/activate
    python pipeline.py --region harrisburg          # a named area
    python pipeline.py --county Dauphin,Cumberland  # one or more counties (gridded)
    python pipeline.py --region harrisburg-metro    # the whole metro in one command
    python pipeline.py --bbox -77.0 40.2 -76.8 40.4 # an explicit lon/lat box
    python pipeline.py --list                        # show named regions
    python pipeline.py                               # default REGION_BBOX (back-compat)
"""

import gzip
import json
import math
import os
import re
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed

import duckdb
import numpy as np
import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

# The scoring algorithm — imported as-is from the validated test pipeline
from test_pipeline import (
    download_tile,
    get_tile_srs,
    load_tile_points,
    get_buildings_from_osm,
    get_tmy_data,
    clip_lidar_to_building,
    extract_roof_planes,
    compute_horizon,
    calculate_annual_kwh,
    grade_home,
    grade_potential,
    save_result,
)

warnings.filterwarnings("ignore")

# ── Configuration ─────────────────────────────────────────────────────────────

# Current target: Harrisburg, PA + near suburbs (~56 tiles, ~1 hr; resumable).
# Outside Chester County, so BUILDING_SOURCE must be "ms" (national footprints).
REGION_BBOX = (-76.930, 40.240, -76.830, 40.310)  # (west, south, east, north)
# Prior target — West Chester: (-75.620, 39.945, -75.595, 39.970) with source "county".

# TNM serves overlapping LiDAR surveys of wildly different vintage/quality for the
# same area; mixing them corrupts scores. Leave this None to AUTO-SELECT the best
# collection per location (density-ranked greedy mosaic — see select_collections()).
# Set to a name substring (e.g. "PA_17County_D24") to force one collection manually.
PREFERRED_COLLECTION = None

# Cap tiles for a fast first validation; set to None to process the whole region.
MAX_TILES = None

# Whole county/state: set to a large bbox to process it as a grid of sub-regions
# (resumable per sub-region AND per tile). None = a single REGION_BBOX run.
WHOLE_REGION_BBOX = None
STEP_DEG = 0.05   # sub-region size (~5 km) when iterating a whole region

# Building inventory source: "ms" (Microsoft, national — required outside Chester),
# "county" (Chester ArcGIS footprints), or "osm" (Overpass — prototyping only).
BUILDING_SOURCE = "ms"
COUNTY_FOOTPRINTS = (
    "https://mapservices.pasda.psu.edu/server/rest/services/"
    "pasda/ChesterCounty/MapServer/14/query"
)

# Microsoft Building Footprints (national, ODbL) — the scale-out source for areas
# without a county service and for other states. Quadkey-partitioned GeoJSONL:
# download the covering quadkey file once, then filter per tile. See DATA_SOURCES.md.
MS_DATASET_LINKS = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
MS_CACHE_DIR = "ms_cache"
MS_ZOOM = 9

# Fallback tile discovery for when TNM's product query is down. Region-agnostic:
# the USGS 3DEP index (below) reports which lidar PROJECT(s) cover a bbox anywhere
# in the US, each with its staged rockyweb LAZ directory (lpc_link). We browse that
# directory and select covering tiles by decoding the UTM grid cell embedded in
# tile filenames (USGS_LPC_..._<zz><MGRS><EEE><NNN>.laz). Projects that name tiles
# some other way (state-plane, sequential ids) can't be spatially selected by name
# — those are logged and skipped; the primary TNM path (naming-agnostic) still gets
# them. No project name is hardcoded here anymore.
INDEX_LPC = ("https://index.nationalmap.gov/arcgis/rest/services/"
             "3DEPElevationIndex/MapServer/8/query")

DB_PATH = "solar_grader.duckdb"     # accumulating production DB (separate from the test)
TILE_CACHE_DIR = "tile_stream"      # tiles downloaded here, then deleted after processing
DELETE_TILES_AFTER = True           # the storage-capping mechanic; resume covers re-runs

# Process-level parallelism: tiles are independent, so score N of them at once in
# separate processes. Separate processes (not threads) sidestep the GIL for the
# RANSAC/pvlib CPU work, and each worker deletes its own tile after scoring, so peak
# RAM/disk stays ~N_WORKERS tiles — the streaming/bounded-storage design is intact.
# The single DuckDB writer stays in the main process (DuckDB is single-writer);
# workers return compact results and never touch the DB. Tune down if RAM is tight.
N_WORKERS = min(4, (os.cpu_count() or 2))

# Required data attribution — show wherever results are displayed. See DATA_SOURCES.md.
ATTRIBUTION = ("Data: USGS 3DEP · Building footprints © Microsoft (ODbL) · "
               "© OpenStreetMap contributors · Chester County GIS/PASDA · EU PVGIS")


# ── Tile discovery ────────────────────────────────────────────────────────────

def find_all_tiles(bbox, page=50):
    """Query USGS TNM for ALL LiDAR tiles covering bbox (paginated, de-duped)."""
    west, south, east, north = bbox
    url = "https://tnmaccess.nationalmap.gov/api/v1/products"
    tiles, offset = {}, 0
    while True:
        params = {
            "datasets": "Lidar Point Cloud (LPC)",
            "bbox": f"{west},{south},{east},{north}",
            "prodFormats": "LAZ",
            "max": page,
            "offset": offset,
        }
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        for it in items:
            dl = it.get("downloadURL")
            if dl:
                tiles[dl] = it
        offset += len(items)
        if not items or offset >= data.get("total", 0):
            break
    return list(tiles.values())


def tile_id_of(tile):
    """Stable id for a tile = its LAZ filename."""
    return tile["downloadURL"].split("/")[-1].split("?")[0]


def _covering_cells(bbox):
    """UTM 1 km cells covering bbox, in the bbox's own UTM zone. Returns
    {code -> (easting_km, northing_km)} with the zone and its EPSG, where `code` is
    the 6-digit 'EEENNN' (easting km, northing km mod 1000) that USGS embeds in tile
    filenames. Zone is derived from the bbox, so this works in any UTM zone."""
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


def _lidar_projects_covering(bbox):
    """Region-agnostic: the USGS 3DEP index → [(project, staged LAZ dir URL)] for
    every lidar project whose extent intersects bbox, anywhere in the US."""
    w, s, e, n = bbox
    data = with_retries(lambda: requests.get(
        INDEX_LPC,
        params={"geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                "outFields": "project,lpc_link", "returnGeometry": "false", "f": "json"},
        timeout=60).json())
    out = []
    for f in data.get("features", []):
        a = f.get("attributes", {})
        link = (a.get("lpc_link") or "").rstrip("/")
        if link:
            out.append((a.get("project") or link.split("/")[-1], link))
    return out


# USGS LPC filename with a UTM/MGRS grid tail: ..._<zz><MGRS-3-letters><EEENNN>.laz
_TILE_RE = re.compile(r"(USGS_LPC_[\w.\-]+?_(\d{2})[A-Z]{3}(\d{6}))\.laz")


def find_tiles_rockyweb(bbox):
    """Fallback tile discovery when TNM's product query is down. Discovers covering
    projects via the 3DEP index (any US region — no hardcoded project), browses each
    project's staged LAZ dir, and selects tiles whose UTM-named grid cell covers
    bbox. Projects that name tiles non-spatially (state-plane, sequential ids) can't
    be decoded here and are logged + skipped (fetch those via the primary TNM path)."""
    cells, zone, epsg = _covering_cells(bbox)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    tiles, seen = [], set()
    for project, laz_dir in _lidar_projects_covering(bbox):
        laz_url = f"{laz_dir}/LAZ/"
        try:
            listing = with_retries(lambda: requests.get(laz_url, timeout=90).text)
        except Exception as ex:
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


# ── Building inventory sources ────────────────────────────────────────────────

def get_buildings_from_county(bbox):
    """Chester County building footprints (PASDA MapServer layer 14), bbox-queried.
    Returns the same dict shape as get_buildings_from_osm so scoring is unchanged.
    Uses an id-only query then batched geometry fetches (robust to the 1000 cap)."""
    west, south, east, north = bbox
    geom = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
    }
    print("  Querying county building footprints...")
    ids = requests.get(
        COUNTY_FOOTPRINTS,
        params={**geom, "where": "1=1", "returnIdsOnly": "true", "f": "json"},
        timeout=90,
    ).json().get("objectIds") or []

    buildings = []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        resp = requests.post(
            COUNTY_FOOTPRINTS,
            data={"objectIds": ",".join(map(str, batch)), "outFields": "OBJECTID",
                  "returnGeometry": "true", "outSR": "4326", "f": "json"},
            timeout=90,
        )
        resp.raise_for_status()
        for f in resp.json().get("features", []):
            rings = (f.get("geometry") or {}).get("rings")
            if not rings:
                continue
            try:
                poly = Polygon(rings[0])  # exterior ring
                if not poly.is_valid or poly.area == 0:
                    continue
            except Exception:
                continue
            cx, cy = poly.centroid.x, poly.centroid.y
            deg2m2 = (111320 ** 2) * math.cos(math.radians(cy))
            buildings.append({
                "osm_id": int(f["attributes"]["OBJECTID"]),  # unique building id
                "geometry": poly, "lat": cy, "lon": cx,
                "footprint_m2": poly.area * deg2m2,
            })
    print(f"  Found {len(buildings)} buildings.")
    return buildings


def _quadkey(lon, lat, z=MS_ZOOM):
    """Bing Maps quadkey for a lon/lat at zoom z (MS footprint partition scheme)."""
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    qk = ""
    for i in range(z, 0, -1):
        digit, mask = 0, 1 << (i - 1)
        if x & mask: digit += 1
        if y & mask: digit += 2
        qk += str(digit)
    return qk


def _ms_link_for(qk):
    """Look up the download URL for a US quadkey in MS's dataset index (cached)."""
    os.makedirs(MS_CACHE_DIR, exist_ok=True)
    links = os.path.join(MS_CACHE_DIR, "dataset-links.csv")
    if not os.path.exists(links):
        print("  Fetching MS dataset index (one-time)...")
        r = requests.get(MS_DATASET_LINKS, timeout=180)
        r.raise_for_status()
        with open(links, "w") as f:
            f.write(r.text)
    with open(links) as f:
        for line in f:
            p = line.split(",")
            if len(p) >= 3 and p[0] == "UnitedStates" and p[1] == qk:
                return p[2]
    return None


_MS_CACHE = {}  # quadkey -> (centroids ndarray, list-of-rings)


def _load_ms_quadkey(qk):
    """Download (once) and parse a quadkey's GeoJSONL into centroids + rings."""
    if qk in _MS_CACHE:
        return _MS_CACHE[qk]
    url = _ms_link_for(qk)
    if not url:
        _MS_CACHE[qk] = (np.empty((0, 2)), [])
        return _MS_CACHE[qk]
    path = os.path.join(MS_CACHE_DIR, f"{qk}.csv.gz")
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
            except Exception:
                continue
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))
            rings.append(coords)
    _MS_CACHE[qk] = (np.array(centroids), rings)
    print(f"  Loaded {len(rings):,} MS buildings for quadkey {qk}.")
    return _MS_CACHE[qk]


def get_buildings_from_ms(bbox):
    """Microsoft Building Footprints within bbox (national/other-states source).
    Returns the same dict shape as the other backends."""
    w, s, e, n = bbox
    quadkeys = {_quadkey(lo, la) for lo in (w, e) for la in (s, n)}
    buildings = []
    for qk in quadkeys:
        centroids, rings = _load_ms_quadkey(qk)
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
            except Exception:
                continue
            lon, lat = float(cx[idx]), float(cy[idx])
            deg2m2 = (111320 ** 2) * math.cos(math.radians(lat))
            buildings.append({
                "osm_id": qk_int * 10_000_000 + int(idx),  # stable unique id
                "geometry": poly, "lat": lat, "lon": lon,
                "footprint_m2": poly.area * deg2m2,
            })
    print(f"  Found {len(buildings)} buildings (MS footprints).")
    return buildings


def get_buildings(bbox):
    """Dispatch to the configured building source."""
    if BUILDING_SOURCE == "county":
        return get_buildings_from_county(bbox)
    if BUILDING_SOURCE == "ms":
        return get_buildings_from_ms(bbox)
    return get_buildings_from_osm(bbox)


# ── LiDAR collection picker (best-quality mosaic per region) ───────────────────

def _collection_of(tile):
    """Collection = the staged '/Projects/<NAME>/' segment of the download URL."""
    m = re.search(r"/Projects/([^/]+)/", tile.get("downloadURL", ""))
    return m.group(1) if m else tile.get("title", "unknown")


def _tile_bbox(tile):
    bb = tile.get("boundingBox") or {}
    if all(k in bb for k in ("minX", "minY", "maxX", "maxY")):
        return (bb["minX"], bb["minY"], bb["maxX"], bb["maxY"])
    return None


def _collection_density(ctiles):
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


def _rank_key(name, ctiles):
    """Rank collections by density, then recency, then QL hint (all descending)."""
    years = [int(m.group(1)) for t in ctiles
             for m in [re.search(r"(20\d\d)", t.get("publicationDate", ""))] if m]
    ql = 2 if "QL1" in name else (1 if "QL2" in name else 0)
    return (_collection_density(ctiles), max(years) if years else 0, ql)


def select_collections(tiles, bbox, cell_deg=0.01):
    """Choose a best-quality LiDAR mosaic: rank collections by density/recency, then
    greedily assign each ~1 km cell to the highest-ranked collection covering it,
    filling gaps with lower-ranked ones. Returns the tiles needed for that mosaic."""
    colls = {}
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
    for name, ct in ranked:
        if usage.get(name):
            print(f"    {name}: {100 * usage[name] / total:.0f}% of area")
    dropped = [name for name, ct in ranked if not usage.get(name)]
    if dropped:
        print(f"    dropped (redundant / lower quality): {', '.join(dropped)}")
    return chosen


def with_retries(fn, *args, attempts=4, base_delay=5, **kwargs):
    """Call fn with exponential backoff on transient failures (e.g. Overpass 504,
    PVGIS hiccups). Re-raises only after the final attempt."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if i == attempts - 1:
                raise
            wait = base_delay * (2 ** i)
            print(f"    {type(e).__name__}: {e}\n    retry {i + 1}/{attempts - 1} in {wait}s...")
            time.sleep(wait)


# ── Database (accumulating, resumable) ────────────────────────────────────────

def setup_db(path):
    con = duckdb.connect(path)
    # Same homes schema as the test pipeline, but NEVER dropped — we accumulate.
    con.execute("""
        CREATE TABLE IF NOT EXISTS homes (
            osm_id                BIGINT PRIMARY KEY,
            lat                   DOUBLE,
            lon                   DOUBLE,
            footprint_m2          DOUBLE,
            usable_roof_area_m2   DOUBLE,
            primary_tilt_deg      DOUBLE,
            primary_azimuth_deg   DOUBLE,
            roof_plane_count      INTEGER,
            res_annual_kwh        DOUBLE,
            res_system_kw         DOUBLE,
            res_panel_count       INTEGER,
            solar_score           INTEGER,
            solar_grade           VARCHAR(2),
            max_annual_kwh        DOUBLE,
            max_system_kw         DOUBLE,
            max_panel_count       INTEGER,
            potential_grade       VARCHAR(2),
            shade_loss_pct        DOUBLE,
            processed_at          TIMESTAMP
        )
    """)
    # Resume ledger: which tiles are already done.
    con.execute("""
        CREATE TABLE IF NOT EXISTS tiles_done (
            tile_id       VARCHAR PRIMARY KEY,
            n_scored      INTEGER,
            processed_at  TIMESTAMP
        )
    """)
    return con


# ── Per-building scoring (mirrors test_pipeline.main's inner loop) ─────────────

def score_building(building, tile_points, transformer, tmy):
    """Full scoring chain for one building. Returns a save_result payload or None."""
    points = clip_lidar_to_building(tile_points, building["geometry"], transformer)
    if points is None:
        return None
    planes = extract_roof_planes(points)
    if not planes:
        return None

    observer = points.mean(axis=0)
    own_footprint = shapely_transform(transformer.transform, building["geometry"]).buffer(1.0)
    horizon = compute_horizon(tile_points, observer, own_footprint)

    sysres = calculate_annual_kwh(building["lat"], building["lon"], tmy, planes, horizon)
    if sysres is None or sysres["res_panels"] == 0:
        return None

    best_plane = sysres["best_plane"]
    total_area = sum(p["area_m2"] for p in planes)
    grade, score = grade_home(
        sysres["res_kwh"], sysres["res_kw"], best_plane["azimuth_deg"]
    )
    return {
        "usable_area_m2": total_area,
        "primary_tilt": best_plane["tilt_deg"],
        "primary_azimuth": best_plane["azimuth_deg"],
        "plane_count": len(planes),
        "res_kwh": sysres["res_kwh"],
        "res_kw": sysres["res_kw"],
        "res_panels": sysres["res_panels"],
        "max_kwh": sysres["max_kwh"],
        "max_kw": sysres["max_kw"],
        "max_panels": sysres["max_panels"],
        "potential_grade": grade_potential(sysres["max_kw"]),
        "shade_loss_pct": sysres["shade_loss_pct"],
        "grade": grade,
        "score": score,
    }


def tile_latlon_bbox(tile_points, tile_srs):
    """Derive the tile's lon/lat bbox from its actual point extent (UTM → 4326)."""
    _, X, Y = tile_points
    inv = Transformer.from_crs(tile_srs, "EPSG:4326", always_xy=True)
    xs = [X.min(), X.max(), X.min(), X.max()]
    ys = [Y.min(), Y.min(), Y.max(), Y.max()]
    lons, lats = inv.transform(xs, ys)
    return (min(lons), min(lats), max(lons), max(lats))


# ── Parallel worker ───────────────────────────────────────────────────────────

def _process_tile(tile, tile_cache=TILE_CACHE_DIR):
    """Fully process ONE tile in a worker process: download, discover buildings,
    score each, then delete the tile file. Returns a compact, picklable result —
    no shapely geometry crosses the process boundary (save_result only needs
    osm_id/lat/lon/footprint). Never touches the DB; the parent does all writes.
    All failures are caught and returned so one bad tile can't kill the pool."""
    tid = tile_id_of(tile)
    try:
        path = download_tile(tile["downloadURL"], tile_cache)
        tile_points = load_tile_points(path)
        tile_srs = get_tile_srs(path)
        transformer = Transformer.from_crs("EPSG:4326", tile_srs, always_xy=True)

        tbbox = tile_latlon_bbox(tile_points, tile_srs)
        buildings = with_retries(get_buildings, tbbox)
        if not buildings:
            _cleanup(path)
            return {"tid": tid, "n_buildings": 0, "scored": [], "error": None}

        # One irradiance fetch per tile (its center) covers all its buildings
        cx = (tbbox[0] + tbbox[2]) / 2
        cy = (tbbox[1] + tbbox[3]) / 2
        tmy = with_retries(get_tmy_data, cy, cx)

        scored = []
        for building in buildings:
            result = score_building(building, tile_points, transformer, tmy)
            if result:
                meta = {k: building[k] for k in ("osm_id", "lat", "lon", "footprint_m2")}
                scored.append((meta, result))

        _cleanup(path)
        return {"tid": tid, "n_buildings": len(buildings), "scored": scored, "error": None}

    except Exception as e:
        # Return the failure instead of raising — keeps the other workers running.
        return {"tid": tid, "n_buildings": 0, "scored": [],
                "error": f"{type(e).__name__}: {e}"}


# ── Main streaming loop ───────────────────────────────────────────────────────

def run(bbox, db_path=DB_PATH, tile_cache=TILE_CACHE_DIR):
    print("=" * 60)
    print("SOLAR GRADER — Streaming Multi-Tile Pipeline")
    print(f"Region bbox: {bbox}")
    print("=" * 60)

    print("\nDiscovering LiDAR tiles...")
    all_tiles = with_retries(find_all_tiles, bbox)
    if not all_tiles:
        print("  TNM returned 0 (spatial query down) — falling back to direct")
        print("  rockyweb tile discovery...")
        all_tiles = with_retries(find_tiles_rockyweb, bbox)
        print(f"  rockyweb found {len(all_tiles)} tiles.")
    if not all_tiles:
        print("  ABORT: no tiles from TNM or rockyweb. Both may be down — retry later.")
        return
    print(f"  {len(all_tiles)} tiles cover this region.")
    if PREFERRED_COLLECTION:
        tiles = [t for t in all_tiles if PREFERRED_COLLECTION in _collection_of(t)]
        print(f"  Forced collection '{PREFERRED_COLLECTION}': {len(tiles)} tiles.")
    else:
        tiles = select_collections(all_tiles, bbox)
    if not tiles:
        print("  ABORT: no tiles selected for this region.")
        return
    if MAX_TILES is not None:
        tiles = tiles[:MAX_TILES]
        print(f"  MAX_TILES set — processing only the first {len(tiles)} this run.")

    con = setup_db(db_path)
    done = {row[0] for row in con.execute("SELECT tile_id FROM tiles_done").fetchall()}
    pending = [t for t in tiles if tile_id_of(t) not in done]
    if done:
        print(f"  {len(done)} already processed — resuming, will skip those.")
    print(f"\nScoring {len(pending)} tiles across {N_WORKERS} worker processes "
          f"(single DB writer in this process)...")

    # Dispatch each tile to a worker; the parent stays the sole DB writer, saving
    # results and marking tiles done as they stream back (order is non-deterministic).
    t_start = time.time()
    completed = 0
    with ProcessPoolExecutor(max_workers=N_WORKERS) as pool:
        futures = {pool.submit(_process_tile, t, tile_cache): tile_id_of(t)
                   for t in pending}
        for fut in as_completed(futures):
            completed += 1
            res = fut.result()   # worker catches its own errors; never raises here
            tid = res["tid"]
            if res["error"]:
                print(f"[{completed}/{len(pending)}] {tid} — ERROR: {res['error']} (skipped)")
                continue
            for meta, result in res["scored"]:
                save_result(con, meta, result)
            _mark_done(con, tid, len(res["scored"]))
            print(f"[{completed}/{len(pending)}] {tid} — "
                  f"scored {len(res['scored'])}/{res['n_buildings']}")

    _summary(con, time.time() - t_start)
    con.close()


def _mark_done(con, tid, n_scored):
    con.execute(
        "INSERT OR REPLACE INTO tiles_done VALUES (?, ?, ?)",
        [tid, n_scored, pd.Timestamp.now()],
    )


def _cleanup(path):
    if DELETE_TILES_AFTER and os.path.exists(path):
        os.remove(path)


def _summary(con, elapsed):
    print(f"\n\n{'=' * 60}")
    print("PIPELINE SUMMARY")
    print(f"{'=' * 60}")
    n_homes = con.execute("SELECT COUNT(*) FROM homes").fetchone()[0]
    n_tiles = con.execute("SELECT COUNT(*) FROM tiles_done").fetchone()[0]
    print(f"  Tiles processed:   {n_tiles}")
    print(f"  Homes scored:      {n_homes}")
    print(f"  Total time:        {elapsed:.1f}s")

    print(f"\n  Grade breakdown:")
    rows = con.execute("""
        SELECT solar_grade, COUNT(*) FROM homes GROUP BY solar_grade
    """).fetchall()
    counts = dict(rows)
    for g in ["A+", "A", "B+", "B", "C", "D"]:
        n = counts.get(g, 0)
        if n:
            print(f"    {g:2s}  {'█' * min(n, 50)}  ({n})")

    print(f"\n  Results saved to: {DB_PATH}")


def run_region(bbox, step_deg=STEP_DEG, db_path=DB_PATH, tile_cache=TILE_CACHE_DIR):
    """Process a whole county/state by tiling it into sub-region bboxes and running
    each through the normal pipeline. Resumable: a regions_done ledger skips finished
    sub-regions, and tiles_done (inside run) dedups tiles shared across sub-regions."""
    w, s, e, n = bbox
    nx = max(1, math.ceil((e - w) / step_deg))   # integer step counts avoid float drift
    ny = max(1, math.ceil((n - s) / step_deg))
    subs = []
    for ix in range(nx):
        x0, x1 = w + ix * step_deg, min(w + (ix + 1) * step_deg, e)
        for iy in range(ny):
            y0, y1 = s + iy * step_deg, min(s + (iy + 1) * step_deg, n)
            if x1 - x0 > 1e-9 and y1 - y0 > 1e-9:   # skip degenerate slivers
                subs.append((round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)))

    print("#" * 60)
    print(f"REGION RUN — {len(subs)} sub-regions (~{step_deg}°) over {bbox}")
    print("#" * 60)

    # regions_done ledger. Opened only BETWEEN run() calls so there's never a
    # second connection open while run() holds the DuckDB writer.
    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS regions_done "
                "(region_key VARCHAR PRIMARY KEY, processed_at TIMESTAMP)")
    done = {r[0] for r in con.execute("SELECT region_key FROM regions_done").fetchall()}
    con.close()
    if done:
        print(f"  Resuming — {len(done)} sub-regions already complete.")

    for i, sub in enumerate(subs, 1):
        key = ",".join(f"{v:.4f}" for v in sub)
        if key in done:
            print(f"\n### sub-region {i}/{len(subs)} {sub} — already done, skip.")
            continue
        print(f"\n### sub-region {i}/{len(subs)} {sub}")
        try:
            run(sub, db_path, tile_cache)
        except Exception as ex:
            print(f"### sub-region {i} ERROR: {type(ex).__name__}: {ex} — continuing.")
            continue
        con = duckdb.connect(db_path)
        con.execute("INSERT OR REPLACE INTO regions_done VALUES (?, ?)",
                    [key, pd.Timestamp.now()])
        con.close()

    con = duckdb.connect(db_path, read_only=True)
    total = con.execute("SELECT COUNT(*) FROM homes").fetchone()[0]
    con.close()
    print("\n" + "#" * 60)
    print(f"REGION RUN COMPLETE — {total} homes scored across {len(subs)} sub-regions.")
    print("#" * 60)


# ── Area targeting: name/county → bbox (so new runs need no source edits) ─────

# US Census TIGERweb county boundaries (free, US Gov). Resolves any county name to
# an exact lon/lat bbox — no hand-typed coordinates, scales to every US county.
TIGERWEB_COUNTIES = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/State_County/MapServer/1/query"
)
_STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}

# Convenience aliases for the Harrisburg region — type `--region <name>`. Each is
# either an explicit bbox or one/more counties resolved live via TIGERweb.
REGIONS = {
    "harrisburg":       {"bbox": (-76.930, 40.240, -76.830, 40.310)},  # city + near suburbs
    "dauphin":          {"counties": ["Dauphin"]},
    "cumberland":       {"counties": ["Cumberland"]},
    "perry":            {"counties": ["Perry"]},
    "york":             {"counties": ["York"]},
    "lancaster":        {"counties": ["Lancaster"]},
    "lebanon":          {"counties": ["Lebanon"]},
    # The whole Harrisburg metro in one command (each county is a resumable grid).
    "harrisburg-metro": {"counties": ["Dauphin", "Cumberland", "Perry"]},
}


def county_bbox(county, state="PA"):
    """Exact lon/lat bbox for a US county via TIGERweb. `county` is the bare name
    (e.g. 'Dauphin', not 'Dauphin County'). Validated against a strict whitelist
    before it reaches the REST where-clause (trust-boundary hygiene)."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{0,48}", county or ""):
        raise ValueError(f"Invalid county name: {county!r}")
    fips = _STATE_FIPS.get(state.upper())
    if not fips:
        raise ValueError(f"Unknown state: {state!r}")
    r = with_retries(lambda: requests.get(
        TIGERWEB_COUNTIES,
        params={"where": f"BASENAME='{county}' AND STATE='{fips}'",
                "outFields": "NAME", "returnGeometry": "true",
                "outSR": "4326", "f": "json"},
        timeout=60,
    ).json())
    feats = r.get("features") or []
    if not feats:
        raise ValueError(f"No county '{county}' found in {state.upper()}.")
    rings = feats[0]["geometry"]["rings"]
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    return (min(xs), min(ys), max(xs), max(ys)), feats[0]["attributes"]["NAME"]


def all_counties(state="PA"):
    """Every county name in a state, alphabetized, via TIGERweb. Powers whole-state
    runs — each county then runs as its own resumable gridded region."""
    fips = _STATE_FIPS.get(state.upper())
    if not fips:
        raise ValueError(f"Unknown state: {state!r}")
    data = with_retries(lambda: requests.get(
        TIGERWEB_COUNTIES,
        params={"where": f"STATE='{fips}'", "outFields": "BASENAME",
                "returnGeometry": "false", "orderByFields": "BASENAME", "f": "json"},
        timeout=90).json())
    return [f["attributes"]["BASENAME"] for f in data.get("features", [])]


def _targets_from_args(args):
    """Resolve CLI args into a list of (label, bbox, use_grid) run targets. All
    targets accumulate into the same DB; the ledgers dedup any overlap."""
    if getattr(args, "all_counties", False):
        names = all_counties(args.state)
        print(f"Resolving {len(names)} counties in {args.state.upper()}...")
        targets = []
        for name in names:
            bbox, label = county_bbox(name, args.state)
            targets.append((label, bbox, True))   # each county is a gridded run
        return targets
    if args.bbox:
        return [("bbox", tuple(args.bbox), args.grid)]
    if args.county:
        names = [c.strip() for c in args.county.split(",") if c.strip()]
        out = []
        for name in names:
            bbox, label = county_bbox(name, args.state)
            out.append((label, bbox, True))   # a county is always a gridded run
        return out
    if args.region:
        spec = REGIONS.get(args.region.lower())
        if not spec:
            raise SystemExit(f"Unknown region '{args.region}'. "
                             f"Options: {', '.join(sorted(REGIONS))}")
        if "bbox" in spec:
            return [(args.region, spec["bbox"], args.grid)]
        out = []
        for name in spec["counties"]:
            bbox, label = county_bbox(name, spec.get("state", "PA"))
            out.append((label, bbox, True))
        return out
    # No target given — preserve the original default behaviour.
    if WHOLE_REGION_BBOX:
        return [("default-region", WHOLE_REGION_BBOX, True)]
    return [("default", REGION_BBOX, False)]


def main_cli():
    import argparse
    p = argparse.ArgumentParser(
        description="Solar Grader — score a region's rooftops. Targets accumulate "
                    "into one DB; every run is resumable (Ctrl-C and re-run).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--county", metavar="A,B,C",
                   help="one or more county names (comma-separated), e.g. Dauphin,Cumberland")
    g.add_argument("--region", metavar="NAME",
                   help=f"a named area: {', '.join(sorted(REGIONS))}")
    g.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                   help="explicit lon/lat bounding box")
    g.add_argument("--all-counties", action="store_true",
                   help="every county in --state — a whole-state run (long, but "
                        "fully resumable county-by-county and tile-by-tile)")
    p.add_argument("--state", default="PA", help="state for --county / --all-counties (default PA)")
    p.add_argument("--grid", action="store_true",
                   help="force gridded run_region for a --bbox/--region (auto-on for counties)")
    p.add_argument("--step", type=float, default=STEP_DEG,
                   help=f"sub-region size in degrees when gridding (default {STEP_DEG})")
    p.add_argument("--db", default=DB_PATH, help=f"DuckDB path (default {DB_PATH})")
    p.add_argument("--list", action="store_true", help="list named regions and exit")
    args = p.parse_args()

    if args.list:
        print("Named regions (--region):")
        for name, spec in sorted(REGIONS.items()):
            what = spec.get("bbox") or "counties: " + ", ".join(spec["counties"])
            print(f"  {name:18s} {what}")
        return

    targets = _targets_from_args(args)
    print(f"Targets to process ({len(targets)}), all into {args.db}:")
    for label, bbox, grid in targets:
        print(f"  • {label:16s} {tuple(round(v, 3) for v in bbox)}  "
              f"{'[grid]' if grid else '[single]'}")

    for label, bbox, grid in targets:
        print(f"\n{'=' * 60}\n▶ {label}\n{'=' * 60}")
        if grid:
            run_region(bbox, step_deg=args.step, db_path=args.db)
        else:
            run(bbox, db_path=args.db)


if __name__ == "__main__":
    main_cli()
