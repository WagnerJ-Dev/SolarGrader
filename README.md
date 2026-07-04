# SolarGrader

Score every rooftop in a region for **solar sales potential** from **free public
data** — USGS 3DEP LiDAR for roof geometry, Microsoft/OSM/county building
footprints, and EU PVGIS irradiance — then surface the best leads on a map.

The whole point is to replace a ~$25k Google Solar API bill with a **$0** pipeline:
every data source is free or open, LiDAR is streamed tile-by-tile and deleted after
scoring (bounded disk), and runs are resumable so you can grade a metro on a laptop
or a whole state over time.

## How it works

For each building footprint the pipeline clips the LiDAR point cloud, fits roof
planes with RANSAC, models a skyline from surrounding points (tree/neighbor
shading), computes plane-of-array irradiance with pvlib, packs discrete panels onto
each plane, and assigns two grades:

- **Residential grade (A+…D)** — lead quality, driven by *specific yield* (kWh per kW
  installed), which already folds in orientation, tilt, and shading.
- **Potential tier (P1…P5)** — the maximum-roof capacity (upside beyond a standard
  residential system).

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The LiDAR reader needs **PDAL**, a system library best installed via conda-forge
(the pip `pdal` package binds to it):

```bash
conda install -c conda-forge pdal python-pdal   # provides libpdal + bindings
# then, in the same environment:  pip install -e ".[dev,lidar]"
```

Everything except reading `.laz` tiles works without PDAL, so the tests and pure
modules install with pip alone.

## Usage

```bash
solargrader run --region harrisburg           # a named area
solargrader run --county Dauphin,Cumberland   # one or more counties (gridded)
solargrader run --all-counties --state PA      # a whole state (long, resumable)
solargrader run --bbox -77.0 40.2 -76.8 40.4   # an explicit lon/lat box
solargrader run --list                         # show named regions

solargrader enrich                             # attach addresses + residential flag
solargrader map && open map.html               # write the Leaflet map
solargrader regrade                            # re-grade in place (no reprocessing)
```

Every run accumulates into one DuckDB file and is **resumable** — Ctrl-C and re-run;
finished tiles, sub-regions, and shared borders are skipped automatically. The old
`python pipeline.py …` / `enrich_addresses.py` / `make_map.py` commands still work as
thin shims.

Validate the physics on a single tile before scaling:

```bash
python examples/validate_single_tile.py
```

## Architecture

```
solargrader/
  config.py      Config dataclass — every tunable in one place
  models.py      dataclasses: Building, RoofPlane, SystemResult, ScoredHome
  geometry.py    pure: CRS reprojection, LiDAR clipping, area conversion
  roof.py        pure: RANSAC roof-plane extraction
  solar.py       pure: panel packing, POA irradiance, annual energy, shading
  grading.py     pure: yield-based residential grade + potential tier
  sources/       tiles · buildings · irradiance · regions (all free sources)
  storage.py     ResultStore — owns the DuckDB connection + resume ledgers
  pipeline.py    streaming, parallel, resumable orchestration
  cli.py         command-line entry points
```

The numerical core is **pure functions** (trivially testable); only genuine state or
swappable interfaces are classes (`Config`, `ResultStore`). Heavy optional
dependencies (PDAL, Open3D, pvlib) are imported lazily, so the pure modules import
with just numpy/shapely/pyproj.

## Data sources & attribution

Every source is free, but several require attribution and building footprints are
ODbL. See [DATA_SOURCES.md](DATA_SOURCES.md); the required credit string lives on
`Config.attribution` and is shown on generated maps.

## Development

```bash
ruff check solargrader tests
pytest -q
```

CI runs lint + the pure-function tests on Python 3.9 and 3.11.

## License

MIT (see `pyproject.toml`). Note the ODbL attribution requirement for building
footprints in `DATA_SOURCES.md`.
