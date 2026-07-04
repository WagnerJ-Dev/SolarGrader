"""
Result storage — a thin class that OWNS the DuckDB connection.

DuckDB is single-writer, so exactly one ``ResultStore`` (in the main process) holds
the connection; worker processes never touch the DB. The store manages the schema,
scored-home writes, and the resume ledgers (which tiles / sub-regions are done).
"""

from __future__ import annotations

import duckdb
import pandas as pd

from .models import ScoredHome

_HOMES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS homes (
        osm_id                BIGINT PRIMARY KEY,
        lat                   DOUBLE,
        lon                   DOUBLE,
        footprint_m2          DOUBLE,
        usable_roof_area_m2   DOUBLE,
        primary_tilt_deg      DOUBLE,
        primary_azimuth_deg   DOUBLE,
        roof_plane_count      INTEGER,
        res_annual_kwh        DOUBLE,
        res_system_kw         DOUBLE,
        res_panel_count       INTEGER,
        solar_score           INTEGER,
        solar_grade           VARCHAR(2),
        max_annual_kwh        DOUBLE,
        max_system_kw         DOUBLE,
        max_panel_count       INTEGER,
        potential_grade       VARCHAR(2),
        shade_loss_pct        DOUBLE,
        processed_at          TIMESTAMP
    )
"""

_GRADE_ORDER = ["A+", "A", "B+", "B", "C", "D"]


class ResultStore:
    """Owns the accumulating results DB. Use as a context manager:

    >>> with ResultStore("solar_grader.duckdb") as store:
    ...     store.save(scored_home)
    """

    def __init__(self, db_path: str, read_only: bool = False):
        self.db_path = db_path
        self.con = duckdb.connect(db_path, read_only=read_only)
        if not read_only:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.con.execute(_HOMES_SCHEMA)
        self.con.execute("CREATE TABLE IF NOT EXISTS tiles_done "
                         "(tile_id VARCHAR PRIMARY KEY, n_scored INTEGER, processed_at TIMESTAMP)")
        self.con.execute("CREATE TABLE IF NOT EXISTS regions_done "
                         "(region_key VARCHAR PRIMARY KEY, processed_at TIMESTAMP)")

    # ── Writes ────────────────────────────────────────────────────────────────
    def save(self, home: ScoredHome) -> None:
        """Insert (or replace) one scored home."""
        b = home.building
        self.con.execute(
            "INSERT OR REPLACE INTO homes VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [b.id, b.lat, b.lon, b.footprint_m2, home.usable_area_m2,
             home.primary_tilt_deg, home.primary_azimuth_deg, home.plane_count,
             home.res_kwh, home.res_kw, home.res_panels, home.score, home.grade,
             home.max_kwh, home.max_kw, home.max_panels, home.potential_grade,
             home.shade_loss_pct, pd.Timestamp.now()],
        )

    # ── Resume ledgers ────────────────────────────────────────────────────────
    def mark_tile_done(self, tile_id: str, n_scored: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO tiles_done VALUES (?, ?, ?)",
                         [tile_id, n_scored, pd.Timestamp.now()])

    def done_tiles(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT tile_id FROM tiles_done").fetchall()}

    def mark_region_done(self, region_key: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO regions_done VALUES (?, ?)",
                         [region_key, pd.Timestamp.now()])

    def done_regions(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT region_key FROM regions_done").fetchall()}

    # ── Reporting ─────────────────────────────────────────────────────────────
    def count_homes(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM homes").fetchone()[0]

    def grade_breakdown(self) -> dict:
        rows = self.con.execute("SELECT solar_grade, COUNT(*) FROM homes GROUP BY 1").fetchall()
        return dict(rows)

    def print_summary(self, elapsed: float | None = None) -> None:
        n_homes = self.count_homes()
        n_tiles = self.con.execute("SELECT COUNT(*) FROM tiles_done").fetchone()[0]
        print(f"\n{'=' * 60}\nPIPELINE SUMMARY\n{'=' * 60}")
        print(f"  Tiles processed:   {n_tiles}")
        print(f"  Homes scored:      {n_homes}")
        if elapsed is not None:
            print(f"  Total time:        {elapsed:.1f}s")
        counts = self.grade_breakdown()
        print("\n  Grade breakdown:")
        for g in _GRADE_ORDER:
            n = counts.get(g, 0)
            if n:
                print(f"    {g:2s}  {'█' * min(n, 50)}  ({n})")
        print(f"\n  Results saved to: {self.db_path}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> ResultStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
