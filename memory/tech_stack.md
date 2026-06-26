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
- Mac: `brew install pdal` then `pip install -r requirements.txt` (no conda needed)
- Windows: conda-forge environment via environment.yml (PDAL native deps need conda on Windows)

**How to apply:** User prefers simple over complex. Don't suggest adding servers,
Docker, or extra dependencies unless absolutely necessary.
