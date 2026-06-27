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
import shapely
from pyproj import Transformer
from scipy.spatial import ConvexHull
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

warnings.filterwarnings("ignore")

# ── Configuration ─────────────────────────────────────────────────────────────

# Small test area: residential single-family streets, West Chester borough, PA.
# Picked because the goal is scoring HOMES — the old Exton bbox (below) was all
# commercial/retail. Adjust these 4 numbers to target a different area; keep it
# small (0.02° × 0.02° max). (west, south, east, north)
TEST_BBOX = (-75.615, 39.955, -75.600, 39.970)
TEST_LAT = 39.962
TEST_LON = -75.607

# Previous test area (Exton commercial strip — big-box roofs, no houses):
#   TEST_BBOX = (-75.640, 40.015, -75.620, 40.030); TEST_LAT/LON = 40.022, -75.630

TILE_CACHE_DIR = "tile_cache"   # LiDAR tiles cached here (re-used on re-runs)
DB_PATH = "solar_results.duckdb"

# ── Panel / system model ──────────────────────────────────────────────────────
# All physical: change these to model different panels/configurations. The energy
# model packs WHOLE panels onto each roof plane after a fire-code edge setback,
# rather than assuming a fixed % of the roof is usable.
PANEL_WIDTH_M     = 1.05    # standard residential module footprint (~1.05 × 1.74 m)
PANEL_HEIGHT_M    = 1.74
PANEL_WATTS       = 400     # module rated DC power at STC (1000 W/m²)
ROOF_SETBACK_M    = 0.46    # fire-code clear pathway from plane edges (~18 inches)
PANEL_PACKING     = 0.90    # fraction of the setback area that packs into whole modules
SYSTEM_LOSSES     = 0.86    # inverter + wiring + soiling (~14% total loss)
MAX_AZIMUTH_OFFSET = 120    # skip planes facing >this many degrees from due south
MAX_SYSTEM_KW     = 10.0    # cap for a typical sellable residential install (DC kW)
SHADE_RADIUS_M    = 50.0    # search radius for shading obstructions (trees, neighbours)
HORIZON_BINS      = 72      # azimuth bins for the roof skyline (5° each)

PANEL_AREA_M2 = PANEL_WIDTH_M * PANEL_HEIGHT_M           # m² per module
MODULE_EFF    = PANEL_WATTS / (PANEL_AREA_M2 * 1000.0)   # derived STC efficiency

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
        headers={"User-Agent": "SolarGrader/1.0 (joshuawagnersd@gmail.com)"},
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
    tmy, _ = pvlib.iotools.get_pvgis_tmy(  # pvlib >=0.11 returns (data, metadata)
        latitude=lat,
        longitude=lon,
        outputformat="json",
        usehorizon=True,
        map_variables=True,   # renames columns to pvlib standard: ghi, dni, dhi
    )
    print(f"  TMY data loaded: {len(tmy)} hourly records.")
    return tmy


# ── Step 4: LiDAR clipping ────────────────────────────────────────────────────

def get_tile_srs(tile_path):
    """Read the LiDAR tile's spatial reference (WKT) from its LAS header."""
    p = pdal.Pipeline(json.dumps(
        {"pipeline": [{"type": "readers.las", "filename": tile_path.replace("\\", "/"), "count": 1}]}
    ))
    p.execute()
    md = p.metadata["metadata"]["readers.las"]
    return md.get("comp_spatialreference") or md.get("spatialreference")


def load_tile_points(tile_path):
    """
    Read the entire LiDAR tile into memory ONCE as a structured numpy array.
    Returns (points, X, Y) where X/Y are float views for fast bbox masking.
    Reading once and filtering per-building in RAM is ~100x faster than
    re-opening the 145 MB file for every building.
    """
    p = pdal.Pipeline(json.dumps(
        {"pipeline": [{"type": "readers.las", "filename": tile_path.replace("\\", "/")}]}
    ))
    p.execute()
    pts = p.arrays[0]
    return pts, np.asarray(pts["X"], dtype=float), np.asarray(pts["Y"], dtype=float)


