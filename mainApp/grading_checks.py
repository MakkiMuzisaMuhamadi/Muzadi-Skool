"""
Grading scale "health check".

A scale with a hole in it fails quietly: a learner whose score lands in the
hole gets no grade (or a wrong division) on the report card, and nobody
notices until a parent complains. These checks run when an admin opens a
scale and list every problem in plain language.

All functions are pure (no database), so they are easy to test and reuse.
"""
from decimal import Decimal


def _num(v):
    return Decimal(str(v))


def find_range_issues(bands, *, low, high, step=1, label="score"):
    """
    bands: iterable of (name, minimum, maximum).
    Returns a list of human-readable problems:
      * a band whose minimum is above its maximum,
      * ranges that overlap,
      * values between `low` and `high` that no band covers,
        measured in `step` units (1 = whole numbers).
    """
    issues = []
    low, high, step = _num(low), _num(high), _num(step)
    cleaned = []
    for name, lo, hi in bands:
        if lo is None or hi is None:
            issues.append(f"{name}: the range is incomplete.")
            continue
        lo, hi = _num(lo), _num(hi)
        if lo > hi:
            issues.append(f"{name}: minimum {lo:g} is higher than maximum {hi:g}.")
            continue
        cleaned.append((name, lo, hi))

    if not cleaned:
        return issues or [f"No ranges have been set for this scale."]

    cleaned.sort(key=lambda b: (b[1], b[2]))

    first_name, first_lo, _ = cleaned[0]
    if first_lo > low:
        issues.append(
            f"No grade covers {label} {low:g} to {first_lo - step:g} "
            f"(the lowest band, {first_name}, starts at {first_lo:g})."
        )

    covered_to = cleaned[0][2]
    prev_name = cleaned[0][0]
    for name, lo, hi in cleaned[1:]:
        if lo <= covered_to:
            issues.append(f"{prev_name} and {name} overlap ({label} {lo:g} to {min(hi, covered_to):g} is in both).")
        elif lo > covered_to + step:
            issues.append(
                f"Gap: no grade covers {label} {covered_to + step:g} to {lo - step:g} "
                f"(between {prev_name} and {name})."
            )
        if hi > covered_to:
            covered_to, prev_name = hi, name

    if covered_to < high:
        issues.append(f"No grade covers {label} {covered_to + step:g} to {high:g}.")

    return issues


def check_percentage_grades(grades):
    """grades: objects with grade_letter, min_percentage, max_percentage (PLE or UCE)."""
    return find_range_issues(
        [(g.grade_letter, g.min_percentage, g.max_percentage) for g in grades],
        low=0, high=100, step=1, label="marks",
    )


def check_ple_divisions(scale, grades, divisions):
    """
    Divisions must cover every aggregate a candidate can actually get:
    from best (required_subjects x best point) to worst
    (required_subjects x worst point), e.g. 4..36 for 4 subjects graded 1..9.
    """
    points = [g.aggregate_point for g in grades if g.aggregate_point]
    if not points:
        return ["Add subject grades with aggregate points before checking divisions."]
    n = scale.required_subjects or 0
    best, worst = n * min(points), n * max(points)
    return find_range_issues(
        [(d.division, d.min_aggregate, d.max_aggregate) for d in divisions],
        low=best, high=worst, step=1, label="aggregate",
    )


def ple_scale_issues(scale):
    grades = list(scale.grades.all())
    issues = [f"Subject grades - {m}" for m in check_percentage_grades(grades)]
    issues += [f"Divisions - {m}" for m in check_ple_divisions(scale, grades, list(scale.divisions.all()))]
    return issues


def uce_scale_issues(scale):
    return [f"Grades - {m}" for m in check_percentage_grades(list(scale.grades.all()))]
