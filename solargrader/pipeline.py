"""
Pipeline orchestration — streaming, parallel, and resumable.

Tiles are independent, so each is scored end-to-end in its own worker process; the
main process holds the single DuckDB writer and persists results as workers finish.
Each worker deletes its own tile after scoring, so peak disk stays ~``n_workers``
tiles. ``Config`` is passed explicitly into every worker (it is picklable), so a run
is fully described by that object even under the spawn start method.
"""

from __future__ import annotations

import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from shapely.ops import transform as shapely_transform

from .config import Config
from .geometry import clip_lidar_to_building, tile_latlon_bbox, transformer_to
from .grading import grade_home, grade_potential
from .models import Building, ScoredHome
from .roof import extract_roof_planes
from .solar import annual_energy, compute_horizon
from .sources.buildings import get_buildings
from .sources.irradiance import get_tmy
from .sources.tiles import discover_tiles, download_tile, get_tile_srs, load_tile_points, tile_id_of
from .storage import ResultStore
from .util import with_retries


def score_building(building: Building, tile_points, transformer, tmy,
                   cfg: Config) -> ScoredHome | None:
    """Full scoring chain for one building: clip LiDAR → fit planes → shade → energy
    → grade. Returns a ``ScoredHome`` or ``None`` if the building can't be scored."""
    points = clip_lidar_to_building(tile_points, building.geometry, transformer, cfg)
    if points is None:
        return None
    planes = extract_roof_planes(points, cfg)
    if not planes:
        return None

    observer = points.mean(axis=0)
    own_footprint = shapely_transform(transformer.transform, building.geometry).buffer(1.0)
    horizon = compute_horizon(tile_points, observer, cfg, exclude_poly=own_footprint)

    sysres = annual_energy(building.lat, building.lon, tmy, planes, cfg, horizon)
    if sysres is None or sysres.res_panels == 0:
        return None

    best = sysres.best_plane
    grade, score = grade_home(sysres.res_kwh, sysres.res_kw, best.azimuth_deg)
    return ScoredHome(
        building=building,
        usable_area_m2=sum(p.area_m2 for p in planes),
        primary_tilt_deg=best.tilt_deg, primary_azimuth_deg=best.azimuth_deg,
        plane_count=len(planes),
        res_kwh=sysres.res_kwh, res_kw=sysres.res_kw, res_panels=sysres.res_panels,
        max_kwh=sysres.max_kwh, max_kw=sysres.max_kw, max_panels=sysres.max_panels,
        potential_grade=grade_potential(sysres.max_kw),
        shade_loss_pct=sysres.shade_loss_pct, grade=grade, score=score,
    )


def _delete_tile(path: str, cfg: Config) -> None:
    if cfg.delete_tiles_after and os.path.exists(path):
        os.remove(path)


def _process_tile(tile: dict, cfg: Config) -> dict:
    """Worker body: fully process ONE tile in its own process, then delete the tile
    file. Returns a compact, picklable result and never touches the DB. All failures
    are caught and returned so one bad tile can't kill the pool."""
    tid = tile_id_of(tile)
    try:
        path = download_tile(tile["downloadURL"], cfg.tile_cache_dir)
        tile_points = load_tile_points(path)
        tile_srs = get_tile_srs(path)
        transformer = transformer_to(tile_srs)
        tbbox = tile_latlon_bbox(tile_points, tile_srs)

        buildings = with_retries(get_buildings, tbbox, cfg)
        if not buildings:
            _delete_tile(path, cfg)
            return {"tid": tid, "n_buildings": 0, "scored": [], "error": None}

        # One irradiance fetch per tile (its center) covers all its buildings.
        cx, cy = (tbbox[0] + tbbox[2]) / 2, (tbbox[1] + tbbox[3]) / 2
        tmy = with_retries(get_tmy, cy, cx)

        scored = []
        for building in buildings:
            home = score_building(building, tile_points, transformer, tmy, cfg)
            if home is not None:
                # Geometry isn't needed downstream (save() uses id/lat/lon/footprint);
                # drop it so the inter-process payload stays small.
                home.building.geometry = None
                scored.append(home)

        _delete_tile(path, cfg)
        return {"tid": tid, "n_buildings": len(buildings), "scored": scored, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"tid": tid, "n_buildings": 0, "scored": [],
                "error": f"{type(e).__name__}: {e}"}


