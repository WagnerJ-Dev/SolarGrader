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

import os
import time
import warnings

import duckdb
import pandas as pd
import requests
from pyproj import Transformer
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

DB_PATH = "solar_grader.duckdb"     # accumulating production DB (separate from the test)
TILE_CACHE_DIR = "tile_stream"      # tiles downloaded here, then deleted after processing
DELETE_TILES_AFTER = True           # the storage-capping mechanic; resume covers re-runs


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
    all_tiles = find_all_tiles(bbox)
    tiles = [t for t in all_tiles if PREFERRED_COLLECTION in t.get("title", "")]
    dropped = len(all_tiles) - len(tiles)
    print(f"  {len(all_tiles)} tiles cover this region; {len(tiles)} match "
          f"'{PREFERRED_COLLECTION}', {dropped} other-collection tiles dropped.")
    if not tiles:
        print("  ABORT: no tiles match the preferred collection — refusing to mix qualities.")
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
            buildings = with_retries(get_buildings_from_osm, tbbox)
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
