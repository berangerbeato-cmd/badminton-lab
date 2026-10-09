#!/usr/bin/env python3
"""Badminton Lab V3.1 date-safe pre-match feature research.

Builds six feature families from the existing BWF MS archive without using
bookmaker odds or future/same-day outcomes:
  1. adjusted recent form (last 10 outcome residuals vs calibrated Elo)
  2. fatigue (7/14-day match and set load)
  3. rest/inactivity (days since last match and 30-day inactivity flag)
  4. adjusted H2H (24-month outcome residuals vs calibrated Elo)
  5. Elo momentum (30/90-day rating change)
  6. recent score dominance (last 5 set margin and point share)

The current calibrated Elo/Platt model remains the baseline. Every V3 variant
is fit on 2024 only and evaluated on the untouched 2025 holdout. A retrospective
2026 replay is reported separately and is never described as archived live T0.

No betting, no bookmaker access, and no Google Sheets writes.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from elo_model import INITIAL_ELO, elo_p, load_matches
from elo_calibrate_daily import calibrate, logit, sigmoid

MODEL_NAME = "bwf_v3_1_feature_logit"
RIDGE = 5.0
TRAIN_YEAR = 2024
HOLDOUT_YEAR = 2025
REPLAY_YEAR = 2026

FEATURE_NAMES = [
    "form10_residual_diff",
    "matches7_diff",
    "sets7_diff",
    "matches14_diff",
    "rest_days_diff",
    "inactive30_diff",
    "h2h24_residual",
    "elo_trend30_diff",
    "elo_trend90_diff",
    "set_margin5_diff",
    "point_share5_diff",
]

FAMILIES = {
    "form": ["form10_residual_diff"],
    "fatigue": ["matches7_diff", "sets7_diff", "matches14_diff"],
    "rest_inactivity": ["rest_days_diff", "inactive30_diff"],
    "h2h": ["h2h24_residual"],
    "elo_momentum": ["elo_trend30_diff", "elo_trend90_diff"],
    "score_dominance": ["set_margin5_diff", "point_share5_diff"],
}

VARIANTS = {
    "elo_v1_baseline": [],
    "elo_plus_form": FAMILIES["form"],
    "elo_plus_fatigue": FAMILIES["fatigue"],
    "elo_plus_rest_inactivity": FAMILIES["rest_inactivity"],
    "elo_plus_h2h": FAMILIES["h2h"],
    "elo_plus_momentum": FAMILIES["elo_momentum"],
    "elo_plus_score": FAMILIES["score_dominance"],
    "elo_v3_1_all": FEATURE_NAMES,
}


@dataclass
class PlayerObs:
    day: date
    actual: float
    expected: float
    sets_for: int
    sets_against: int
    points_for: int
    points_against: int


@dataclass
class H2HObs:
    day: date
    a_id: str
    b_id: str
    winner: int
    expected_a: float


def parse_score(score: str) -> tuple[int, int, int, int, int]:
    sets_a = sets_b = points_a = points_b = games = 0
    for token in (score or "").split():
        if "-" not in token:
            continue
        x, y = token.split("-", 1)
        try:
            x, y = int(x), int(y)
        except ValueError:
            continue
        games += 1
        points_a += x
        points_b += y
        if x > y:
            sets_a += 1
        elif y > x:
            sets_b += 1
    return sets_a, sets_b, points_a, points_b, games


def mean(values: list[float], default: float = 0.0) -> float:
    return sum(values) / len(values) if values else default


def recent(hist: list[PlayerObs], today: date, days: int | None = None, n: int | None = None) -> list[PlayerObs]:
    rows = hist
    if days is not None:
        cutoff = today - timedelta(days=days)
        rows = [x for x in rows if cutoff <= x.day < today]
    if n is not None:
        rows = rows[-n:]
    return rows


def form10(hist: list[PlayerObs]) -> float:
    rows = hist[-10:]
    return mean([x.actual - x.expected for x in rows])


def load_stats(hist: list[PlayerObs], today: date) -> tuple[float, float, float, float, float]:
    d7 = recent(hist, today, days=7)
    d14 = recent(hist, today, days=14)
    matches7 = float(len(d7))
    sets7 = float(sum(x.sets_for + x.sets_against for x in d7))
    matches14 = float(len(d14))
    if hist:
        rest = float(min(60, max(0, (today - hist[-1].day).days)))
    else:
        rest = 60.0
    inactive30 = 1.0 if rest >= 30.0 else 0.0
    return matches7, sets7, matches14, rest, inactive30


def score_dominance(hist: list[PlayerObs]) -> tuple[float, float]:
    rows = hist[-5:]
    if not rows:
        return 0.0, 0.0
    set_margin = mean([float(x.sets_for - x.sets_against) for x in rows])
    shares = []
    for x in rows:
        total = x.points_for + x.points_against
        shares.append((x.points_for / total - 0.5) if total else 0.0)
    return set_margin, mean(shares)


def rating_at_or_before(history: list[tuple[date, float]], cutoff: date) -> float | None:
    for d, r in reversed(history):
        if d <= cutoff:
            return r
    return None


def elo_trends(current: float, history: list[tuple[date, float]], today: date) -> tuple[float, float]:
    r30 = rating_at_or_before(history, today - timedelta(days=30))
    r90 = rating_at_or_before(history, today - timedelta(days=90))
    return ((current - r30) if r30 is not None else 0.0,
            (current - r90) if r90 is not None else 0.0)


def h2h_residual(rows: list[H2HObs], today: date, player_a: str) -> float:
    cutoff = today - timedelta(days=730)
    valid = [r for r in rows if cutoff <= r.day < today]
    if not valid:
        return 0.0
    residual = 0.0
    for r in valid:
        if r.a_id == player_a:
            actual = 1.0 if r.winner == 1 else 0.0
            expected = r.expected_a
        else:
            actual = 1.0 if r.winner == 2 else 0.0
            expected = 1.0 - r.expected_a
        residual += actual - expected
    # Two pseudo-matches shrink tiny H2H samples toward zero.
    return residual / (len(valid) + 2.0)


def diff_features(a_hist: list[PlayerObs], b_hist: list[PlayerObs],
                  a_rhist: list[tuple[date, float]], b_rhist: list[tuple[date, float]],
                  h2h: list[H2HObs], today: date, a_id: str,
                  rating_a: float, rating_b: float) -> dict[str, float]:
    a_load = load_stats(a_hist, today)
    b_load = load_stats(b_hist, today)
    a_mom = elo_trends(rating_a, a_rhist, today)
    b_mom = elo_trends(rating_b, b_rhist, today)
    a_score = score_dominance(a_hist)
    b_score = score_dominance(b_hist)
    return {
        "form10_residual_diff": form10(a_hist) - form10(b_hist),
        "matches7_diff": a_load[0] - b_load[0],
        "sets7_diff": a_load[1] - b_load[1],
        "matches14_diff": a_load[2] - b_load[2],
        "rest_days_diff": a_load[3] - b_load[3],
        "inactive30_diff": a_load[4] - b_load[4],
        "h2h24_residual": h2h_residual(h2h, today, a_id),
        "elo_trend30_diff": a_mom[0] - b_mom[0],
        "elo_trend90_diff": a_mom[1] - b_mom[1],
        "set_margin5_diff": a_score[0] - b_score[0],
        "point_share5_diff": a_score[1] - b_score[1],
    }


def build_rows(matches, k: int, slope: float, intercept: float) -> list[dict]:
    rating = defaultdict(lambda: INITIAL_ELO)
    histories: dict[str, list[PlayerObs]] = defaultdict(list)
    rating_history: dict[str, list[tuple[date, float]]] = defaultdict(list)
    h2h_hist: dict[tuple[str, str], list[H2HObs]] = defaultdict(list)
    out = []
    pos = 0
    while pos < len(matches):
        start = pos
        day_str = matches[pos].date
        today = date.fromisoformat(day_str)
        while pos < len(matches) and matches[pos].date == day_str:
            pos += 1
        batch = matches[start:pos]
        pending = []
        for m in batch:
            ra, rb = rating[m.p1_id], rating[m.p2_id]
            raw_p = elo_p(ra, rb)
            base_p = calibrate(raw_p, slope, intercept)
            pair = tuple(sorted((m.p1_id, m.p2_id)))
            features = diff_features(
                histories[m.p1_id], histories[m.p2_id],
                rating_history[m.p1_id], rating_history[m.p2_id],
                h2h_hist[pair], today, m.p1_id, ra, rb,
            )
            sa, sb, pa, pb, games = parse_score(m.score)
            row = {
                "match_id": m.key, "date": m.date, "year": int(m.date[:4]),
                "tournament": m.tourney, "round": m.round,
                "player_a_id": m.p1_id, "player_a": m.p1_name,
                "player_b_id": m.p2_id, "player_b": m.p2_name,
                "rating_a_pre": ra, "rating_b_pre": rb,
                "raw_p_a": raw_p, "elo_v1_p_a": base_p,
                "winner": m.winner, "score": m.score,
                **features,
            }
            out.append(row)
            pending.append((m, raw_p, base_p, sa, sb, pa, pb, games))

        # Apply every same-day result only after every same-day forecast/features exist.
        delta = defaultdict(float)
        touched = set()
        for m, raw_p, base_p, sa, sb, pa, pb, games in pending:
            y = 1.0 if m.winner == 1 else 0.0
            shift = k * (y - raw_p)
            delta[m.p1_id] += shift
            delta[m.p2_id] -= shift
            touched.update((m.p1_id, m.p2_id))
            histories[m.p1_id].append(PlayerObs(today, y, base_p, sa, sb, pa, pb))
            histories[m.p2_id].append(PlayerObs(today, 1.0-y, 1.0-base_p, sb, sa, pb, pa))
            pair = tuple(sorted((m.p1_id, m.p2_id)))
            h2h_hist[pair].append(H2HObs(today, m.p1_id, m.p2_id, m.winner, base_p))
        for pid in touched:
            rating[pid] += delta[pid]
            rating_history[pid].append((today, rating[pid]))
    return out


def standardization(rows: list[dict], names: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    means, scales = {}, {}
    for name in names:
        vals = [float(r[name]) for r in rows]
        mu = mean(vals)
        var = mean([(v-mu)**2 for v in vals])
        means[name] = mu
        scales[name] = math.sqrt(var) if var > 1e-12 else 1.0
    return means, scales


def design(row: dict, names: list[str], means: dict[str, float], scales: dict[str, float]) -> list[float]:
    # intercept, Elo logit, then standardized extra features
    return [1.0, logit(float(row["raw_p_a"]))] + [
        (float(row[n]) - means[n]) / scales[n] for n in names
    ]


def solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("Singular Hessian")
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        for j in range(col, n+1):
            m[col][j] /= div
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            if factor == 0:
                continue
            for j in range(col, n+1):
                m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def fit_variant(rows: list[dict], names: list[str], ridge: float = RIDGE) -> dict:
    means, scales = standardization(rows, names)
    beta = [0.0, 1.0] + [0.0] * len(names)  # intercept=0, Elo slope=1 priors
    priors = [0.0, 1.0] + [0.0] * len(names)
    xs = [design(r, names, means, scales) for r in rows]
    ys = [1.0 if r["winner"] == 1 else 0.0 for r in rows]
    for _ in range(60):
        d = len(beta)
        grad = [ridge * (beta[j] - priors[j]) for j in range(d)]
        hess = [[0.0] * d for _ in range(d)]
        for j in range(d):
            hess[j][j] = ridge
        for x, y in zip(xs, ys):
            z = sum(b*xv for b, xv in zip(beta, x))
            p = sigmoid(z)
            err = p - y
            w = p * (1.0-p)
            for j in range(d):
                grad[j] += err * x[j]
                for k in range(j+1):
                    hess[j][k] += w * x[j] * x[k]
        for j in range(len(beta)):
            for k in range(j):
                hess[k][j] = hess[j][k]
        step = solve_linear(hess, grad)
        max_step = max(abs(v) for v in step)
        # Same spirit as V1: control pathological Newton jumps.
        if max_step > 0.5:
            factor = 0.5 / max_step
            step = [v*factor for v in step]
        beta = [b-s for b, s in zip(beta, step)]
        if sum(abs(v) for v in step) < 1e-9:
            break
    return {"features": names, "means": means, "scales": scales, "beta": beta}


def predict(row: dict, model: dict) -> float:
    x = design(row, model["features"], model["means"], model["scales"])
    return sigmoid(sum(b*xv for b, xv in zip(model["beta"], x)))


def metrics(rows: list[dict], probs: list[float]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    ys = [1.0 if r["winner"] == 1 else 0.0 for r in rows]
    brier = sum((p-y)**2 for p, y in zip(probs, ys)) / n
    ll = -sum(y*math.log(max(1e-12,p)) + (1-y)*math.log(max(1e-12,1-p)) for p,y in zip(probs,ys))/n
    acc = sum((p >= 0.5) == bool(y) for p,y in zip(probs,ys))/n
    return {"n": n, "brier": round(brier, 6), "log_loss": round(ll, 6), "accuracy": round(acc, 6)}


def coefficient_map(model: dict) -> dict[str, float]:
    names = ["intercept", "elo_logit"] + model["features"]
    return {n: round(v, 6) for n, v in zip(names, model["beta"])}


def run(data_dir: Path, model_dir: Path, output_dir: Path) -> dict:
    matches, quality = load_matches(data_dir)
    cal = json.loads((model_dir / "elo_calibration_report.json").read_text(encoding="utf-8"))
    k = int(cal["k"])
    slope = float(cal["platt_slope"])
    intercept = float(cal["platt_intercept"])
    rows = build_rows(matches, k, slope, intercept)
    train = [r for r in rows if r["year"] == TRAIN_YEAR]
    holdout = [r for r in rows if r["year"] == HOLDOUT_YEAR]
    replay = [r for r in rows if r["year"] == REPLAY_YEAR]
    if len(train) < 100 or len(holdout) < 100:
        raise ValueError("Insufficient 2024/2025 rows for V3.1 evaluation")

    report = {
        "schema_version": "badminton_v3_1_features_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": "MODEL_RESEARCH_ONLY_NO_ODDS_NO_BET_NO_T0_REWRITE",
        "model": MODEL_NAME,
        "feature_families": FAMILIES,
        "training_year": TRAIN_YEAR,
        "independent_holdout_year": HOLDOUT_YEAR,
        "retrospective_replay_year": REPLAY_YEAR,
        "ridge": RIDGE,
        "elo_k": k,
        "source_quality": quality,
        "methodology": [
            "All features for a UTC date use results strictly before that date.",
            "Same-day results are applied only after all forecasts/features for that date are frozen.",
            "Every V3 coefficient is fit using 2024 only; 2025 remains the comparison holdout.",
            "2026 is retrospective replay only, never represented as archived live T0.",
            "No bookmaker odds, P&L or ROI are used in V3.1 model selection.",
        ],
        "variants": {},
    }

    baseline_brier_2025 = None
    for variant, names in VARIANTS.items():
        model = fit_variant(train, names)
        p25 = [predict(r, model) for r in holdout]
        p26 = [predict(r, model) for r in replay]
        m25 = metrics(holdout, p25)
        m26 = metrics(replay, p26)
        if variant == "elo_v1_baseline":
            baseline_brier_2025 = m25["brier"]
            # Verify our generalized logistic fitter reproduces V1 calibration closely.
            if abs(model["beta"][1] - slope) > 5e-6 or abs(model["beta"][0] - intercept) > 5e-6:
                raise AssertionError("Generalized baseline no longer reproduces V1 Platt fit")
        report["variants"][variant] = {
            "features": names,
            "coefficients_standardized": coefficient_map(model),
            "holdout_2025": m25,
            "replay_2026": m26,
        }

    for variant, item in report["variants"].items():
        item["holdout_2025"]["brier_delta_vs_elo_v1"] = round(
            item["holdout_2025"]["brier"] - baseline_brier_2025, 6)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    fields = [
        "match_id", "date", "tournament", "round", "player_a_id", "player_a",
        "player_b_id", "player_b", "raw_p_a", "elo_v1_p_a", "winner", "score",
    ] + FEATURE_NAMES
    with (output_dir / "features_2024_2026.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if r["year"] in (2024, 2025, 2026):
                w.writerow({k: (round(v, 8) if isinstance(v, float) else v) for k,v in r.items()})
    return report


def self_test() -> None:
    from types import SimpleNamespace
    def M(key, d, a, b, winner, score):
        return SimpleNamespace(key=key, date=d, tourney="Test", round="R1",
            p1_id=a, p1_name=a, p2_id=b, p2_name=b, winner=winner, score=score)
    sample = [
        M("1", "2024-01-01", "A", "B", 1, "21-10 21-10"),
        M("2", "2024-01-01", "A", "C", 2, "21-18 18-21 15-21"),
        M("3", "2024-01-03", "A", "B", 1, "21-19 21-17"),
    ]
    rows = build_rows(sample, 48, 1.0, 0.0)
    assert rows[0]["form10_residual_diff"] == 0.0
    # Same-day second match must not see result from first match.
    assert rows[1]["form10_residual_diff"] == 0.0
    # Two prior same-day matches become available on the later date.
    assert rows[2]["matches7_diff"] == 1.0
    assert rows[2]["sets7_diff"] == 3.0
    assert rows[2]["h2h24_residual"] > 0.0
    # Score parser sanity.
    assert parse_score("21-10 18-21 21-19") == (2,1,60,50,3)
    print("V3.1 self-test OK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    ap.add_argument("--model-dir", type=Path, default=Path("data/model"))
    ap.add_argument("--output", type=Path, default=Path("data/model_v3_1"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    report = run(args.data_dir, args.model_dir, args.output)
    print(json.dumps({
        "generated_at_utc": report["generated_at_utc"],
        "holdout": {k:v["holdout_2025"] for k,v in report["variants"].items()},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
