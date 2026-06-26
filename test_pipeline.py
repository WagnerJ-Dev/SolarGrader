"""
Solar Grader — Single Tile Pipeline Test
Tests the full pipeline on one LiDAR tile from Chester County, PA.

Run with:
    conda activate solar-grader
    python test_pipeline.py
"""

import json
import os
import time
import warnings

import duckdb
import numpy as np
import open3d as o3d
import pandas as pd
import pdal
import pvlib
import requests
from scipy.spatial import ConvexHull
from shapely.geometry import Polygon

warnings.filterwarnings("ignore")

# ── Configuration ─────────────────────────────────────────────────────────────

# Small test area: suburban Chester County, PA (near Exton)
# Adjust this bbox to pick a different area — keep it small (0.02° × 0.02° max)
TEST_BBOX = (-75.640, 40.015, -75.620, 40.030)  # (west, south, east, north)
TEST_LAT = 40.022
TEST_LON = -75.630

TILE_CACHE_DIR = "tile_cache"   # LiDAR tiles cached here (re-used on re-runs)
DB_PATH = "solar_results.duckdb"

MIN_ROOF_POINTS = 20            # skip buildings with fewer LiDAR points than this
RANSAC_ITERATIONS = 300         # lower = faster, higher = more accurate
RANSAC_DISTANCE_THRESHOLD = 0.20  # meters; how far a point can be from a plane


# ── Step 1: Find & download a LiDAR tile ─────────────────────────────────────

def find_lidar_tile(bbox):
    """Query USGS TNM API for a LiDAR tile covering the bounding box."""
    west, south, east, north = bbox
    url = "https://tnmaccess.nationalmap.gov/api/v1/products"
    params = {
        "datasets": "Lidar Point Cloud (LPC)",
        "bbox": f"{west},{south},{east},{north}",
        "prodFormats": "LAZ",
        "max": 5,
    }
    print(f"  Querying USGS TNM API...")
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    items = r.json().get("items", [])
    if not items:
        print("  No tiles found — widening search area...")
        expand = 0.05
        params["bbox"] = f"{west-expand},{south-expand},{east+expand},{north+expand}"
        r = requests.get(url, params=params, timeout=30)
        items = r.json().get("items", [])

    if not items:
        raise RuntimeError(
            "No LiDAR tiles found. Check https://apps.nationalmap.gov/downloader/ "
            "manually for PA coverage."
        )

    tile = items[0]
    size_mb = tile.get("sizeInBytes", 0) / 1e6
    print(f"  Found: {tile['title']}")
    print(f"  Size:  {size_mb:.0f} MB")
    if size_mb > 300:
        print("  WARNING: large tile — download may take a few minutes.")
    return tile


def download_tile(download_url, cache_dir):
    """Download a tile to cache_dir, skipping if already present."""
    os.makedirs(cache_dir, exist_ok=True)
    filename = download_url.split("/")[-1].split("?")[0]
    local_path = os.path.join(cache_dir, filename)

    if os.path.exists(local_path):
        print(f"  Using cached tile: {local_path}")
        return local_path

    print(f"  Downloading to {local_path} ...")
    r = requests.get(download_url, stream=True, timeout=600)
    r.raise_for_status()

    total = int(r.headers.get("content-length", 0))
    downloaded = 0
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                print(
                    f"\r  {downloaded / 1e6:.1f} / {total / 1e6:.1f} MB "
                    f"({downloaded / total * 100:.0f}%)",
                    end="",
                )
    print(f"\n  Done.")
    return local_path


# ── Step 2: Get building footprints from OpenStreetMap ────────────────────────

def get_buildings_from_osm(bbox):
    """
    Fetch building polygons from Overpass API.
    Returns a list of dicts with geometry and metadata.
    """
    west, south, east, north = bbox
    # Overpass bbox order is: south, west, north, east
    query = f"""
[out:json][timeout:60];
(
  way["building"]({south},{west},{north},{east});
);
out geom;
"""
    print("  Querying Overpass API for buildings...")
    r = requests.post(
        "https://overpass-api.de/api/interpreter",
        data={"data": query},
        timeout=90,
    )
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
            cx, cy = poly.centroid.x, poly.centroid.y
            # Rough m² from degrees: 1° lat ≈ 111,320m; 1° lon ≈ 111,320 × cos(lat)
            deg2m2 = (111320 ** 2) * np.cos(np.radians(cy))
            buildings.append(
                {
                    "osm_id": el["id"],
                    "geometry": poly,
                    "lat": cy,
                    "lon": cx,
                    "footprint_m2": poly.area * deg2m2,
                }
            )
        except Exception:
            pass

    print(f"  Found {len(buildings)} buildings.")
    return buildings


