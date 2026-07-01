# DIY Solar Potential Scoring Pipeline — Full Plan

## Overview

A one-time data pipeline that produces a permanent, per-address solar score for every home in Pennsylvania (~5M homes), then an application on top of it for sales reps. Everything below is free and open source.

---

## Architecture at a Glance

```
[LiDAR Point Clouds]  [Building Footprints]  [Solar Irradiance]  [Property Records]
        ↓                      ↓                      ↓                  ↓
   [Roof Plane Extraction] ──→ [Solar Calc (pvlib)] ──→ [Grading] ──→ [PostGIS DB]
                                                                            ↓
                                                              [Sales Rep Web App]
                                                                            ↓
                                                              [Route Optimizer (OSRM)]
```

---

## Phase 1 — Data Acquisition

### 1A. LiDAR Data (USGS 3DEP)

**What it gives you:** 3D point clouds of every structure in PA. From this you derive roof pitch, orientation (azimuth), usable area, and shading from trees/neighboring structures.

**How to get it:**
- Primary source: https://apps.nationalmap.gov/downloader/ — USGS National Map Downloader
- PA-specific portal: PASDA (Pennsylvania Spatial Data Access) at https://www.pasda.psu.edu — often has county-level LiDAR already organized, which saves significant download and indexing overhead
- Data format: LAZ files (compressed LAS point clouds), organized in ~1km × 1km tiles
- Coverage: Most of PA has 1-point-per-meter or better density. Urban areas often 8+ pts/m²

**Volume reality check:**
- PA is ~119,000 km²
- Expect 10–30 GB per 100km² at typical density
- Total raw download: **100 GB – 2 TB** for full state (varies by tile density available)
- You don't need to download all at once — process county by county

**Download strategy:**
```bash
# USGS 3DEP data is mirrored on AWS S3
# Downloading from within an AWS EC2 instance in us-east-1 costs nothing in egress
# s3://prd-tnm/StagedProducts/Elevation/
#
# PA bounds: lat 39.72–42.27, lon -80.52 to -74.69
# Use USGS National Map Downloader to enumerate available tiles for PA bounding box
```

### 1B. Building Footprints — pluggable source (the key to full coverage)

**What it gives you:** 2D polygon outlines of every building. Used to mask LiDAR points to only those belonging to a specific building. Completeness here = how many houses end up scored, so this is the single biggest coverage lever.

**Design:** a pluggable `get_buildings(bbox)` with interchangeable backends (scoring/address/grading downstream is identical regardless of source). Backends, from validation → scale:

