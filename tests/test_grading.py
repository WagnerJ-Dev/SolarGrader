"""Grading is a pure policy function — the cheapest, most important thing to pin."""

from solargrader.grading import grade_home, grade_potential


def test_prime_south_roof_is_top_grade():
    grade, score = grade_home(res_annual_kwh=14000, res_system_kw=10, primary_azimuth_deg=180)
    assert grade == "A+"
    assert score >= 85


def test_poorly_oriented_low_yield_roof_grades_low():
    grade, _ = grade_home(res_annual_kwh=9000, res_system_kw=10, primary_azimuth_deg=0)
    assert grade in ("C", "D")


def test_yield_dominates_when_orientation_and_size_fixed():
    _, low = grade_home(10000, 10, 180)
    _, high = grade_home(14000, 10, 180)
    assert high > low


def test_sub_residential_system_is_penalized():
    _, big = grade_home(14000, 10, 180)     # 10 kW, +10 size
    _, tiny = grade_home(1400, 1, 180)      # 1 kW, same yield, -10 size
    assert tiny < big


def test_grade_potential_tiers():
    assert grade_potential(35) == "P1"
    assert grade_potential(25) == "P2"
    assert grade_potential(15) == "P3"
    assert grade_potential(8) == "P4"
    assert grade_potential(3) == "P5"
