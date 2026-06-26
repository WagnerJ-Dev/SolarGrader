---
name: Solar Grader Project Overview
description: What this project is, its goals, and current status
type: project
---

Application to score every home in Pennsylvania (~5M) for solar panel sales potential,
then surface A/A+ graded homes to sales reps with optimized daily routes.

**Why:** Zero-budget alternative to Google Solar API (~$25k for full PA). DIY pipeline
using free public data sources produces a permanent scored database with no per-query cost.

**Key data sources (all free):**
- USGS 3DEP LiDAR → roof geometry (pitch, azimuth, usable area)
- Microsoft Building Footprints (github.com/microsoft/buildings) → building polygons
- NREL NSRDB / PVGIS → solar irradiance
- OpenStreetMap Overpass API → building footprints for testing
- PASDA (pasda.psu.edu) → PA-specific parcel/address data

**How to apply:** When suggesting data sources, tools, or architecture, remember the
$0 budget constraint. Everything must be free or open source.

**Current status (as of 2026-06-25):** Pipeline plan written, test script created.
Next step: user runs test_pipeline.py against a single Chester County LiDAR tile
to validate density and RANSAC accuracy before committing to full PA run.
