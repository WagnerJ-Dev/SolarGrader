"""
Central configuration for the whole pipeline.

Everything tunable lives on one ``Config`` dataclass instead of scattered module
globals, so a run is fully described by a single object (easy to override per run,
easy to test, easy to serialize into worker processes). Secrets are never stored
here as literals — they are read from the environment on demand (see
``require_nrel_api_key``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """All pipeline settings. Construct with defaults and override fields as needed:

    >>> cfg = Config(building_source="ms", n_workers=8)
    """

    # ── Panel / system model (all physical — change to model different hardware) ──
    panel_width_m: float = 1.05          # standard residential module footprint
    panel_height_m: float = 1.74
    panel_watts: int = 400               # module rated DC power at STC (1000 W/m²)
    roof_setback_m: float = 0.46         # fire-code clear pathway from plane edges (~18")
    panel_packing: float = 0.90          # fraction of setback area that packs into modules
    system_losses: float = 0.86          # inverter + wiring + soiling (~14% loss)
    max_azimuth_offset: float = 120.0    # skip planes facing >this many ° from due south
    max_system_kw: float = 10.0          # cap for a typical sellable residential install
    shade_radius_m: float = 50.0         # search radius for shading obstructions
    horizon_bins: int = 72               # azimuth bins for the roof skyline (5° each)

    # ── RANSAC roof-plane fitting ────────────────────────────────────────────────
    min_roof_points: int = 20            # skip buildings with fewer LiDAR points
    ransac_iterations: int = 300         # lower = faster, higher = more accurate
    ransac_distance_threshold: float = 0.20   # meters a point may sit off a plane

    # ── Pipeline / storage ───────────────────────────────────────────────────────
    db_path: str = "solar_grader.duckdb"      # accumulating, resumable results DB
    tile_cache_dir: str = "tile_stream"       # tiles land here, deleted after scoring
    delete_tiles_after: bool = True           # storage cap; resume covers re-runs
    n_workers: int = field(default_factory=lambda: min(4, os.cpu_count() or 2))
    step_deg: float = 0.05                     # sub-region size when gridding a region

    # ── Building inventory source: "ms" | "county" | "osm" ───────────────────────
    building_source: str = "ms"

    # ── LiDAR collection selection ───────────────────────────────────────────────
    # None → auto-select the best-quality mosaic per region (density-ranked). Set to
    # a project-name substring to force one collection. ``max_tiles`` caps a run for
    # a quick first validation (None = process the whole region).
    preferred_collection: str | None = None
    max_tiles: int | None = None

    # ── Service endpoints (free data sources) ────────────────────────────────────
    tnm_url: str = "https://tnmaccess.nationalmap.gov/api/v1/products"
    index_lpc_url: str = ("https://index.nationalmap.gov/arcgis/rest/services/"
                          "3DEPElevationIndex/MapServer/8/query")
    county_footprints_url: str = ("https://mapservices.pasda.psu.edu/server/rest/services/"
                                  "pasda/ChesterCounty/MapServer/14/query")
    ms_dataset_links_url: str = ("https://minedbuildings.z5.web.core.windows.net/"
                                 "global-buildings/dataset-links.csv")
    ms_cache_dir: str = "ms_cache"
    ms_zoom: int = 9
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    tigerweb_counties_url: str = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                                  "TIGERweb/State_County/MapServer/1/query")

    # ── Networking ───────────────────────────────────────────────────────────────
    http_user_agent: str = "SolarGrader/1.0 (https://github.com/)"
    retry_attempts: int = 4
    retry_base_delay: float = 5.0

    # ── Required attribution — display wherever results are shown (DATA_SOURCES.md)
    attribution: str = ("Data: USGS 3DEP · Building footprints © Microsoft (ODbL) · "
                        "© OpenStreetMap contributors · Chester County GIS/PASDA · EU PVGIS")

    # ── Derived quantities (kept as properties so they track the fields above) ────
    @property
    def panel_area_m2(self) -> float:
        """Module area in m² (width × height)."""
        return self.panel_width_m * self.panel_height_m

    @property
    def module_efficiency(self) -> float:
        """STC efficiency implied by the rated watts and module area."""
        return self.panel_watts / (self.panel_area_m2 * 1000.0)

    @property
    def residential_panel_cap(self) -> int:
        """Whole panels that fit under the residential kW cap."""
        return int(self.max_system_kw * 1000 / self.panel_watts)

    # ── Secrets: read from the environment, never defaulted (a defaulted secret is
    #    a leaked secret). Only needed if you wire the optional NREL NSRDB source. ─
    def require_nrel_api_key(self) -> str:
        """Return NREL_API_KEY from the environment, raising if it is unset."""
        return os.environ["NREL_API_KEY"]


# A ready-to-use default instance for simple scripts and the CLI's defaults.
DEFAULT = Config()
