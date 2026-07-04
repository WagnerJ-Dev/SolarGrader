"""
SolarGrader — score every rooftop in a region for solar sales potential from free
public data (USGS 3DEP LiDAR, Microsoft/OSM/county building footprints, EU PVGIS
irradiance), then surface the best leads.

The package is organized so the numerical core is pure functions (easy to test) and
only genuine state/interfaces are classes:

    config      Config dataclass — every tunable in one place
    models      lightweight dataclasses: Building, RoofPlane, SystemResult, ScoredHome
    geometry    pure: CRS reprojection, LiDAR clipping, area conversion
    roof        pure: RANSAC roof-plane extraction
    solar       pure: panel packing, plane-of-array irradiance, annual energy, shading
    grading     pure: yield-based residential grade + potential tier
    sources/    data acquisition: tiles, buildings, irradiance, regions
    storage     ResultStore — owns the DuckDB connection (schema, writes, resume ledgers)
    pipeline    streaming, parallel, resumable orchestration
    cli         command-line entry points

Heavy optional dependencies (PDAL, Open3D, pvlib) are imported lazily inside the
functions that need them, so the pure modules import with only numpy/shapely/pyproj.
"""

from .config import Config

__version__ = "0.1.0"
__all__ = ["Config", "__version__"]