# ── Step 3: Get solar irradiance data (PVGIS, free, no API key) ───────────────

def get_tmy_data(lat, lon):
    """
    Fetch a Typical Meteorological Year from the EU PVGIS service.
    Free, no API key required, covers the US.
    Returns a DataFrame with hourly irradiance columns: ghi, dni, dhi.
    """
    print(f"  Fetching TMY data from PVGIS for ({lat:.3f}, {lon:.3f})...")
    tmy, _, _, _ = pvlib.iotools.get_pvgis_tmy(
        latitude=lat,
        longitude=lon,
        outputformat="json",
        usehorizon=True,
        map_variables=True,   # renames columns to pvlib standard: ghi, dni, dhi
    )
    print(f"  TMY data loaded: {len(tmy)} hourly records.")
    return tmy


# ── Step 4: LiDAR clipping ────────────────────────────────────────────────────

def clip_lidar_to_building(tile_path, footprint_poly, buffer_deg=0.00002):
    """
    Use PDAL to extract LiDAR points that fall within a building footprint.
    Returns an (N, 3) numpy array of [X, Y, Z], or None if too few points.
    """
    buffered_wkt = footprint_poly.buffer(buffer_deg).wkt

    pipeline_json = json.dumps(
        {
            "pipeline": [
                {"type": "readers.las", "filename": tile_path.replace("\\", "/")},
                {"type": "filters.crop", "polygon": buffered_wkt},
            ]
        }
    )

    try:
        pipeline = pdal.Pipeline(pipeline_json)
        pipeline.execute()
        arrays = pipeline.arrays
        if not arrays or len(arrays[0]) == 0:
            return None

        pts = arrays[0]

        # Prefer building-classified points (class 6); fall back to height filter
        if "Classification" in pts.dtype.names:
            roof_pts = pts[pts["Classification"] == 6]
            if len(roof_pts) < MIN_ROOF_POINTS:
                z_min = pts["Z"].min()
                roof_pts = pts[pts["Z"] > z_min + 2.0]
        else:
            z_min = pts["Z"].min()
            roof_pts = pts[pts["Z"] > z_min + 2.0]

        if len(roof_pts) < MIN_ROOF_POINTS:
            return None

        return np.column_stack([roof_pts["X"], roof_pts["Y"], roof_pts["Z"]])

    except Exception:
        return None


# ── Step 5: Roof plane extraction (RANSAC) ────────────────────────────────────

def extract_roof_planes(points_xyz):
    """
    Fit roof planes to LiDAR points using Open3D RANSAC.
    Returns a list of plane dicts with tilt, azimuth, and area.
    """
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points_xyz)

    planes = []
    remaining = pcd

    for _ in range(6):  # up to 6 roof faces
        if len(remaining.points) < MIN_ROOF_POINTS:
            break

        plane_model, inliers = remaining.segment_plane(
            distance_threshold=RANSAC_DISTANCE_THRESHOLD,
            ransac_n=3,
            num_iterations=RANSAC_ITERATIONS,
        )

        if len(inliers) < MIN_ROOF_POINTS:
            break

        # Plane equation: ax + by + cz + d = 0  → normal = [a, b, c]
        normal = np.array(plane_model[:3])
        if normal[2] < 0:
            normal = -normal  # ensure normal points upward

        norm_len = np.linalg.norm(normal)
        if norm_len == 0:
            remaining = remaining.select_by_index(inliers, invert=True)
            continue

        nx, ny, nz = normal / norm_len

        # Tilt from horizontal: 0° = flat, 90° = vertical wall
        tilt_deg = float(np.degrees(np.arccos(min(nz, 1.0))))

        # Skip near-vertical surfaces (walls, dormers)
        if tilt_deg > 72:
            remaining = remaining.select_by_index(inliers, invert=True)
            continue

        # Azimuth: 0°=N, 90°=E, 180°=S, 270°=W (compass convention)
        # In projected coords, +X=east, +Y=north → atan2(nx, ny) gives bearing
        azimuth_deg = float((np.degrees(np.arctan2(nx, ny)) + 360) % 360)

        # Estimate plane area via convex hull of projected inlier points
        inlier_pts = np.asarray(remaining.points)[inliers]
        try:
            if len(inlier_pts) >= 3:
                hull = ConvexHull(inlier_pts[:, :2])
                area_m2 = float(hull.volume)  # in 2D, .volume = area in coordinate units²
                # If coords are in degrees (geographic), convert to m²
                if abs(inlier_pts[0, 0]) < 180:  # likely lat/lon, not projected
                    area_m2 *= (111320 ** 2) * np.cos(np.radians(np.mean(inlier_pts[:, 1])))
            else:
                area_m2 = 0.0
        except Exception:
            area_m2 = float(len(inlier_pts))  # rough fallback: 1 pt ≈ 1 m²

        planes.append(
            {
                "tilt_deg": tilt_deg,
                "azimuth_deg": azimuth_deg,
                "area_m2": area_m2,
                "point_count": len(inliers),
            }
        )

        remaining = remaining.select_by_index(inliers, invert=True)

    return planes


