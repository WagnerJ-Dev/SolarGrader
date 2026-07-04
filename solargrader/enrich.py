"""
Address enrichment — attach real street addresses and a residential flag to scored
homes by matching each building centroid to the nearest official address point.

Decoupled from scoring: it operates on the existing DB, is re-runnable, and needs no
LiDAR reprocessing. The address-point service is county-specific (Chester County /
PASDA by default); where no service covers a region, homes are still classified
residential by footprint size, just without street addresses.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import requests
from scipy.spatial import cKDTree

from .config import Config

# Chester County address points (PASDA). Map the right service per county to extend.
ADDR_LAYER = ("https://mapservices.pasda.psu.edu/server/rest/services/"
              "pasda/ChesterCounty/MapServer/12/query")
MATCH_RADIUS_M = 50.0            # max centroid→address-point distance to accept
BBOX_MARGIN_DEG = 0.002          # pad the query bbox so edge homes still match
RESIDENTIAL_MAX_FOOTPRINT_M2 = 500
UNIT_MARKERS = (" APT", " STE", " UNIT", " FL ", " #", " BLDG", " RM ")


def _fetch_address_points(bbox):
    """All address points intersecting bbox: an id-only query then batched POSTs
    (robust to the service's 1000-record cap and URL-length limits)."""
    west, south, east, north = bbox
    geom = {"geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects"}
    ids = requests.get(ADDR_LAYER, params={**geom, "where": "1=1",
                       "returnIdsOnly": "true", "f": "json"}, timeout=90) \
        .json().get("objectIds") or []
    print(f"  {len(ids)} address points in region; fetching...")

    points = []
    for i in range(0, len(ids), 200):
        resp = requests.post(ADDR_LAYER, data={
            "objectIds": ",".join(map(str, ids[i:i + 200])),
            "outFields": "ADDR_NUM,ROAD_NAME,FULL_ADDRE,MUNI_NAME,ZIP_CODE_S,TAX_EXEMPT",
            "returnGeometry": "true", "outSR": "4326", "f": "json"}, timeout=90)
        resp.raise_for_status()
        for f in resp.json().get("features", []):
            g, a = f.get("geometry"), f.get("attributes", {})
            if g:
                points.append((g["x"], g["y"], a))
    return points


def _projector(lat0):
    """Local equirectangular lon/lat → meters, for fast nearest-neighbour matching."""
    cos0 = np.cos(np.radians(lat0))
    return lambda lon, lat: (np.asarray(lon) * 111320.0 * cos0,
                             np.asarray(lat) * 110540.0)


def enrich(cfg: Config) -> None:
    try:
        con = duckdb.connect(cfg.db_path)
    except duckdb.IOException:
        print("Database is locked — is the pipeline still running? Let it finish, "
              "then re-run this (it's a separate step).")
        return
    homes = con.execute("SELECT osm_id, lat, lon, footprint_m2 FROM homes").fetchdf()
    if homes.empty:
        print("No homes in the database yet — run the pipeline first.")
        con.close()
        return
    print(f"Enriching {len(homes)} scored homes with addresses...")

    bbox = (homes["lon"].min() - BBOX_MARGIN_DEG, homes["lat"].min() - BBOX_MARGIN_DEG,
            homes["lon"].max() + BBOX_MARGIN_DEG, homes["lat"].max() + BBOX_MARGIN_DEG)
    pts = _fetch_address_points(bbox)
    footprints = dict(zip(homes["osm_id"].values, homes["footprint_m2"].values))
    rows, matched = [], 0

    if not pts:
        print("  No county address points for this region — classifying residential "
              "by footprint only (street addresses need that county's service).")
        for osm_id in homes["osm_id"].values:
            osm_id = int(osm_id)
            foot = footprints.get(osm_id)
            small = foot is None or foot < RESIDENTIAL_MAX_FOOTPRINT_M2
            rows.append((osm_id, None, None, None, None, None, None, small))
    else:
        proj = _projector(float(homes["lat"].median()))
        ax, ay = proj([p[0] for p in pts], [p[1] for p in pts])
        tree = cKDTree(np.column_stack([ax, ay]))
        hx, hy = proj(homes["lon"].values, homes["lat"].values)
        dist, idx = tree.query(np.column_stack([hx, hy]), distance_upper_bound=MATCH_RADIUS_M)
        for osm_id, d, j in zip(homes["osm_id"].values, dist, idx):
            osm_id = int(osm_id)
            foot = footprints.get(osm_id)
            small = foot is None or foot < RESIDENTIAL_MAX_FOOTPRINT_M2
            if np.isinf(d):
                rows.append((osm_id, None, None, None, None, None, None, small))
                continue
            matched += 1
            a = pts[j][2]
            full = a.get("FULL_ADDRE") or ""
            tax = a.get("TAX_EXEMPT")
            has_unit = any(mk in f" {full.upper()}" for mk in UNIT_MARKERS)
            is_res = (tax != "Yes") and (not has_unit) and small
            rows.append((osm_id, full or None, a.get("ADDR_NUM"), a.get("ROAD_NAME"),
                         a.get("MUNI_NAME"), a.get("ZIP_CODE_S"), tax, is_res))

    for col, typ in [("full_address", "VARCHAR"), ("house_number", "VARCHAR"),
                     ("street", "VARCHAR"), ("city", "VARCHAR"), ("zip_code", "VARCHAR"),
                     ("tax_exempt", "VARCHAR"), ("is_residential", "BOOLEAN")]:
        try:
            con.execute(f"ALTER TABLE homes ADD COLUMN {col} {typ}")
        except duckdb.Error:
            pass  # already exists on a re-run

    con.register("addr_df", pd.DataFrame(rows, columns=[
        "osm_id", "full_address", "house_number", "street", "city", "zip_code",
        "tax_exempt", "is_residential"]))
    con.execute("""
        UPDATE homes SET full_address = a.full_address, house_number = a.house_number,
            street = a.street, city = a.city, zip_code = a.zip_code,
            tax_exempt = a.tax_exempt, is_residential = a.is_residential
        FROM addr_df a WHERE homes.osm_id = a.osm_id
    """)

    total = len(homes)
    n_res = con.execute("SELECT COUNT(*) FROM homes WHERE is_residential").fetchone()[0]
    print(f"\n  Matched {matched} / {total} homes ({100 * matched / total:.0f}%) to an address.")
    print(f"  Residential (single-family): {n_res} / {total} ({100 * n_res / total:.0f}%).")
    con.close()