def run(bbox, cfg: Config, store: ResultStore | None = None) -> None:
    """Score every tile covering ``bbox``, accumulating into the results DB."""
    print("=" * 60)
    print(f"SOLAR GRADER — region {tuple(round(v, 3) for v in bbox)}")
    print("=" * 60)
    print("\nDiscovering LiDAR tiles...")
    tiles = discover_tiles(bbox, cfg)
    if not tiles:
        print("  ABORT: no tiles for this region (TNM + fallback both empty).")
        return

    own_store = store is None
    store = store or ResultStore(cfg.db_path)
    try:
        done = store.done_tiles()
        pending = [t for t in tiles if tile_id_of(t) not in done]
        if done:
            print(f"  {len(done)} already processed — resuming.")
        print(f"\nScoring {len(pending)} tiles across {cfg.n_workers} worker "
              f"processes (single DB writer in this process)...")

        t_start = time.time()
        completed = 0
        with ProcessPoolExecutor(max_workers=cfg.n_workers) as pool:
            futures = {pool.submit(_process_tile, t, cfg): tile_id_of(t) for t in pending}
            for fut in as_completed(futures):
                completed += 1
                res = fut.result()   # workers catch their own errors; never raises here
                tid = res["tid"]
                if res["error"]:
                    print(f"[{completed}/{len(pending)}] {tid} — ERROR: {res['error']} (skipped)")
                    continue
                for home in res["scored"]:
                    store.save(home)
                store.mark_tile_done(tid, len(res["scored"]))
                print(f"[{completed}/{len(pending)}] {tid} — "
                      f"scored {len(res['scored'])}/{res['n_buildings']}")

        if own_store:
            store.print_summary(time.time() - t_start)
    finally:
        if own_store:
            store.close()


def run_region(bbox, cfg: Config, store: ResultStore | None = None) -> None:
    """Process a whole county/state by tiling ``bbox`` into sub-regions and running
    each. Resumable: a ``regions_done`` ledger skips finished sub-regions, and
    ``tiles_done`` (inside ``run``) dedups tiles shared across sub-regions."""
    w, s, e, n = bbox
    step = cfg.step_deg
    nx = max(1, math.ceil((e - w) / step))   # integer step counts avoid float drift
    ny = max(1, math.ceil((n - s) / step))
    subs = []
    for ix in range(nx):
        x0, x1 = w + ix * step, min(w + (ix + 1) * step, e)
        for iy in range(ny):
            y0, y1 = s + iy * step, min(s + (iy + 1) * step, n)
            if x1 - x0 > 1e-9 and y1 - y0 > 1e-9:
                subs.append((round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)))

    print("#" * 60)
    print(f"REGION RUN — {len(subs)} sub-regions (~{step}°) over "
          f"{tuple(round(v, 3) for v in bbox)}")
    print("#" * 60)

    own_store = store is None
    store = store or ResultStore(cfg.db_path)
    try:
        done = store.done_regions()
        if done:
            print(f"  Resuming — {len(done)} sub-regions already complete.")
        for i, sub in enumerate(subs, 1):
            key = ",".join(f"{v:.4f}" for v in sub)
            if key in done:
                print(f"\n### sub-region {i}/{len(subs)} {sub} — already done, skip.")
                continue
            print(f"\n### sub-region {i}/{len(subs)} {sub}")
            try:
                run(sub, cfg, store=store)
            except Exception as ex:  # noqa: BLE001
                print(f"### sub-region {i} ERROR: {type(ex).__name__}: {ex} — continuing.")
                continue
            store.mark_region_done(key)
        print("\n" + "#" * 60)
        print(f"REGION RUN COMPLETE — {store.count_homes()} homes scored.")
        print("#" * 60)
    finally:
        if own_store:
            store.close()
