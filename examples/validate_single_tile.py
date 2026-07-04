"""
Validate the scoring physics on a SINGLE LiDAR tile before scaling up.

Downloads one tile over a small residential area, scores every building with the
package API, and prints a grade breakdown plus two sanity checks: LiDAR point
density and an unshaded kWh/kW realism check (PA expected ~1,100–1,300). This is the
"trust the physics on a known tile" step from CLAUDE.md — run it after any change to
the roof/solar model.

    python examples/validate_single_tile.py
"""

import time
import warnings

import numpy as np

from solargrader.config import Config
from solargrader.geometry import transformer_to
from solargrader.pipeline import score_building
from solargrader.sources.buildings import get_buildings
from solargrader.sources.irradiance import get_tmy
from solargrader.sources.tiles import download_tile, find_all_tiles, get_tile_srs, load_tile_points

warnings.filterwarnings("ignore")

# Small residential area in West Chester borough, PA (0.015° ≈ 1.5 km square).
TEST_BBOX = (-75.615, 39.955, -75.600, 39.970)
TEST_LAT, TEST_LON = 39.962, -75.607


def main():
    cfg = Config(building_source="osm", tile_cache_dir="tile_cache")
    print("=" * 60, "\nSOLAR GRADER — single-tile validation\n", "=" * 60, sep="")

    print("\n[1/4] Finding + downloading a LiDAR tile...")
    tiles = find_all_tiles(TEST_BBOX, cfg)
    if not tiles:
        print("  No tiles found for this bbox — try another area.")
        return
    path = download_tile(tiles[0]["downloadURL"], cfg.tile_cache_dir)
    tile_srs = get_tile_srs(path)
    transformer = transformer_to(tile_srs)
    tile_points = load_tile_points(path)
    print(f"  Loaded {len(tile_points[0]):,} points from {tiles[0]['title']}")

    print("\n[2/4] Fetching buildings (OpenStreetMap)...")
    buildings = get_buildings(TEST_BBOX, cfg)
    if not buildings:
        print("  No buildings found — adjust TEST_BBOX.")
        return

    print("\n[3/4] Fetching irradiance (PVGIS)...")
    tmy = get_tmy(TEST_LAT, TEST_LON)

    print(f"\n[4/4] Scoring {len(buildings)} buildings...")
    grade_counts, density_samples, homes = {}, [], []
    t0 = time.time()
    for i, b in enumerate(buildings, 1):
        print(f"\r  {i}/{len(buildings)} scored={len(homes)}  ", end="")
        home = score_building(b, tile_points, transformer, tmy, cfg)
        if home is None:
            continue
        homes.append(home)
        grade_counts[home.grade] = grade_counts.get(home.grade, 0) + 1
        density_samples.append((home.res_kwh, home.res_kw, home.shade_loss_pct))

    print(f"\n\n{'=' * 60}\nRESULTS ({time.time() - t0:.0f}s)\n{'=' * 60}")
    print(f"  Buildings: {len(buildings)}  |  scored: {len(homes)}")
    print("\n  Grade breakdown:")
    for g in ["A+", "A", "B+", "B", "C", "D"]:
        if grade_counts.get(g):
            print(f"    {g:2s}  {'█' * grade_counts[g]}  ({grade_counts[g]})")

    # Realism: back out shading to check the UNSHADED physics (PA ~1,100–1,300 kWh/kW).
    yields = [rk / (1 - sh / 100.0) / rw for rk, rw, sh in density_samples if rw > 0 and sh < 100]
    if yields:
        med = float(np.median(yields))
        verdict = "OK" if 950 <= med <= 1450 else "WARNING — outside expected band"
        print(f"\n  Realism check: median unshaded yield ~{med:,.0f} kWh/kW  [{verdict}]")


if __name__ == "__main__":
    main()
