"""ResultStore round-trip + resume ledgers, against a temporary DuckDB file."""

from shapely.geometry import box

from solargrader.models import Building, ScoredHome
from solargrader.storage import ResultStore


def _home(home_id: int, grade: str = "A+") -> ScoredHome:
    b = Building(id=home_id, geometry=box(0, 0, 1, 1), lat=40.0, lon=-76.0, footprint_m2=120.0)
    return ScoredHome(building=b, usable_area_m2=80, primary_tilt_deg=25,
                      primary_azimuth_deg=180, plane_count=2, res_kwh=13000, res_kw=10,
                      res_panels=25, max_kwh=15000, max_kw=12, max_panels=30,
                      potential_grade="P3", shade_loss_pct=5.0, grade=grade, score=90)


def test_store_roundtrip_and_ledgers(tmp_path):
    db = str(tmp_path / "t.duckdb")
    with ResultStore(db) as s:
        s.save(_home(1))
        s.save(_home(2, grade="B"))
        s.mark_tile_done("tileA", 2)
        s.mark_region_done("regionA")
        assert s.count_homes() == 2
        assert s.done_tiles() == {"tileA"}
        assert s.done_regions() == {"regionA"}
        assert s.grade_breakdown() == {"A+": 1, "B": 1}
    # State persists across reopen — the basis of resumability.
    with ResultStore(db) as s:
        assert s.count_homes() == 2
        assert s.done_tiles() == {"tileA"}


def test_save_is_idempotent(tmp_path):
    db = str(tmp_path / "t.duckdb")
    with ResultStore(db) as s:
        s.save(_home(1))
        s.save(_home(1))            # same id → INSERT OR REPLACE, not a duplicate
        assert s.count_homes() == 1
