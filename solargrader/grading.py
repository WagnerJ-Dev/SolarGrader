"""
Grading policy — pure functions turning physical results into sales grades.

Two independent axes:
  * ``grade_home``      residential lead quality (A+…D), driven by specific yield.
  * ``grade_potential`` maximum-roof capacity tier (P1…P5), the upside beyond a
                        standard residential system.

Thresholds are module-level constants so the scoring policy is easy to read and
tune in one place. Re-grading a stored DB never needs LiDAR reprocessing — see
``solargrader.regrade``.
"""

from __future__ import annotations

# Residential grade: specific-yield tiers (kWh per kW installed) and their points.
# Yield already integrates orientation, tilt, and shading into one physical number,
# so it is the dominant axis. PA rooftops span ~900–1500 kWh/kW.
_YIELD_POINTS = [(1400, 65), (1300, 52), (1200, 38), (1100, 24), (1000, 12)]
_YIELD_FLOOR = 4

# Orientation reinforces prime south-facing among similar-yield roofs.
_ORIENT_POINTS = [(15, 25), (45, 17), (75, 7)]

# Letter-grade cutoffs on the 0–100 score.
_GRADE_CUTOFFS = [(85, "A+"), (70, "A"), (55, "B+"), (40, "B"), (25, "C")]

# Potential tier cutoffs on max installable kW (DC).
_POTENTIAL_CUTOFFS = [(30, "P1"), (20, "P2"), (12, "P3"), (7, "P4")]


def grade_home(res_annual_kwh: float, res_system_kw: float,
               primary_azimuth_deg: float) -> tuple[str, int]:
    """Residential SALES grade (letter, 0–100 score).

    Driven by SPECIFIC YIELD (kWh/kW) rather than total energy: the residential
    system is capped, so total kWh saturates and stops discriminating (it collapsed
    every home to A/A+). Yield restores the spread — a south-facing unshaded PA roof
    lands ~1400–1500, a poorly-oriented or shaded one ~900–1000. Roof SIZE is the
    other grade's job (``grade_potential``), so it enters here only as a small-system
    penalty, never a reward the capped majority would all collect.
    """
    yield_kwh_per_kw = res_annual_kwh / res_system_kw if res_system_kw else 0.0
    score = _YIELD_FLOOR
    for threshold, points in _YIELD_POINTS:
        if yield_kwh_per_kw >= threshold:
            score = points
            break

    offset = abs(primary_azimuth_deg - 180)
    for max_offset, points in _ORIENT_POINTS:
        if offset <= max_offset:
            score += points
            break

    # Deal size: only penalize sub-residential roofs; don't reward the capped majority.
    if res_system_kw >= 7:
        score += 10
    elif res_system_kw >= 5:
        score += 5
    elif res_system_kw < 3:
        score -= 10

    for cutoff, letter in _GRADE_CUTOFFS:
        if score >= cutoff:
            return letter, score
    return "D", score


def grade_potential(max_kw: float) -> str:
    """Tier a roof by its MAXIMUM installable capacity (kW DC) — the upside beyond a
    standard residential system (battery, EV, expansion, or commercial). P1 = very
    high (large/commercial roof) … P5 = barely fits a full residential set."""
    for cutoff, tier in _POTENTIAL_CUTOFFS:
        if max_kw >= cutoff:
            return tier
    return "P5"
