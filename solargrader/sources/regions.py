"""
Area targeting — turn a place (county, named region, or explicit bbox) into the
bounding box(es) a run should cover. County boundaries come from US Census TIGERweb,
so any US county resolves by name with no hand-typed coordinates.
"""

from __future__ import annotations

import re

import requests

from ..config import Config
from ..util import with_retries

# A run target: (human label, bbox, whether to grid it into sub-regions).
Target = tuple[str, tuple[float, float, float, float], bool]

_STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}

# Convenience aliases — type ``--region <name>``. Each is an explicit bbox or one or
# more counties resolved live via TIGERweb.
REGIONS = {
    "harrisburg":       {"bbox": (-76.930, 40.240, -76.830, 40.310)},  # city + suburbs
    "dauphin":          {"counties": ["Dauphin"]},
    "cumberland":       {"counties": ["Cumberland"]},
    "perry":            {"counties": ["Perry"]},
    "york":             {"counties": ["York"]},
    "lancaster":        {"counties": ["Lancaster"]},
    "lebanon":          {"counties": ["Lebanon"]},
    "harrisburg-metro": {"counties": ["Dauphin", "Cumberland", "Perry"]},
}


def county_bbox(county: str, cfg: Config, state: str = "PA") -> tuple[tuple, str]:
    """Exact lon/lat bbox for a US county via TIGERweb. ``county`` is the bare name
    (e.g. 'Dauphin', not 'Dauphin County'), validated against a strict whitelist
    before it reaches the REST where-clause (trust-boundary hygiene)."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{0,48}", county or ""):
        raise ValueError(f"Invalid county name: {county!r}")
    fips = _STATE_FIPS.get(state.upper())
    if not fips:
        raise ValueError(f"Unknown state: {state!r}")
    data = with_retries(lambda: requests.get(cfg.tigerweb_counties_url, params={
        "where": f"BASENAME='{county}' AND STATE='{fips}'", "outFields": "NAME",
        "returnGeometry": "true", "outSR": "4326", "f": "json"}, timeout=60).json())
    feats = data.get("features") or []
    if not feats:
        raise ValueError(f"No county '{county}' found in {state.upper()}.")
    rings = feats[0]["geometry"]["rings"]
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    return (min(xs), min(ys), max(xs), max(ys)), feats[0]["attributes"]["NAME"]


def all_counties(cfg: Config, state: str = "PA") -> list[str]:
    """Every county name in a state, alphabetized, via TIGERweb (whole-state runs)."""
    fips = _STATE_FIPS.get(state.upper())
    if not fips:
        raise ValueError(f"Unknown state: {state!r}")
    data = with_retries(lambda: requests.get(cfg.tigerweb_counties_url, params={
        "where": f"STATE='{fips}'", "outFields": "BASENAME", "returnGeometry": "false",
        "orderByFields": "BASENAME", "f": "json"}, timeout=90).json())
    return [f["attributes"]["BASENAME"] for f in data.get("features", [])]


def resolve_targets(cfg: Config, *, bbox: tuple | None = None, county: str | None = None,
                    region: str | None = None, all_counties_flag: bool = False,
                    state: str = "PA", grid: bool = False) -> list[Target]:
    """Resolve a user's area choice into run targets (label, bbox, use_grid). All
    targets accumulate into the same DB; the resume ledgers dedup any overlap."""
    if all_counties_flag:
        names = all_counties(cfg, state)
        print(f"Resolving {len(names)} counties in {state.upper()}...")
        return [(*_county_target(cfg, n, state),) for n in names]
    if bbox:
        return [("bbox", tuple(bbox), grid)]
    if county:
        names = [c.strip() for c in county.split(",") if c.strip()]
        return [_county_target(cfg, n, state) for n in names]
    if region:
        spec = REGIONS.get(region.lower())
        if not spec:
            raise ValueError(f"Unknown region '{region}'. Options: {', '.join(sorted(REGIONS))}")
        if "bbox" in spec:
            return [(region, spec["bbox"], grid)]
        return [_county_target(cfg, n, spec.get("state", "PA")) for n in spec["counties"]]
    raise ValueError("No target given — pass bbox, county, region, or all_counties_flag.")


def _county_target(cfg: Config, name: str, state: str) -> Target:
    bbox, label = county_bbox(name, cfg, state)
    return (label, bbox, True)   # a county is always a gridded run