def clip_lidar_to_building(tile_points, footprint_poly, transformer, buffer_m=0.5):
    """
    Extract LiDAR points inside a building footprint from already-loaded tile
    points. The footprint comes in lon/lat (EPSG:4326); the tile is in projected
    meters, so we reproject the polygon into the tile's CRS before clipping.
    Returns an (N, 3) numpy array of [X, Y, Z], or None if too few points.
    """
    pts, X, Y = tile_points
    proj_poly = shapely_transform(transformer.transform, footprint_poly).buffer(buffer_m)

    # Fast bounding-box prefilter, then exact point-in-polygon on the small subset
    minx, miny, maxx, maxy = proj_poly.bounds
    bbox_mask = (X >= minx) & (X <= maxx) & (Y >= miny) & (Y <= maxy)
    if not bbox_mask.any():
        return None

    sub = pts[bbox_mask]
    inside = shapely.contains_xy(proj_poly, sub["X"], sub["Y"])
    sub = sub[inside]
    if len(sub) == 0:
        return None

    # Prefer building-classified points (class 6); fall back to height filter
    if "Classification" in sub.dtype.names:
        roof_pts = sub[sub["Classification"] == 6]
        if len(roof_pts) < MIN_ROOF_POINTS:
            z_min = sub["Z"].min()
            roof_pts = sub[sub["Z"] > z_min + 2.0]
    else:
        z_min = sub["Z"].min()
        roof_pts = sub[sub["Z"] > z_min + 2.0]

    if len(roof_pts) < MIN_ROOF_POINTS:
        return None

    return np.column_stack([roof_pts["X"], roof_pts["Y"], roof_pts["Z"]])


# ── Step 5: Roof plane extraction (RANSAC) ────────────────────────────────────

def extract_roof_planes(points_xyz):
    """
    Fit roof planes to LiDAR points using Open3D RANSAC.
    Returns a list of plane dicts with tilt, azimuth, and area.
    """
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points_xyz)

    # Ground point density (points per m² of horizontal area) from ONE convex
    # hull over all points. Used to size each plane by its point count — this
    # avoids the inflation you get from summing overlapping per-plane hulls.
    # Coordinates are in projected meters (tile CRS), so areas are already m².
    all_xy = np.asarray(points_xyz)[:, :2]
    try:
        covered_m2 = float(ConvexHull(all_xy).volume) if len(all_xy) >= 3 else 0.0
    except Exception:
        covered_m2 = 0.0
    ground_density = (len(points_xyz) / covered_m2) if covered_m2 > 0 else 0.0

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

        # Plane area = (inlier point count ÷ ground density) gives the plane's
        # horizontal (ground-projected) area; dividing by cos(tilt) converts it
        # to true tilted surface area. Each point belongs to exactly one plane,
        # so summing these areas can never exceed the building's real footprint.
        if ground_density > 0:
            ground_area_m2 = len(inliers) / ground_density
            area_m2 = ground_area_m2 / max(np.cos(np.radians(tilt_deg)), 0.1)
        else:
            area_m2 = 0.0

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

