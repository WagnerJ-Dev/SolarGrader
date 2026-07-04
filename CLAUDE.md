# SolarGrader — Engineering Guidelines for Claude Code

Score every PA home (~5M) for solar sales potential from free public data, then surface
A/A+ homes to reps with optimized routes. **Hard constraint: $0 budget** — every tool and
data source must be free or open source. Background context lives in `memory/` (project
overview, tech stack, pipeline design); read it, don't duplicate it here.

These principles are binding. When one conflicts with a quick fix, the principle wins —
or stop and flag the tradeoff. Don't silently take the easy path.

## 1. Design with structure — classes where there's state, functions where there isn't

Look for the pattern before writing code; build clean, named abstractions that scale to 5M
rows. But this is a numerical/geospatial pipeline, so **match structure to the work**:

- **Pure functions** for stateless transforms (tile lookup, RANSAC plane fit, pitch/azimuth,
  irradiance calc). Easy to test in isolation, no hidden state. This is most of the pipeline.
- **Classes** for genuine state or a real interface: a `TileProcessor`/`Pipeline` orchestrator,
  a `Config` dataclass (replace the module-level constants in `test_pipeline.py`), a DB writer
  that owns the DuckDB connection, a swappable irradiance source.
- Don't wrap stateless math in a class for ceremony's sake — it makes code harder to test, not
  easier. "Simple over complex" (see `memory/tech_stack.md`).
- One responsibility per unit. If a function spans tile I/O **and** RANSAC **and** DB writes,
  split it. `test_pipeline.py` is a script today; production code gets factored into a package.

## 2. Never hardcode secrets — environment variables only

- API keys / passwords / tokens are **never** inline literals. Read from `os.environ`.
- Real values live only in `.env` (gitignored). `.env.example` documents required vars with
  empty values. Keep them in sync — add a var to one, add it to the other.
- **Fail loud:** `os.environ["NREL_API_KEY"]` (KeyError if missing), never
  `os.environ.get("NREL_API_KEY", "<some-key>")`. A defaulted secret is a leaked secret.
- Concrete case now: the NREL NSRDB irradiance source needs a free key (`NREL_API_KEY`).
  Wire it through env from day one — don't paste it into a notebook "just to test."
- Never log, print, or commit a secret's value. Before any commit, scan the diff for keys.

## 3. Minimize USD cost — it's the whole point of this project

The reason this pipeline exists is to avoid Google Solar API's ~$25k. Protect that:

- **Free data sources only** (USGS 3DEP, MS Building Footprints, NSRDB/PVGIS, OSM, PASDA).
  Before adding any source/service, confirm it's free at PA scale and say so.
- **Stream, don't store.** Process LiDAR tile-by-tile and delete each after — the user does
  not have 2TB. Keep working-set storage bounded (~25GB). See `memory/pipeline_design.md`.
- **Cache to avoid re-fetching** (tiles already cache in `tile_cache/`). Don't re-download or
  re-compute what's on disk.
- **Think before brute force.** 5M homes × wasteful per-row work is real wall-clock and, on
  paid compute, real money. Vectorize with numpy/pandas; batch DB writes; design for the
  embarrassingly-parallel tile model (independent tiles, resumable county-by-county).
- If something is free locally but costs money at scale, say so up front.

## 4. Evaluate security for the operating domain

Threat model shifts by layer — check the one you're in:

- **Pipeline / data ingest:** treat external inputs (USGS/OSM/NSRDB responses, LiDAR files)
  as untrusted. Validate before use; never `eval`/`exec` fetched content; pin the schema you
  expect. Don't pass unsanitized strings into shell/PDAL pipeline JSON.
- **Database (DuckDB → maybe Postgres):** **parameterized queries only**, never string-built
  SQL with external values. This applies the moment any user/address input reaches a query.
- **Future web layer (FastAPI + Leaflet):** guard XSS (escape/encode all user-facing output),
  SQL injection (parameterized), CSRF on state-changing routes, authn/authz on rep data,
  rate-limit public endpoints. Address PII (5M home records) is sensitive — scope access.
- Validate at trust boundaries; fail closed.

## 5. Additional standards

- **Type hints + docstrings** on functions/classes; docstring says *what & why*, not line-by-line.
- **Testable & reproducible:** pure functions over hidden globals; keep determinism knobs
  explicit (RANSAC iterations/threshold/seed live in config). Validate accuracy on a known tile
  before scaling — never trust an unvalidated run across 5M homes.
- **Fail loud, recover gracefully:** raise on programmer error; for a bad tile, log and skip so
  a county run isn't lost. Long runs must be interruptible and resumable.
- **Reuse before adding deps** — the user keeps the stack lean (`memory/tech_stack.md`). Justify
  any new dependency; prefer what's already imported.
- **Leave the campsite cleaner:** small, focused changes; match surrounding style; no dead code.

## Working with this user

- **Explain before running.** Narrate what a command does and why before executing; no
  rapid-fire reruns. (`memory/` → explain-before-running)
- **The user runs `test_pipeline.py` himself** to watch the logs/density output. Don't run it
  for him — make the change, explain it, hand it back. (`memory/` → user-runs-the-pipeline)
- Prefer simple over complex. Don't add servers, Docker, or dependencies unless necessary.
