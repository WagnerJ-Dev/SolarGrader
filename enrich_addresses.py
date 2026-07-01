"""
Solar Grader — Address Enrichment (Chester County, PA)

Post-processing step: attaches real street addresses to scored homes by matching
each building centroid to its nearest official address point from Chester County's
PASDA ArcGIS service. Decoupled from scoring — operates on the existing DuckDB,
re-runnable, no LiDAR reprocessing.

Coverage note: the address-point service is Chester-County-specific. Other counties
have their own PASDA/GIS services; we'd map the right one per region when scaling
beyond Chester.

Run with:
    source .venv/bin/activate
    python enrich_addresses.py
"""

import warnings

import duckdb
import numpy as np
import pandas as pd
import requests
from scipy.spatial import cKDTree

warnings.filterwarnings("ignore")

DB_PATH = "solar_grader.duckdb"
ADDR_LAYER = (
    "https://mapservices.pasda.psu.edu/server/rest/services/"
    "pasda/ChesterCounty/MapServer/12/query"
)
MATCH_RADIUS_M = 50.0     # max centroid→address-point distance to accept a match
BBOX_MARGIN_DEG = 0.002   # pad the query bbox so edge homes still find a point

# Residential filter: exclude institutional (tax-exempt), multi-unit/commercial
# (address has a unit marker), or too-large-to-be-a-house footprints. Heuristic —
# refine later with parcel land-use codes if needed.
RESIDENTIAL_MAX_FOOTPRINT_M2 = 500
UNIT_MARKERS = (" APT", " STE", " UNIT", " FL ", " #", " BLDG", " RM ")


# ── Pull address points from the county service ───────────────────────────────

def fetch_address_points(bbox):
    """All Chester County address points intersecting bbox, via ArcGIS REST.
    Uses an id-only query then batched fetches (robust to the 1000-record cap)."""
    west, south, east, north = bbox
    geom = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
    }

    ids = requests.get(
        ADDR_LAYER,
        params={**geom, "where": "1=1", "returnIdsOnly": "true", "f": "json"},
        timeout=90,
    ).json().get("objectIds") or []
    print(f"  {len(ids)} address points in region; fetching...")

    points = []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        # POST (not GET) so a long objectIds list can't blow the URL length limit
        resp = requests.post(
            ADDR_LAYER,
            data={
                "objectIds": ",".join(map(str, batch)),
                "outFields": "ADDR_NUM,ROAD_NAME,FULL_ADDRE,MUNI_NAME,ZIP_CODE_S,TAX_EXEMPT",
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "json",
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        for f in data.get("features", []):
            g, a = f.get("geometry"), f.get("attributes", {})
            if g:
                points.append((g["x"], g["y"], a))
    return points


# ── Local equirectangular projection for fast nearest-neighbour matching ──────

def _projector(lat0):
    cos0 = np.cos(np.radians(lat0))
    return lambda lon, lat: (np.asarray(lon) * 111320.0 * cos0,
                             np.asarray(lat) * 110540.0)


def main():
    try:
        con = duckdb.connect(DB_PATH)
    except duckdb.IOException:
        print("Database is locked — is pipeline.py still running?")
        print("Let the scoring run finish, then re-run this (it's a separate step).")
        return
    homes = con.execute("SELECT osm_id, lat, lon, footprint_m2 FROM homes").fetchdf()
    if homes.empty:
        print("No homes in the database yet — run pipeline.py first.")
        return
    print(f"Enriching {len(homes)} scored homes with addresses...")

    bbox = (
        homes["lon"].min() - BBOX_MARGIN_DEG, homes["lat"].min() - BBOX_MARGIN_DEG,
        homes["lon"].max() + BBOX_MARGIN_DEG, homes["lat"].max() + BBOX_MARGIN_DEG,
    )
    pts = fetch_address_points(bbox)
    if not pts:
        print("No address points returned — check the region/service.")
        return

    lat0 = float(homes["lat"].median())
    proj = _projector(lat0)
    ax, ay = proj([p[0] for p in pts], [p[1] for p in pts])
    tree = cKDTree(np.column_stack([ax, ay]))

    hx, hy = proj(homes["lon"].values, homes["lat"].values)
    dist, idx = tree.query(np.column_stack([hx, hy]), distance_upper_bound=MATCH_RADIUS_M)

    footprints = dict(zip(homes["osm_id"].values, homes["footprint_m2"].values))
    rows, matched = [], 0
    for osm_id, d, j in zip(homes["osm_id"].values, dist, idx):
        osm_id = int(osm_id)
        foot = footprints.get(osm_id)
        small = foot is None or foot < RESIDENTIAL_MAX_FOOTPRINT_M2
        if np.isinf(d):
            # No address match — classify residential on footprint alone
            rows.append((osm_id, None, None, None, None, None, None, small))
            continue
        matched += 1
        a = pts[j][2]
        full = (a.get("FULL_ADDRE") or "")
        tax = a.get("TAX_EXEMPT")
        has_unit = any(mk in f" {full.upper()}" for mk in UNIT_MARKERS)
        is_res = (tax != "Yes") and (not has_unit) and small
        rows.append((osm_id, full or None, a.get("ADDR_NUM"), a.get("ROAD_NAME"),
                     a.get("MUNI_NAME"), a.get("ZIP_CODE_S"), tax, is_res))

    # Add columns (idempotent) and bulk-update via a join
    for col, typ in [("full_address", "VARCHAR"), ("house_number", "VARCHAR"),
                     ("street", "VARCHAR"), ("city", "VARCHAR"), ("zip_code", "VARCHAR"),
                     ("tax_exempt", "VARCHAR"), ("is_residential", "BOOLEAN")]:
        try:
            con.execute(f"ALTER TABLE homes ADD COLUMN {col} {typ}")
        except duckdb.Error:
            pass  # already exists on a re-run

    addr_df = pd.DataFrame(rows, columns=[
        "osm_id", "full_address", "house_number", "street", "city", "zip_code",
        "tax_exempt", "is_residential"])
    con.register("addr_df", addr_df)
    con.execute("""
        UPDATE homes SET
            full_address = a.full_address, house_number = a.house_number,
            street = a.street, city = a.city, zip_code = a.zip_code,
            tax_exempt = a.tax_exempt, is_residential = a.is_residential
        FROM addr_df a
        WHERE homes.osm_id = a.osm_id
    """)

    # ── Report ────────────────────────────────────────────────────────────────
    total = len(homes)
    n_res = con.execute("SELECT COUNT(*) FROM homes WHERE is_residential").fetchone()[0]
    print(f"\n  Matched {matched} / {total} homes ({100 * matched / total:.0f}%) to an address.")
    print(f"  Residential (single-family): {n_res} / {total} "
          f"({100 * n_res / total:.0f}%) — the rest are institutional/multi-unit/large.")

    print("\n  Top RESIDENTIAL A+/A leads:")
    df = con.execute("""
        SELECT full_address, city, zip_code, solar_grade AS grade,
               ROUND(res_annual_kwh) AS res_kwh, potential_grade AS pot
        FROM homes
        WHERE solar_grade IN ('A+', 'A') AND is_residential AND full_address IS NOT NULL
        ORDER BY res_annual_kwh DESC
        LIMIT 10
    """).fetchdf()
    print(df.to_string(index=False) if not df.empty else "    (none yet)")
    con.close()


if __name__ == "__main__":
    main()