def panels_on_plane(plane_area_m2):
    """
    How many whole panels physically fit on a roof plane of this area.
    Approximate the plane as an equal-area square, erode it inward by the
    fire-code setback on all sides, apply a packing factor, then divide by the
    module footprint. Small/cut-up roofs lose proportionally more area — the
    realistic behaviour a flat usable-% can't capture.
    """
    side = np.sqrt(plane_area_m2)
    usable_side = max(0.0, side - 2 * ROOF_SETBACK_M)
    usable_area = (usable_side ** 2) * PANEL_PACKING
    return int(usable_area // PANEL_AREA_M2)


def compute_horizon(tile_points, observer_xyz, exclude_poly=None,
                    radius_m=SHADE_RADIUS_M, n_bins=HORIZON_BINS):
    """
    Build the roof's skyline: the maximum obstruction elevation (degrees above the
    roof) in each compass direction, using surrounding LiDAR points — trees,
    neighbouring buildings — within radius_m. Points inside exclude_poly (the
    building's OWN footprint, in tile CRS) are dropped so a roof can't shade
    itself. Returns an array indexed by azimuth bin (0=N, clockwise); all zeros =
    wide-open sky.
    """
    pts, X, Y = tile_points
    ox, oy, oz = observer_xyz
    horizon = np.zeros(n_bins)

    m = (X >= ox - radius_m) & (X <= ox + radius_m) & (Y >= oy - radius_m) & (Y <= oy + radius_m)
    if not m.any():
        return horizon

    sx, sy = X[m], Y[m]
    sz = np.asarray(pts["Z"], dtype=float)[m]
    # Drop the building's own roof so it can't count as an obstruction to itself
    if exclude_poly is not None:
        outside = ~shapely.contains_xy(exclude_poly, sx, sy)
        sx, sy, sz = sx[outside], sy[outside], sz[outside]

    dx = sx - ox
    dy = sy - oy
    dz = sz - oz
    horiz = np.sqrt(dx * dx + dy * dy)
    # Keep points within radius, not right on top of us, and ABOVE the roof
    keep = (horiz <= radius_m) & (horiz > 1.0) & (dz > 0)
    if not keep.any():
        return horizon

    dx, dy, dz, horiz = dx[keep], dy[keep], dz[keep], horiz[keep]
    elev = np.degrees(np.arctan2(dz, horiz))                    # elevation angle
    az = (np.degrees(np.arctan2(dx, dy)) + 360) % 360           # 0=N, 90=E (compass)
    bins = (az / (360.0 / n_bins)).astype(int) % n_bins
    np.maximum.at(horizon, bins, elev)                          # tallest per direction
    return horizon


def calculate_annual_kwh(lat, lon, tmy, planes, horizon=None):
    """
    Pack whole panels onto each viable roof plane (pvlib irradiance per plane),
    then report TWO systems:
      - max:         every panel that fits the roof (upside potential)
      - residential: the best-producing panels up to MAX_SYSTEM_KW (sellable now)
    If a horizon (skyline) is given, the direct beam is removed during hours when
    the sun sits below the skyline, and shade_loss_pct reports the production lost.
    Returns a dict (incl. shade_loss_pct) or None if no viable planes exist.
    """
    location = pvlib.location.Location(latitude=lat, longitude=lon, tz="Etc/GMT+5")
    solar_pos = location.get_solarposition(tmy.index)

    # Per-hour direct-beam shading mask: sun up but below the roof's skyline
    sun_blocked = None
    if horizon is not None:
        sun_elev = 90.0 - solar_pos["apparent_zenith"].values
        sun_az = solar_pos["azimuth"].values
        sun_bins = (sun_az / (360.0 / len(horizon))).astype(int) % len(horizon)
        sun_blocked = (sun_elev > 0) & (sun_elev <= horizon[sun_bins])

    viable = []   # (per_panel_shaded, per_panel_unshaded, n_panels, plane)
    for plane in planes:
        # Skip planes facing too far from due south to be worth paneling
        if abs(plane["azimuth_deg"] - 180) > MAX_AZIMUTH_OFFSET:
            continue

        n_panels = panels_on_plane(plane["area_m2"])
        if n_panels <= 0:
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
        glob = poa["poa_global"].fillna(0).values
        unshaded_annual = glob.sum() / 1000  # kWh/m²/yr with full sun
        if sun_blocked is not None:
            # Remove the beam component during blocked hours (diffuse still counts)
            direct = poa["poa_direct"].fillna(0).values
            shaded_annual = (glob - direct * sun_blocked).sum() / 1000
        else:
            shaded_annual = unshaded_annual

        ppk_shaded = shaded_annual * PANEL_AREA_M2 * MODULE_EFF * SYSTEM_LOSSES
        ppk_unshaded = unshaded_annual * PANEL_AREA_M2 * MODULE_EFF * SYSTEM_LOSSES
        viable.append((ppk_shaded, ppk_unshaded, n_panels, plane))

    if not viable:
        return None

    # Max system: every panel that physically fits the roof
    max_panels = sum(n for _, _, n, _ in viable)
    max_kwh = sum(s * n for s, _, n, _ in viable)
    max_unshaded = sum(u * n for _, u, n, _ in viable)
    max_kw = max_panels * PANEL_WATTS / 1000.0
    shade_loss_pct = 100.0 * (1 - max_kwh / max_unshaded) if max_unshaded > 0 else 0.0

    # Residential system: fill the best-producing panels first, up to the kW cap
    cap_panels = int(MAX_SYSTEM_KW * 1000 / PANEL_WATTS)
    res_panels = 0
    res_kwh = 0.0
    for ppk_shaded, _, n_panels, _ in sorted(viable, key=lambda v: v[0], reverse=True):
        take = min(n_panels, cap_panels - res_panels)
        if take <= 0:
            break
        res_panels += take
        res_kwh += take * ppk_shaded
    res_kw = res_panels * PANEL_WATTS / 1000.0

    best_plane = max(viable, key=lambda v: v[0])[3]
    return {
        "res_kwh": res_kwh, "res_kw": res_kw, "res_panels": res_panels,
        "max_kwh": max_kwh, "max_kw": max_kw, "max_panels": max_panels,
        "shade_loss_pct": shade_loss_pct, "best_plane": best_plane,
    }


# ── Step 7: Grading ───────────────────────────────────────────────────────────

def grade_home(annual_kwh, total_area_m2, primary_azimuth_deg, shade_loss_pct):
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

    # Shade credit from the LiDAR skyline analysis (less shading = better lead)
    if shade_loss_pct <= 5:    score += 10
    elif shade_loss_pct <= 15: score += 7
    elif shade_loss_pct <= 30: score += 4

    if score >= 85:    return "A+", score
    elif score >= 70:  return "A",  score
    elif score >= 55:  return "B+", score
    elif score >= 40:  return "B",  score
    elif score >= 25:  return "C",  score
    else:              return "D",  score


def grade_potential(max_kw):
    """
    Tier a roof by its MAXIMUM installable capacity (kW DC) — the upside beyond a
    standard residential system (battery, EV, future expansion, or commercial).
    Separate from the residential sales grade, which is about the sellable system.
    """
    if max_kw >= 30:   return "P1"   # very high — large/commercial-scale roof
    elif max_kw >= 20: return "P2"
    elif max_kw >= 12: return "P3"
    elif max_kw >= 7:  return "P4"
    else:              return "P5"   # limited — barely fits a full residential set


# ── Step 8: Database ──────────────────────────────────────────────────────────

def setup_db(path):
    con = duckdb.connect(path)
    # Fresh table each run so test results reflect only the current tile/bbox
    # (the real multi-tile pipeline would accumulate instead of dropping).
    con.execute("DROP TABLE IF EXISTS homes")
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
    return con


def save_result(con, building, result):
    con.execute(
        """
        INSERT OR REPLACE INTO homes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            result["res_kwh"],
            result["res_kw"],
            result["res_panels"],
            result["score"],
            result["grade"],
            result["max_kwh"],
            result["max_kw"],
            result["max_panels"],
            result["potential_grade"],
            result["shade_loss_pct"],
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

    # Build a transformer from lon/lat (OSM) into the tile's projected CRS (meters)
    tile_srs = get_tile_srs(tile_path)
    transformer = Transformer.from_crs("EPSG:4326", tile_srs, always_xy=True)
    print(f"  Tile CRS: {tile_srs.split(',')[0].split('[')[-1].strip(chr(34))}")

    # Load the whole tile into memory once (filtered per-building in RAM below)
    print("  Loading tile points into memory...")
    tile_points = load_tile_points(tile_path)
    print(f"  Loaded {len(tile_points[0]):,} points.")

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
    density_samples = []   # (n_points, footprint_m2) for buildings that had LiDAR
    t_total = time.time()

    for i, building in enumerate(buildings):
        t0 = time.time()
        print(
            f"\r  [{i+1}/{len(buildings)}] scored={counts['scored']} "
            f"skipped={counts['skipped_no_points']+counts['skipped_no_planes']}  ",
            end="",
        )

        # Clip LiDAR
        points = clip_lidar_to_building(tile_points, building["geometry"], transformer)
        if points is not None:
            density_samples.append((len(points), building["footprint_m2"]))
        if points is None:
            counts["skipped_no_points"] += 1
            continue

        # Extract roof planes
        planes = extract_roof_planes(points)
        if not planes:
            counts["skipped_no_planes"] += 1
            continue

        # Skyline shading from surrounding trees/buildings (observer = roof centroid),
        # excluding the building's own footprint so it can't shade itself
        observer = points.mean(axis=0)
        own_footprint = shapely_transform(transformer.transform, building["geometry"]).buffer(1.0)
        horizon = compute_horizon(tile_points, observer, own_footprint)

        # Solar calc — returns both a residential-capped and a max-roof system
        sysres = calculate_annual_kwh(
            building["lat"], building["lon"], tmy, planes, horizon
        )
        if sysres is None or sysres["res_panels"] == 0:
            counts["skipped_no_planes"] += 1
            continue

        # Grade: residential = sellable system (lead quality); potential = max roof
        best_plane = sysres["best_plane"]
        total_area = sum(p["area_m2"] for p in planes)
        grade, score = grade_home(
            sysres["res_kwh"], total_area, best_plane["azimuth_deg"], sysres["shade_loss_pct"]
        )
        potential_grade = grade_potential(sysres["max_kw"])

        save_result(
            con,
            building,
            {
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
                "potential_grade": potential_grade,
                "shade_loss_pct": sysres["shade_loss_pct"],
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

    print(f"\n  Top 10 A/A+ leads (res = sellable ~{MAX_SYSTEM_KW:.0f}kW system, max = full-roof upside):")
    df = con.execute("""
        SELECT lat, lon,
               solar_grade                  AS grade,
               ROUND(res_system_kw, 1)      AS res_kw,
               ROUND(res_annual_kwh)        AS res_kwh,
               ROUND(shade_loss_pct)        AS shade_pct,
               potential_grade              AS pot,
               ROUND(max_system_kw, 1)      AS max_kw,
               ROUND(max_annual_kwh)        AS max_kwh,
               ROUND(primary_tilt_deg)      AS tilt,
               ROUND(primary_azimuth_deg)   AS azimuth
        FROM homes
        WHERE solar_grade IN ('A+', 'A')
        ORDER BY res_annual_kwh DESC, max_annual_kwh DESC
        LIMIT 10
    """).fetchdf()

    if df.empty:
        print("    No A/A+ homes found in this tile.")
        print("    Try a denser suburban area, or check LiDAR point density.")
    else:
        print(df.to_string(index=False))

    # Realism check: validate the PHYSICS on an UNSHADED basis (~1,100–1,300 in PA),
    # and report real-world shading separately so it doesn't look like a model error.
    print(f"\n  Realism check (UNSHADED kWh per kW — PA expected ~1,100–1,300):")
    yields = con.execute("""
        SELECT (res_annual_kwh / NULLIF(1 - shade_loss_pct / 100.0, 0)) / res_system_kw
                   AS unshaded_yield,
               shade_loss_pct
        FROM homes WHERE res_system_kw > 0
    """).fetchdf()
    if not yields.empty:
        med_yield = float(yields["unshaded_yield"].median())
        med_shade = float(yields["shade_loss_pct"].median())
        print(f"    Median unshaded yield: ~{med_yield:,.0f} kWh/kW")
        if 950 <= med_yield <= 1450:
            print("    OK — the panel/energy physics is in the realistic range.")
        else:
            print("    WARNING — outside the expected band; the panel/energy model needs review.")
        print(f"    Median shading loss:   {med_shade:.0f}%  (real reduction from trees/neighbours)")

    # Diagnostic: LiDAR point density check (averaged over buildings that had LiDAR)
    print(f"\n  LiDAR density check:")
    if density_samples:
        densities = [n / max(fp, 1) for n, fp in density_samples]
        density = float(np.median(densities))
        print(f"    Buildings with LiDAR: {len(density_samples)} of {len(buildings)}")
        print(f"    Median point density: ~{density:.2f} pts/m²")
        if density < 0.5:
            print("    WARNING: Low density (<0.5 pts/m²) — RANSAC results may be unreliable.")
            print("    Consider finding a higher-density tile for this area.")
        elif density >= 4:
            print("    Density is good — RANSAC results should be reliable.")
        else:
            print("    Density is acceptable.")
    else:
        print("    No buildings overlapped the tile — density unknown.")
        print("    The test bbox is likely larger than this single tile's footprint.")

    print(f"\n  Results saved to: {DB_PATH}")
    print(f"  Open with: python -c \"import duckdb; print(duckdb.connect('{DB_PATH}').execute('SELECT * FROM homes LIMIT 5').fetchdf())\"")
    con.close()


if __name__ == "__main__":
    main()