1. **OSM Overpass** — used in initial validation. Simple, per-bbox API, but **incomplete** (measured ~2,500 vs the county's 3,391 in West Chester = ~26% of buildings missing) and rate-limits/504s at scale. Fine for prototyping, not for coverage.
2. **County ArcGIS footprint layer** (CURRENT for Chester rollout) — e.g. PASDA Chester County `MapServer/14` Building Footprints, bbox-queryable like the address layer. More complete than OSM, and **no Overpass rate-limit/504 problem**. Best where a county publishes footprints; downside is per-county endpoints.
3. **Microsoft US Building Footprints** (for statewide → other states) — https://github.com/microsoft/USBuildingFootprints, ODbL, ~130M US buildings, one GeoJSON per state (~PA 5M). Quality ≥ OSM per Microsoft. Pattern: download state file once → load into a local spatial index (DuckDB spatial extension) → query per-tile locally. **Uniform national coverage = the "PA then state-by-state" unlock.**

**Also:** record un-scoreable buildings (no roof / no viable plane) as a low/"not-viable" status instead of dropping them, so every structure appears in the output, not just solar-viable ones.

### 1C. Solar Irradiance (NREL NSRDB)

**What it gives you:** Hourly solar radiation values (GHI, DNI, DHI) for any point in PA, which pvlib uses to calculate actual energy production.

**How to get it:**
- API: https://developer.nrel.gov/docs/solar/nsrdb/ — free API key
- Spatial resolution: ~4km grid
- Temporal: hourly for any year (use a recent typical meteorological year)
- Since PA is ~119,000 km², at 4km resolution that's ~7,400 grid cells
- **Download all PA grid cells once** and store locally — this is a small dataset (few GB for a full year)

### 1D. Pennsylvania Address / Parcel Data

**What it gives you:** Ties everything back to a real mailable address for sales reps.

**How to get it:**
- PASDA: https://www.pasda.psu.edu — statewide parcel data
- PA Open Data: https://data.pa.gov
- County GIS offices: many PA counties publish parcel shapefiles free
- OpenAddresses: https://openaddresses.io — free bulk download of PA addresses

---

## Phase 2 — Infrastructure Setup

### Hardware / Compute

Since the budget is $0, two options:

**Option A: Your own machine**
- 16GB+ RAM recommended
- Multi-core CPU (processing 5M homes is parallelizable)
- 3–4 TB disk space for raw data + working files
- Processing time estimate: several weeks running continuously

**Option B: Free cloud compute (better)**
- **Oracle Cloud Free Tier** — genuinely free forever, gives you 4 ARM cores + 24GB RAM + 200GB storage. Best free cloud option.
- **AWS Spot Instances** — not free but extremely cheap (~$0.03/hour for a 16-core instance). Total compute cost for full PA: ~$20–50.

### Software Stack

```bash
# Core pipeline
pip install pdal python-pdal numpy scipy scikit-learn
pip install pvlib geopandas shapely fiona pyproj
pip install open3d          # point cloud plane segmentation
pip install dask[dataframe] # parallel processing
pip install psycopg2 sqlalchemy geoalchemy2

# Database
# PostgreSQL 15 + PostGIS 3.x (free, install locally or on Oracle Cloud)

# Routing
# OSRM (Docker image, self-hosted)
pip install ortools          # route optimization
```

---

## Phase 3 — LiDAR Roof Extraction Pipeline

This is the most technically complex phase. For each building:

### Step 3.1 — Tile Indexing

Build a spatial index mapping each building footprint to the LiDAR tile(s) it falls within:

```
For each building footprint:
  → Find which LAZ tile(s) intersect the footprint bounding box
  → Record tile filename(s) in database
```

### Step 3.2 — Point Cloud Clipping

For each building, extract only the LiDAR points that fall within its footprint polygon:

```python
# PDAL pipeline (JSON config) — handles I/O and classification filtering
{
  "pipeline": [
    {"type": "readers.las", "filename": "tile.laz"},
    {"type": "filters.crop", "polygon": "<WKT of building footprint>"},
    {"type": "filters.range", "limits": "Classification[6:6]"},  # class 6 = building points
    {"type": "writers.las", "filename": "building_123.laz"}
  ]
}
```

LiDAR point classification codes:
- Class 2 = ground
- Class 6 = building
- Class 1 = unclassified (may include trees)

> **Note:** PDAL handles I/O and filtering only. Plane fitting uses Open3D (see next step).

### Step 3.3 — Roof Plane Segmentation (Open3D RANSAC)

Extract distinct roof planes — each building may have multiple facets:

```python
import open3d as o3d
import numpy as np

def extract_roof_planes(points_xyz, min_points=50):
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points_xyz)

    planes = []
    remaining = pcd

    while len(remaining.points) > min_points:
        # RANSAC plane fitting
        plane_model, inliers = remaining.segment_plane(
            distance_threshold=0.15,  # 15cm tolerance
            ransac_n=3,
            num_iterations=1000
        )

        if len(inliers) < min_points:
            break

        planes.append({
            'normal': plane_model[:3],       # [a, b, c] of ax+by+cz+d=0
            'point_count': len(inliers),
            'points': np.asarray(remaining.points)[inliers]
        })

        # Remove inlier points and continue for next plane
        remaining = remaining.select_by_index(inliers, invert=True)

    return planes
```

### Step 3.4 — Derive Roof Geometry from Plane Normals

```python
def plane_normal_to_tilt_azimuth(normal):
    nx, ny, nz = normal

    # Tilt (pitch) from horizontal
    tilt_degrees = np.degrees(np.arccos(abs(nz) / np.linalg.norm(normal)))

    # Azimuth (0=North, 90=East, 180=South, 270=West)
    azimuth_degrees = np.degrees(np.arctan2(nx, ny)) % 360

    return tilt_degrees, azimuth_degrees

def compute_plane_area(points_in_plane):
    from scipy.spatial import ConvexHull
    hull = ConvexHull(points_in_plane[:, :2])
    return hull.volume  # in 2D, .volume = area (sq meters)
```

### Step 3.5 — Shade Analysis

For each roof plane, check if surrounding LiDAR points (trees, chimneys, adjacent buildings) cast shadows:

```python
def compute_shade_score(roof_plane_points, all_surrounding_points, lat, lon):
    import pvlib
    import pandas as pd

    times = pd.date_range('2023-01-01', '2023-12-31', freq='1h', tz='America/New_York')
    location = pvlib.location.Location(lat, lon)
    solar_position = location.get_solarposition(times)
    daytime = solar_position[solar_position['elevation'] > 0]

    unshaded_hours = 0
    for _, sun in daytime.iterrows():
        if not ray_intersects_obstacles(sun['azimuth'], sun['elevation'],
                                        roof_plane_points, all_surrounding_points):
            unshaded_hours += 1

    return unshaded_hours / len(daytime)
```

> **Performance tip:** Full ray-casting per sun position is slow. A faster alternative is to compute a horizon profile in 36 directions (every 10°) from the LiDAR, then check sun positions against it.

---

## Phase 4 — Solar Potential Calculation (pvlib)

For each usable roof plane, calculate annual energy production:

```python
import pvlib
import pandas as pd

def calculate_annual_kwh(lat, lon, tilt, azimuth, area_m2, shade_factor, nsrdb_data):
    location = pvlib.location.Location(latitude=lat, longitude=lon, tz='America/New_York')

    solar_pos = location.get_solarposition(nsrdb_data.index)

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tilt,
        surface_azimuth=azimuth,
        solar_zenith=solar_pos['apparent_zenith'],
        solar_azimuth=solar_pos['azimuth'],
        dni=nsrdb_data['DNI'],
        ghi=nsrdb_data['GHI'],
        dhi=nsrdb_data['DHI']
    )

    # Apply shade factor
    poa_effective = poa['poa_global'] * shade_factor

    # Typical residential solar panel ~20% efficiency, ~14% system losses
    PANEL_EFFICIENCY = 0.20
    SYSTEM_LOSSES = 0.86

    annual_kwh = (poa_effective * area_m2 * PANEL_EFFICIENCY * SYSTEM_LOSSES).sum() / 1000

    return annual_kwh

# For homes with multiple roof planes: sum across all south-ish facing planes
# Exclude planes with azimuth >90° from south, or tilt <5°
```

---

## Phase 5 — Grading Algorithm

```python
def grade_home(annual_kwh, usable_area_m2, primary_azimuth, primary_tilt, shade_factor):
    score = 0

    # 1. Annual production potential (0–50 points) — most important factor
    if annual_kwh >= 12000:   score += 50
    elif annual_kwh >= 9000:  score += 40
    elif annual_kwh >= 6000:  score += 30
    elif annual_kwh >= 3000:  score += 15

    # 2. Usable roof area (0–20 points)
    if usable_area_m2 >= 50:    score += 20  # ~540 sq ft
    elif usable_area_m2 >= 30:  score += 12
    elif usable_area_m2 >= 15:  score += 5

    # 3. Roof orientation (0–20 points) — south-facing is ideal
    azimuth_offset = abs(primary_azimuth - 180)  # deviation from true south
    if azimuth_offset <= 15:   score += 20
    elif azimuth_offset <= 45: score += 14
    elif azimuth_offset <= 75: score += 7

    # 4. Shade factor (0–10 points)
    if shade_factor >= 0.90:   score += 10
    elif shade_factor >= 0.75: score += 6
    elif shade_factor >= 0.60: score += 2

    # Assign grade
    if score >= 85:    return 'A+'
    elif score >= 70:  return 'A'
    elif score >= 55:  return 'B+'
    elif score >= 40:  return 'B'
    elif score >= 25:  return 'C'
    else:              return 'D'
```

---

## Phase 6 — Database Schema (DuckDB)

**PostGIS vs DuckDB:** PostGIS is just PostgreSQL + a spatial extension — it's not a separate database.
For this project, DuckDB is simpler and sufficient: it's a single file, no server to run, handles
5M+ records easily, and has its own spatial extension. Use DuckDB throughout.
Migrate to PostgreSQL only if you need concurrent writes from many workers simultaneously.

```sql
-- Core table: one row per home
CREATE TABLE homes (
    id                   BIGSERIAL PRIMARY KEY,
    address              TEXT,
    parcel_id            TEXT,
    geom                 GEOMETRY(Point, 4326),      -- lat/lon centroid
    footprint            GEOMETRY(Polygon, 4326),    -- building outline

    -- Roof analysis results
    usable_area_m2       FLOAT,
    primary_tilt_deg     FLOAT,
    primary_azimuth_deg  FLOAT,
    shade_factor         FLOAT,
    roof_plane_count     INT,

    -- Solar results
    annual_kwh_potential FLOAT,

    -- Grading
    solar_score          INT,
    solar_grade          CHAR(2),   -- A+, A, B+, B, C, D

    -- Pipeline metadata
    processed_at         TIMESTAMP,
    lidar_source         TEXT
);

-- Spatial index for map queries
CREATE INDEX homes_geom_idx ON homes USING GIST(geom);

-- Index for grade filtering (sales rep queries)
CREATE INDEX homes_grade_idx ON homes(solar_grade);

-- Route planning table
CREATE TABLE sales_routes (
    id          BIGSERIAL PRIMARY KEY,
    rep_id      TEXT,
    created_at  TIMESTAMP,
    home_ids    BIGINT[],
    route_geom  GEOMETRY(LineString, 4326),
    total_km    FLOAT
);
```

---

## Phase 7 — Processing Pipeline Orchestration

With 5M homes, parallel processing is essential:

```python
import dask
from dask import delayed

@delayed
def process_home(building_id, footprint_wkt, lat, lon, lidar_tile_paths, nsrdb_data):
    # 1. Load and clip LiDAR to footprint
    points = clip_lidar_to_footprint(lidar_tile_paths, footprint_wkt)
    if len(points) < 50:
        return None  # not enough points to analyze

    # 2. Extract roof planes
    planes = extract_roof_planes(points)

    # 3. For each plane: tilt, azimuth, area
    plane_data = [plane_normal_to_tilt_azimuth_area(p) for p in planes]

    # 4. Shade analysis
    shade = compute_shade_score_fast(points, lat, lon)

    # 5. Solar calc
    kwh = calculate_annual_kwh(
        lat, lon,
        plane_data[0]['tilt'],
        plane_data[0]['azimuth'],
        sum(p['area'] for p in plane_data),
        shade, nsrdb_data
    )

    # 6. Grade
    grade = grade_home(kwh, ...)

    return {'building_id': building_id, 'kwh': kwh, 'grade': grade, ...}

# Process in batches using Dask with multiple workers
results = dask.compute(
    *[process_home(...) for home in batch],
    scheduler='processes',
    num_workers=8
)
```

**Estimated processing time:**
- ~2–5 seconds per home (LiDAR I/O dominates)
- 8 parallel workers → ~0.4 seconds per home effective throughput
- 5M homes / 0.4s = ~2M seconds ÷ 3600 = **~23 days on 8 cores**
- On 32 cores (Oracle Cloud ARM free tier): **~6 days**
- Process counties independently to allow safe resuming if interrupted

---

## Phase 8 — Sales Rep Web Application

### Backend API (FastAPI)

```
GET  /api/homes?grade=A,A+&bbox=-75.5,39.9,-74.9,40.4  → paginated list of homes in map view
GET  /api/homes/{id}                                     → detail for one home
POST /api/routes                                         → generate optimized route
GET  /api/routes/{id}                                    → route with waypoints
```

### Frontend

- **Leaflet.js** with OpenStreetMap tiles (free) for the map
- Color-coded pins by grade: A+ = gold, A = green, B = yellow, etc.
- Sales rep clicks homes to add to their daily route list
- Route optimization triggered on submit

### Route Optimization Setup

**Step 1: Self-host OSRM for Pennsylvania**

```bash
# Download PA road network from Geofabrik (~300–500 MB)
wget https://download.geofabrik.de/north-america/us/pennsylvania-latest.osm.pbf

# Process with OSRM via Docker
docker run -t -v $(pwd):/data osrm/osrm-backend osrm-extract \
    -p /opt/car.lua /data/pennsylvania-latest.osm.pbf
docker run -t -v $(pwd):/data osrm/osrm-backend osrm-partition /data/pennsylvania-latest.osrm
docker run -t -v $(pwd):/data osrm/osrm-backend osrm-customize /data/pennsylvania-latest.osrm

# Run the routing server
docker run -t -i -p 5000:5000 -v $(pwd):/data osrm/osrm-backend osrm-routed \
    --algorithm mld /data/pennsylvania-latest.osrm
```

**Step 2: OR-Tools TSP Solver (free, Apache 2.0)**

```python
from ortools.constraint_solver import routing_enums_pb2, pywrapcp
import requests

def build_optimized_route(homes, rep_start_location):
    # 1. Build distance matrix via OSRM Table API
    coords = [rep_start_location] + [h['coords'] for h in homes]
    coord_str = ';'.join(f"{lon},{lat}" for lat, lon in coords)

    response = requests.get(
        f"http://localhost:5000/table/v1/driving/{coord_str}",
        params={"annotations": "duration"}
    )
    duration_matrix = response.json()['durations']

    # 2. Solve TSP with OR-Tools
    manager = pywrapcp.RoutingIndexManager(len(coords), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_idx, to_idx):
        return int(duration_matrix[manager.IndexToNode(from_idx)]
                                  [manager.IndexToNode(to_idx)])

    transit_idx = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )

    solution = routing.SolveWithParameters(search_params)
    return extract_route_order(manager, routing, solution, homes)
```

---

## Phase 9 — Phased Rollout Strategy

Don't try to process all of PA at once. Process in this order:

| Phase | Target | Reason |
|-------|--------|--------|
| 1 | 1 county (e.g. Chester County) | Validate full pipeline end-to-end |
| 2 | Philadelphia metro + suburbs | Highest density = most leads |
| 3 | Pittsburgh metro | Second largest market |
| 4 | Remaining counties | Background processing |

---

## Key Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| LiDAR coverage gaps in rural areas | Check PASDA coverage map first; fall back to building footprint area + regional average tilt for uncovered parcels |
| LiDAR tiles not pre-classified (no building class) | Use height filtering (points above X meters) + building footprint mask as fallback |
| Stale LiDAR (trees grown, new buildings) | Record acquisition date per tile; flag homes where LiDAR is >5 years old |
| Processing time too long | Prioritize urban/suburban tiles; skip agricultural/rural tiles with few homes |
| NSRDB API rate limits | Download all ~7,400 PA grid cells once as CSV files, store locally — never hit the API again |

---

## Cost Summary

| Component | Cost |
|-----------|------|
| LiDAR data (USGS 3DEP) | Free |
| Building footprints (Microsoft) | Free |
| Solar irradiance (NREL NSRDB) | Free |
| Property/address data (PASDA, OpenAddresses) | Free |
| PostGIS database | Free |
| OSRM routing engine | Free |
| OR-Tools optimizer | Free |
| Leaflet.js + OpenStreetMap | Free |
| Compute (Oracle Cloud Free Tier) | Free |
| Compute (AWS Spot, if preferred) | ~$20–50 one-time |

**Total: $0** (or ~$20–50 if using cloud compute for speed)

---

## What You're Actually Building

1. A **one-time data pipeline** (Python scripts) that runs for ~1–4 weeks and produces a scored database
2. A **PostGIS database** with ~5M scored PA homes
3. A **lightweight web app** (FastAPI + Leaflet) for sales reps to browse A/A+ homes
4. A **self-hosted OSRM routing engine** for generating daily optimized routes
