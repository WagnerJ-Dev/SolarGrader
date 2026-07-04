"""
Command-line interface.

    solargrader run  --region harrisburg          # a named area
    solargrader run  --county Dauphin,Cumberland  # one or more counties (gridded)
    solargrader run  --all-counties --state PA     # a whole state (long, resumable)
    solargrader run  --bbox -77.0 40.2 -76.8 40.4  # an explicit lon/lat box
    solargrader enrich                              # attach addresses + residential flag
    solargrader map                                 # write map.html
    solargrader regrade                             # re-grade in place, no reprocessing

Targets accumulate into one DB and every run is resumable (Ctrl-C and re-run).
"""

from __future__ import annotations

import argparse

from .config import Config
from .sources.regions import REGIONS, resolve_targets


def _config_from_args(args) -> Config:
    """Build a Config, overriding only the fields the user set."""
    overrides = {}
    if getattr(args, "db", None):
        overrides["db_path"] = args.db
    if getattr(args, "source", None):
        overrides["building_source"] = args.source
    if getattr(args, "workers", None):
        overrides["n_workers"] = args.workers
    if getattr(args, "max_tiles", None) is not None:
        overrides["max_tiles"] = args.max_tiles
    if getattr(args, "preferred_collection", None):
        overrides["preferred_collection"] = args.preferred_collection
    if getattr(args, "step", None):
        overrides["step_deg"] = args.step
    return Config(**overrides)


def _cmd_run(args) -> None:
    from .pipeline import run, run_region

    cfg = _config_from_args(args)
    if args.list:
        print("Named regions (--region):")
        for name, spec in sorted(REGIONS.items()):
            what = spec.get("bbox") or "counties: " + ", ".join(spec["counties"])
            print(f"  {name:18s} {what}")
        return

    # Default to the Harrisburg region when no target is given (backward compatible).
    have_target = any([args.bbox, args.county, args.region, args.all_counties])
    targets = resolve_targets(
        cfg, bbox=args.bbox, county=args.county,
        region=args.region if have_target else "harrisburg",
        all_counties_flag=args.all_counties, state=args.state, grid=args.grid,
    )

    print(f"Targets to process ({len(targets)}), all into {cfg.db_path}:")
    for label, bbox, grid in targets:
        print(f"  • {label:16s} {tuple(round(v, 3) for v in bbox)}  "
              f"{'[grid]' if grid else '[single]'}")
    for label, bbox, grid in targets:
        print(f"\n{'=' * 60}\n▶ {label}\n{'=' * 60}")
        (run_region if grid else run)(bbox, cfg)


def _cmd_enrich(args) -> None:
    from .enrich import enrich
    enrich(_config_from_args(args))


def _cmd_map(args) -> None:
    from .webmap import build_map
    build_map(_config_from_args(args), out_html=args.out,
              include_non_residential=args.all)


def _cmd_regrade(args) -> None:
    from .regrade import regrade
    regrade(_config_from_args(args))


def _add_db_arg(p) -> None:
    p.add_argument("--db", default=None, help="DuckDB path (default from Config)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="solargrader", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    # run
    r = sub.add_parser("run", help="score rooftops in a region")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--county", metavar="A,B,C", help="county names (comma-separated)")
    g.add_argument("--region", metavar="NAME", help=f"named area: {', '.join(sorted(REGIONS))}")
    g.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    g.add_argument("--all-counties", action="store_true",
                   help="every county in --state (whole-state, resumable)")
    r.add_argument("--state", default="PA", help="state for --county/--all-counties (default PA)")
    r.add_argument("--grid", action="store_true", help="force gridded run for a --bbox/--region")
    r.add_argument("--step", type=float, default=None, help="sub-region degrees when gridding")
    r.add_argument("--source", choices=["ms", "county", "osm"], default=None,
                   help="building footprint source (default ms)")
    r.add_argument("--workers", type=int, default=None, help="parallel worker processes")
    r.add_argument("--max-tiles", type=int, default=None, dest="max_tiles",
                   help="cap tiles processed this run")
    r.add_argument("--preferred-collection", default=None, dest="preferred_collection",
                   help="force one LiDAR collection (name substring)")
    r.add_argument("--list", action="store_true", help="list named regions and exit")
    _add_db_arg(r)
    r.set_defaults(func=_cmd_run)

    # enrich
    e = sub.add_parser("enrich", help="attach addresses + residential flag")
    _add_db_arg(e)
    e.set_defaults(func=_cmd_enrich)

    # map
    m = sub.add_parser("map", help="write a Leaflet map.html")
    _add_db_arg(m)
    m.add_argument("--out", default="map.html", help="output HTML path")
    m.add_argument("--all", action="store_true", help="include non-residential leads")
    m.set_defaults(func=_cmd_map)

    # regrade
    rg = sub.add_parser("regrade", help="re-grade in place (no reprocessing)")
    _add_db_arg(rg)
    rg.set_defaults(func=_cmd_regrade)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
