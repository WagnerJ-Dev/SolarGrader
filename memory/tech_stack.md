---
name: Tech Stack Decisions
description: Chosen tools and the reasoning behind each
type: project
---

**Pipeline:** Python + PDAL (LiDAR I/O) + Open3D RANSAC (roof plane extraction) +
pvlib (solar calc) + Duckdb (storage)

**Database:** DuckDB — single file, no server, handles 5M+ records, good for analytics.
Chosen over PostGIS/PostgreSQL for simplicity. PostGIS = PostgreSQL + spatial extension,
not a separate DB. Migrate to PostgreSQL only if concurrent multi-worker writes become
a bottleneck.

**Routing:** OSRM (self-hosted, PA OSM data from Geofabrik) + Google OR-Tools for TSP.

**Frontend:** FastAPI + Leaflet.js + OpenStreetMap tiles.

**Environment:**
- Mac: `brew install pdal` then `pip install -r requirements.txt` in a venv (no conda needed)
- Windows: conda-forge environment via environment.yml (PDAL native deps need conda on Windows)

**PDAL pip gotcha (hit 2026-06-25):** The PyPI package is `pdal`, NOT `python-pdal`.
`python-pdal` is the *conda* name; on PyPI it's an empty 0.0.1 stub that installs
nothing. requirements.txt has been corrected to `pdal`. The pip `pdal` wheel builds
against the Homebrew libpdal via pdal-config, so `brew install pdal` must come first.
Verified working: Homebrew PDAL 2.10.2 + pip pdal 3.5.3 + Python 3.9.6 venv.

**How to apply:** User prefers simple over complex. Don't suggest adding servers,
Docker, or extra dependencies unless absolutely necessary.
