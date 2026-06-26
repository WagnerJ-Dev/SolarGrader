---
name: Pipeline Design — Streaming Tile Approach
description: How the pipeline avoids 2TB storage by streaming LiDAR tile by tile
type: project
---

Full PA LiDAR would be 500GB–2TB total, which the user doesn't have room for.
Solution: stream-process tile by tile, deleting each tile after processing.

**Storage at any moment:** ~25GB (one tile in memory + output DB + footprints + NSRDB)

**Tile source:** USGS S3 bucket `s3://prd-tnm/` — PDAL can read directly from S3 URLs,
no local download needed if running from AWS us-east-1 (zero egress cost).

**Processing time estimates:**
- ~2–5s per building (LiDAR I/O dominates)
- 8 cores → ~17 days for full PA
- 32 cores (Oracle Cloud free ARM) → ~4–5 days
- Parallelization is embarrassingly parallel (tiles are independent)

**Rollout order:** Chester County (validate) → Philadelphia metro → Pittsburgh metro →
remaining counties. Process county by county so runs can be safely interrupted/resumed.

**Key risk:** LiDAR point density varies by county. Need ≥1 pt/m² for reliable RANSAC.
Density check is built into test_pipeline.py output.

**How to apply:** Always frame storage discussions around the streaming approach.
User does not have 2TB available.