# ── Step 6: Solar potential calculation (pvlib) ───────────────────────────────

def calculate_annual_kwh(lat, lon, tmy, planes):
    """
    Calculate total annual kWh potential across all viable roof planes.
    Returns (total_kwh, best_plane).
    """
    location = pvlib.location.Location(latitude=lat, longitude=lon, tz="Etc/GMT+5")
    solar_pos = location.get_solarposition(tmy.index)

    PANEL_EFFICIENCY = 0.20   # typical monocrystalline panel
    SYSTEM_LOSSES = 0.86      # inverter + wiring + soiling (~14% total loss)
    MAX_PANEL_AREA = 150.0    # sanity cap in m² — no residential roof has more

    total_kwh = 0.0
    best_plane = None
    best_kwh = 0.0

    for plane in planes:
        # Skip near-north-facing planes (>120° from south = not worth paneling)
        if abs(plane["azimuth_deg"] - 180) > 120:
            continue

        area = min(plane["area_m2"], MAX_PANEL_AREA)
        if area <= 0:
            continue

        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=plane["tilt_deg"],
            surface_azimuth=plane["azimuth_deg"],
            solar_zenith=solar_pos["apparent_zenith"],
            solar_azimuth=solar_pos["azimuth"],
            ghi=tmy["ghi"],
            dni=tmy["dni"],
            dhi=tmy["dhi"],
        )

        plane_kwh = float(
            (poa["poa_global"].fillna(0) * area * PANEL_EFFICIENCY * SYSTEM_LOSSES).sum()
            / 1000
        )

        total_kwh += plane_kwh
        if plane_kwh > best_kwh:
            best_kwh = plane_kwh
            best_plane = plane

    return total_kwh, best_plane


# ── Step 7: Grading ───────────────────────────────────────────────────────────

def grade_home(annual_kwh, total_area_m2, primary_azimuth_deg):
    score = 0

    if annual_kwh >= 12000:    score += 50
    elif annual_kwh >= 9000:   score += 40
    elif annual_kwh >= 6000:   score += 30
    elif annual_kwh >= 3000:   score += 15

    if total_area_m2 >= 50:    score += 20
    elif total_area_m2 >= 30:  score += 12
    elif total_area_m2 >= 15:  score += 5

    offset = abs(primary_azimuth_deg - 180)
    if offset <= 15:           score += 20
    elif offset <= 45:         score += 14
    elif offset <= 75:         score += 7

    # Shade factor placeholder (always 1.0 until shade analysis is implemented)
    score += 10

    if score >= 85:    return "A+", score
    elif score >= 70:  return "A",  score
    elif score >= 55:  return "B+", score
    elif score >= 40:  return "B",  score
    elif score >= 25:  return "C",  score
    else:              return "D",  score


# ── Step 8: Database ──────────────────────────────────────────────────────────

