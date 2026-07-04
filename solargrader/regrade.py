"""
Re-grade stored homes in place — no LiDAR reprocessing.

Grading is a pure function of columns already persisted (yield, kW, azimuth, max
kW), so tuning the grading policy never requires re-running the pipeline. This reads
every row, re-applies ``grade_home`` / ``grade_potential``, and reports the shift.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from .config import Config
from .grading import grade_home, grade_potential

_GRADE_ORDER = ["A+", "A", "B+", "B", "C", "D"]


def regrade(cfg: Config) -> None:
    con = duckdb.connect(cfg.db_path)
    homes = con.execute(
        "SELECT osm_id, res_annual_kwh, res_system_kw, primary_azimuth_deg, "
        "max_system_kw, solar_grade FROM homes"
    ).fetchdf()
    if homes.empty:
        print("No homes to re-grade — run the pipeline first.")
        con.close()
        return

    before = homes["solar_grade"].value_counts().to_dict()
    rows = []
    for r in homes.itertuples(index=False):
        grade, score = grade_home(r.res_annual_kwh, r.res_system_kw, r.primary_azimuth_deg)
        rows.append((int(r.osm_id), grade, int(score), grade_potential(r.max_system_kw)))

    con.register("new_df", pd.DataFrame(rows, columns=["osm_id", "grade", "score", "pot"]))
    con.execute("""
        UPDATE homes SET solar_grade = n.grade, solar_score = n.score,
                         potential_grade = n.pot
        FROM new_df n WHERE homes.osm_id = n.osm_id
    """)

    total = len(homes)
    after = con.execute("SELECT solar_grade, COUNT(*) FROM homes GROUP BY 1").fetchdf() \
        .set_index("solar_grade")["count_star()"].to_dict()
    con.close()

    print(f"Re-graded {total} homes.\n")
    print(f"  {'grade':>5}  {'before':>8}  {'after':>8}")
    for g in _GRADE_ORDER:
        b, a = before.get(g, 0), after.get(g, 0)
        print(f"  {g:>5}  {b:>8}  {a:>8}  ({100 * a / total:>4.1f}%)")
