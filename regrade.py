"""
Solar Grader — Re-grade in place (no LiDAR re-run)

Re-applies the current grade_home() / grade_potential() logic to every home already
scored in the DB, using the stored physical measurements (yield, kW, azimuth). Grading
is a cheap pure function of columns we already persisted, so tuning the grade never
requires reprocessing point clouds — run this instead.

Run with:
    source .venv/bin/activate
    python regrade.py
"""

import warnings

import duckdb
import pandas as pd

from test_pipeline import grade_home, grade_potential

warnings.filterwarnings("ignore")

DB_PATH = "solar_grader.duckdb"


def main():
    con = duckdb.connect(DB_PATH)
    homes = con.execute(
        "SELECT osm_id, res_annual_kwh, res_system_kw, primary_azimuth_deg, "
        "max_system_kw, solar_grade FROM homes"
    ).fetchdf()
    if homes.empty:
        print("No homes to re-grade — run pipeline.py first.")
        return

    before = homes["solar_grade"].value_counts().to_dict()

    rows = []
    for r in homes.itertuples(index=False):
        grade, score = grade_home(r.res_annual_kwh, r.res_system_kw, r.primary_azimuth_deg)
        rows.append((int(r.osm_id), grade, int(score), grade_potential(r.max_system_kw)))

    new_df = pd.DataFrame(rows, columns=["osm_id", "grade", "score", "pot"])
    con.register("new_df", new_df)
    con.execute("""
        UPDATE homes SET
            solar_grade = n.grade, solar_score = n.score, potential_grade = n.pot
        FROM new_df n WHERE homes.osm_id = n.osm_id
    """)

    total = len(homes)
    order = ["A+", "A", "B+", "B", "C", "D"]
    after = con.execute(
        "SELECT solar_grade, COUNT(*) FROM homes GROUP BY 1"
    ).fetchdf().set_index("solar_grade")["count_star()"].to_dict()

    print(f"Re-graded {total} homes.\n")
    print(f"  {'grade':>5}  {'before':>8}  {'after':>8}")
    for g in order:
        b, a = before.get(g, 0), after.get(g, 0)
        print(f"  {g:>5}  {b:>8}  {a:>8}  ({100*a/total:>4.1f}%)")
    con.close()


if __name__ == "__main__":
    main()