def setup_db(path):
    con = duckdb.connect(path)
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
            annual_kwh_potential  DOUBLE,
            solar_score           INTEGER,
            solar_grade           VARCHAR(2),
            processed_at          TIMESTAMP
        )
    """)
    return con


def save_result(con, building, result):
    con.execute(
        """
        INSERT OR REPLACE INTO homes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            building["osm_id"],
            building["lat"],
            building["lon"],
            building["footprint_m2"],
            result["usable_area_m2"],
            result["primary_tilt"],
            result["primary_azimuth"],
            result["plane_count"],
            result["annual_kwh"],
            result["score"],
            result["grade"],
            pd.Timestamp.now(),
        ],
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("SOLAR GRADER — Pipeline Test")
    print(f"Test area bbox: {TEST_BBOX}")
    print("=" * 60)

    # 1. Find tile
    print("\n[1/5] Finding LiDAR tile...")
    tile_info = find_lidar_tile(TEST_BBOX)

    # 2. Download tile
    print("\n[2/5] Downloading tile...")
    tile_path = download_tile(tile_info["downloadURL"], TILE_CACHE_DIR)

    # 3. Get buildings
    print("\n[3/5] Fetching building footprints from OpenStreetMap...")
    buildings = get_buildings_from_osm(TEST_BBOX)
    if not buildings:
        print("ERROR: No buildings found. Try adjusting TEST_BBOX.")
        return

    # 4. Solar irradiance data (one fetch covers all buildings in the area)
    print("\n[4/5] Fetching solar irradiance data...")
    tmy = get_tmy_data(TEST_LAT, TEST_LON)

    # 5. Process each building
    print(f"\n[5/5] Processing {len(buildings)} buildings...")
    con = setup_db(DB_PATH)

    counts = {"scored": 0, "skipped_no_points": 0, "skipped_no_planes": 0}
    grade_counts = {}
    timings = []
    t_total = time.time()

    for i, building in enumerate(buildings):
        t0 = time.time()
        print(
            f"\r  [{i+1}/{len(buildings)}] scored={counts['scored']} "
            f"skipped={counts['skipped_no_points']+counts['skipped_no_planes']}  ",
            end="",
        )

        # Clip LiDAR
        points = clip_lidar_to_building(tile_path, building["geometry"])
        if points is None:
            counts["skipped_no_points"] += 1
            continue

        # Extract roof planes
        planes = extract_roof_planes(points)
        if not planes:
            counts["skipped_no_planes"] += 1
            continue

        # Solar calc
        annual_kwh, best_plane = calculate_annual_kwh(
            building["lat"], building["lon"], tmy, planes
        )
        if annual_kwh == 0 or best_plane is None:
            counts["skipped_no_planes"] += 1
            continue

        # Grade
        total_area = sum(p["area_m2"] for p in planes)
        grade, score = grade_home(annual_kwh, total_area, best_plane["azimuth_deg"])

        save_result(
            con,
            building,
            {
                "usable_area_m2": total_area,
                "primary_tilt": best_plane["tilt_deg"],
                "primary_azimuth": best_plane["azimuth_deg"],
                "plane_count": len(planes),
                "annual_kwh": annual_kwh,
                "grade": grade,
                "score": score,
            },
        )

        counts["scored"] += 1
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        timings.append(time.time() - t0)

    elapsed = time.time() - t_total

    # ── Results ───────────────────────────────────────────────────────────────
    print(f"\n\n{'=' * 60}")
    print("RESULTS")
    print(f"{'=' * 60}")
    print(f"  Buildings found:         {len(buildings)}")
    print(f"  Successfully scored:     {counts['scored']}")
    print(f"  Skipped (no LiDAR):      {counts['skipped_no_points']}")
    print(f"  Skipped (no roof found): {counts['skipped_no_planes']}")
    if timings:
        print(f"  Avg time per building:   {np.mean(timings):.2f}s")
    print(f"  Total time:              {elapsed:.1f}s")

    print(f"\n  Grade breakdown:")
    for g in ["A+", "A", "B+", "B", "C", "D"]:
        n = grade_counts.get(g, 0)
        if n:
            print(f"    {g:2s}  {'█' * n}  ({n})")

    print(f"\n  Top 10 A/A+ leads:")
    df = con.execute("""
        SELECT lat, lon,
               ROUND(annual_kwh_potential) AS kwh_yr,
               ROUND(usable_roof_area_m2)  AS roof_m2,
               ROUND(primary_tilt_deg)     AS tilt,
               ROUND(primary_azimuth_deg)  AS azimuth,
               solar_grade                 AS grade
        FROM homes
        WHERE solar_grade IN ('A+', 'A')
        ORDER BY annual_kwh_potential DESC
        LIMIT 10
    """).fetchdf()

    if df.empty:
        print("    No A/A+ homes found in this tile.")
        print("    Try a denser suburban area, or check LiDAR point density.")
    else:
        print(df.to_string(index=False))

    # Diagnostic: LiDAR point density check
    print(f"\n  LiDAR density check:")
    sample = buildings[0] if buildings else None
    if sample:
        pts = clip_lidar_to_building(tile_path, sample["geometry"])
        if pts is not None:
            footprint_m2 = sample["footprint_m2"]
            density = len(pts) / max(footprint_m2, 1)
            print(f"    Sample building: {len(pts)} points over ~{footprint_m2:.0f} m²")
            print(f"    Point density: ~{density:.2f} pts/m²")
            if density < 0.5:
                print("    WARNING: Low density (<0.5 pts/m²) — RANSAC results may be unreliable.")
                print("    Consider finding a higher-density tile for this area.")
            elif density >= 4:
                print("    Density is good — RANSAC results should be reliable.")
            else:
                print("    Density is acceptable.")

    print(f"\n  Results saved to: {DB_PATH}")
    print(f"  Open with: python -c \"import duckdb; print(duckdb.connect('{DB_PATH}').execute('SELECT * FROM homes LIMIT 5').fetchdf())\"")
    con.close()


if __name__ == "__main__":
    main()
