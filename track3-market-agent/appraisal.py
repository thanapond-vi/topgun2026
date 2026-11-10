"""Estimate a lot's True Value (TV) from its Sample, using the formula the market publishes.

The formula and its numbers come from GET /v1/calendar -> "rules", so nothing is hard-coded here.
"""
import math
import os


def grade(rules: dict, defect_pct: float) -> tuple[str, float]:
    for step in rules["defect_steps"]:
        if defect_pct <= step["max_defect_pct"]:
            return step["grade"], step["step"]
    last = rules["defect_steps"][-1]
    return last["grade"], last["step"]


def moisture_factor(rules: dict, m: float) -> float:
    mo = rules["moisture"]
    if m <= mo["ref"]:
        return 1.0
    if m <= mo["hard_limit"]:
        return (1 - m / 100) / (1 - mo["ref"] / 100) - mo["penalty_per_pp"] * (m - mo["ref"])
    return mo["hard_factor"]


def screen_factor(rules: dict, screen: int) -> float:
    factors = {int(k): v for k, v in rules["screen_factors"].items()}
    codes = sorted(factors)
    return factors[min(max(int(screen), codes[0]), codes[-1])]


def true_value(rules: dict, sample: dict) -> float:
    """TV = P0 × G(d) × M(m) × S(s), baht per kg."""
    d, m = sample["defect_pct"], sample["moisture_pct"]
    g = (1 - d / 100) * grade(rules, d)[1]
    return rules["p0"] * g * moisture_factor(rules, m) * screen_factor(rules, sample["screen"])


# Sample noise (standard deviation, percentage points) of defect_pct and moisture_pct versus the true lot.
# Off by default (0 = the plain formula): on the mock market (mock/run_games.py, 200 games per setting)
# the noise-aware estimate scored the same as the plain one, because with symmetric noise the gains and
# losses around a grade step cancel. Turn it on only if the sandbox (GET /v1/sandbox/reveal) shows the
# noise is lopsided, e.g. samples read worse than the true lot.
DEFECT_SD = float(os.environ.get("APPRAISAL_DEFECT_SD", "0"))
MOISTURE_SD = float(os.environ.get("APPRAISAL_MOISTURE_SD", "0"))
GRID = [i / 10 for i in range(-30, 31)]           # -3 sd ... +3 sd in steps of 0.1 sd


def robust_value(rules: dict, sample: dict, defect_sd: float = DEFECT_SD, moisture_sd: float = MOISTURE_SD) -> float:
    """The estimate with the smallest expected |error| / TV when the true lot may differ from the sample.

    The appraisal score falls with the mean relative error, so the best single answer is the median of the
    possible true values weighted by 1 / TV. Near a grade step (1, 3, 5, 7, 15 % defects) this differs from
    the plain formula: a sample at 1.1 % may really be a PREMIUM lot worth 4 % more.
    """
    if defect_sd <= 0 and moisture_sd <= 0:
        return true_value(rules, sample)
    points = []
    for zd in GRID:
        for zm in GRID if moisture_sd > 0 else [0.0]:
            w = math.exp(-(zd * zd + zm * zm) / 2)
            q = {"defect_pct": max(0.0, sample["defect_pct"] + zd * defect_sd),
                 "moisture_pct": sample["moisture_pct"] + zm * moisture_sd, "screen": sample["screen"]}
            v = true_value(rules, q)
            points.append((v, w / v))
    points.sort()
    half, acc = sum(w for _, w in points) / 2, 0.0
    for v, w in points:
        acc += w
        if acc >= half:
            return v
    return points[-1][0]


def estimate_lots(rules: dict, lots: list[dict], defect_sd: float = DEFECT_SD,
                  moisture_sd: float = MOISTURE_SD) -> dict[str, float]:
    return {lot["lot_id"]: round(robust_value(rules, lot["sample"], defect_sd, moisture_sd), 2) for lot in lots}


def appraisal_items(rules: dict, lots: list[dict], tv: dict[str, float]) -> list[dict]:
    return [{"lot_id": lot["lot_id"], "grade": grade(rules, lot["sample"]["defect_pct"])[0],
             "est_value_per_kg": tv[lot["lot_id"]]} for lot in lots]
