# Data Sources & Attribution

Every dataset the Solar Grader pipeline uses is free, but several require
**attribution**, and one (building footprints) is **ODbL**. This file is the
canonical record of what we use and the credit we must display.

**Rule of thumb:** our *outputs* (grades, lead lists, maps) are ODbL "Produced
Works" — we may keep them proprietary and sell them, but we **must show the
attribution below** anywhere results are displayed or delivered. Share-alike only
applies if we were to publicly redistribute a building-footprint *database* itself,
which we don't.

| Source | Provides | License | Required credit |
|--------|----------|---------|-----------------|
| USGS 3DEP LiDAR | Roof geometry (point clouds) | Public domain (US Gov) | None required; courtesy: "Elevation data: USGS 3DEP" |
| Microsoft Building Footprints | Building outlines (scaling) | **ODbL** | **"Building footprints © Microsoft"** + note ODbL |
| OpenStreetMap (Overpass + basemap tiles) | Buildings (prototype) / map tiles | ODbL (data), tile policy (tiles) | **"© OpenStreetMap contributors"** |
| Chester County / PASDA | County footprints + address points | Public / open gov data | "Parcel & address data: Chester County GIS / PASDA" |
| EU PVGIS | Solar irradiance (TMY) | Free, attribution requested | "Solar data: EU PVGIS" |

## Canonical attribution string

Display this wherever results are shown (maps, exports, reports):

> Data: USGS 3DEP · Building footprints © Microsoft (ODbL) · © OpenStreetMap
> contributors · Chester County GIS/PASDA · EU PVGIS

## Notes for production

- **OSM basemap tiles** in `map.html` are fine for development but OSM's tile usage
  policy discourages heavy/commercial load — swap to a paid/self-hosted tile
  provider before launch (attribution to the chosen provider then applies).
- The pipeline exposes this string as `Config.attribution` (see
  `solargrader/config.py`) and shows it on generated maps; keep the two in sync.
- Not legal advice — a licensing review is worthwhile before commercial launch.
