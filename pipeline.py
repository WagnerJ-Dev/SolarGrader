"""
Solar Grader — Streaming Multi-Tile Pipeline
Processes every LiDAR tile covering a region, one at a time, accumulating scored
homes into DuckDB. Streaming + tile deletion keeps disk bounded; a tiles_done
table makes the run resumable (re-running skips finished tiles).

Reuses the VALIDATED scoring functions from test_pipeline.py unchanged — only the
orchestration (tile discovery, accumulate, resume, cleanup) is new here.

Run with:
    source .venv/bin/activate
    python pipeline.py
"""

import gzip
import json
import math
import os
import re
import time
import warnings

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

# First validation target: West Chester, PA.
# Widen this to a county once the streaming/resume mechanics are confirmed.
REGION_BBOX = (-75.620, 39.945, -75.595, 39.970)  # (west, south, east, north)

# CRITICAL: TNM serves overlapping LiDAR surveys of wildly different vintage/quality
# for the same area. Pin to ONE modern, high-density collection so results are
# consistent — mixing a 2024 survey with 2006 data corrupts the scores. Tiles whose
# title doesn't contain this string are dropped. (PA_17County_D24 = 2024, ~35 pts/m²,
# covers the Chester County rollout. Full-PA would map the best collection per region.)
PREFERRED_COLLECTION = "PA_17County_D24"

# Cap tiles for a fast first validation; set to None to process the whole region.
MAX_TILES = None

# Building inventory source: "county" (Chester ArcGIS footprints — most complete
# here, no Overpass rate limits) or "osm" (Overpass — incomplete, prototyping only).
BUILDING_SOURCE = "county"
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

# Fallback tile discovery when TNM's spatial query is down: browse the project's
# staged LAZ directories on rockyweb and select tiles by the bbox→tile-name decode.
# (Tile name USGS_LPC_..._18SVK<EEE><NNN> = UTM 18N easting EEE*1000, northing
# 4,000,000 + NNN*1000. Verified against known tiles.) Scoped to this project.
ROCKYWEB_PROJECT = (
    "https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/"
    "Projects/PA_17County_D24"
)

DB_PATH = "solar_grader.duckdb"     # accumulating production DB (separate from the test)
TILE_CACHE_DIR = "tile_stream"      # tiles downloaded here, then deleted after processing
DELETE_TILES_AFTER = True           # the storage-capping mechanic; resume covers re-runs

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


def _covering_tile_codes(bbox):
    """The set of 6-digit tile codes (EEENNN) whose 1 km UTM cells cover bbox."""
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:26918", always_xy=True)  # UTM 18N
    w, s, e, n = bbox
    xs, ys = fwd.transform([w, e, w, e], [s, s, n, n])
    e_range = range(int(min(xs) // 1000), int(max(xs) // 1000) + 1)
    n_range = range(int((min(ys) - 4_000_000) // 1000), int((max(ys) - 4_000_000) // 1000) + 1)
    return {f"{ee}{nn}" for ee in e_range for nn in n_range}


def find_tiles_rockyweb(bbox):
    """Fallback for when TNM is down: list the project's staged LAZ dirs on
    rockyweb and pick tiles covering bbox via the tile-name UTM decode."""
    codes = _covering_tile_codes(bbox)
    index = requests.get(f"{ROCKYWEB_PROJECT}/", timeout=60).text
    subprojects = sorted(set(re.findall(r'href="(PA_17Co_\d+_D24)/"', index)))

    tiles, seen = [], set()
    for sub in subprojects:
        laz_url = f"{ROCKYWEB_PROJECT}/{sub}/LAZ/"
        listing = requests.get(laz_url, timeout=90).text
        for fn in re.findall(r"USGS_LPC_PA_17County_D24_18[A-Z]{3}\d{6}\.laz", listing):
            code = fn[-10:-4]  # the 6 digits before ".laz"
            if code in codes and fn not in seen:
                seen.add(fn)
                mgrs = fn[:-4].split("_")[-1]
                tiles.append({
                    "downloadURL": laz_url + fn,
                    "title": f"USGS Lidar Point Cloud PA_17County_D24 {mgrs}",
                    "sizeInBytes": 0,
                })
        if len(seen) >= len(codes):
            break  # found every covering tile
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
        sysres["res_kwh"], total_area, best_plane["azimuth_deg"], sysres["shade_loss_pct"]
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
    tiles = [t for t in all_tiles if PREFERRED_COLLECTION in t.get("title", "")]
    dropped = len(all_tiles) - len(tiles)
    print(f"  {len(all_tiles)} tiles cover this region; {len(tiles)} match "
          f"'{PREFERRED_COLLECTION}', {dropped} other-collection tiles dropped.")
    if not tiles:
        print(f"  ABORT: {len(all_tiles)} tiles found but none match "
              f"'{PREFERRED_COLLECTION}' — refusing to mix collection qualities.")
        return
    if MAX_TILES is not None:
        tiles = tiles[:MAX_TILES]
        print(f"  MAX_TILES set — processing only the first {len(tiles)} this run.")

    con = setup_db(db_path)
    done = {row[0] for row in con.execute("SELECT tile_id FROM tiles_done").fetchall()}
    if done:
        print(f"  {len(done)} already processed — resuming, will skip those.")

    t_start = time.time()
    for i, tile in enumerate(tiles, 1):
        tid = tile_id_of(tile)
        if tid in done:
            print(f"\n[tile {i}/{len(tiles)}] {tid}  (skip — already done)")
            continue

        print(f"\n[tile {i}/{len(tiles)}] {tid}")
        try:
            path = download_tile(tile["downloadURL"], tile_cache)
            tile_points = load_tile_points(path)
            tile_srs = get_tile_srs(path)
            transformer = Transformer.from_crs("EPSG:4326", tile_srs, always_xy=True)

            tbbox = tile_latlon_bbox(tile_points, tile_srs)
            buildings = with_retries(get_buildings, tbbox)
            if not buildings:
                _mark_done(con, tid, 0)
                _cleanup(path)
                continue

            # One irradiance fetch per tile (its center) covers all its buildings
            cx = (tbbox[0] + tbbox[2]) / 2
            cy = (tbbox[1] + tbbox[3]) / 2
            tmy = with_retries(get_tmy_data, cy, cx)

            n_scored = 0
            for j, building in enumerate(buildings):
                print(f"\r  scoring {j+1}/{len(buildings)}  (scored={n_scored})  ", end="")
                result = score_building(building, tile_points, transformer, tmy)
                if result:
                    save_result(con, building, result)
                    n_scored += 1
            print(f"\r  scored {n_scored} of {len(buildings)} buildings in tile.        ")

            _mark_done(con, tid, n_scored)
            _cleanup(path)

        except Exception as e:
            # Isolate failures — one bad tile must not kill the whole run
            print(f"\n  ERROR on {tid}: {type(e).__name__}: {e} — skipping tile.")
            continue

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


if __name__ == "__main__":
    run(REGION_BBOX)
