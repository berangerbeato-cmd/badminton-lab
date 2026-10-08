#!/usr/bin/env python3
"""Elo V0 for BWF singles (MS), with chronological 2024 validation and 2025 holdout.

Reads the persistent JSON.gz archives collected by bwf_sync.py; never calls
BWF or uses bookmaker prices. All ratings/probabilities on a date are computed
using results strictly before that UTC calendar date. First-time players have
rating 1500. No prediction for a completed 2026 match is treated as live T0.

Run: python elo_model.py --data-dir data/bwf --output-dir data/model
Test: python elo_model.py --self-test
Python 3.12 standard library only.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile

MODEL_NAME = "bwf_elo_ms_v0"
START_YEAR = 2018
VALIDATION_YEAR = 2024
TEST_YEAR = 2025
INITIAL_ELO = 1500.0
SCALE = 400.0
K_CANDIDATES = (12, 20, 28, 36, 48, 64)
GAME_SPAN = re.compile(r"<span>\s*(\d+)\s*</span>", re.I)


@dataclass(frozen=True)
class Match:
    key: str
    date: str
    tourney: str
    round: str
    p1_id: str
    p1_name: str
    p2_id: str
    p2_name: str
    winner: int
    score: str


def _matches(payload):
    res = payload.get("results") or {}
    if not isinstance(res, dict):
        return []
    found = {}
    by_time = (res.get("by_time") or {}).get("time_group") or []
    for item in by_time:
        if isinstance(item, dict) and item.get("id") is not None:
            found[str(item["id"])] = item
    for court in (res.get("by_court") or {}).values():
        if not isinstance(court, dict):
            continue
        for item in court.values():
            if isinstance(item, dict) and item.get("id") is not None:
                k = str(item["id"])
                if k not in found or (item.get("winner") in (1, 2) and found[k].get("winner") not in (1, 2)):
                    found[k] = item
    return list(found.values())


def _make_match(tid, tname, m, counters):
    if (m.get("draw_name") or "").strip().split(" ")[0].upper() != "MS":
        counters["not_MS"] += 1
        return None
    winner = m.get("winner")
    if winner not in (1, 2):
        counters["no_winner"] += 1
        return None
    p1, p2 = m.get("t1p1_detail"), m.get("t2p1_detail")
    if not isinstance(p1, dict) or not isinstance(p2, dict) or not p1.get("id") or not p2.get("id"):
        counters["missing_player_ids"] += 1
        return None
    if (m.get("t1p2_detail") or {}).get("id") or (m.get("t2p2_detail") or {}).get("id"):
        counters["double_mislabeled_MS"] += 1
        return None
    if str(p1["id"]) == str(p2["id"]):
        counters["same_player_id"] += 1
        return None
    a = [int(x) for x in GAME_SPAN.findall(m.get("team1Score") or "")]
    b = [int(x) for x in GAME_SPAN.findall(m.get("team2Score") or "")]
    if len(a) not in (2, 3) or len(a) != len(b):
        counters["no_full_score"] += 1
        return None
    # Reject matches that never reached a completed best-of-3 result; catches
    # walkovers, interruptions and most retirements. The source winner is kept.
    wins_a = sum(x > y and x >= 21 and x-y >= 2 or x == 30 and x > y for x, y in zip(a, b))
    wins_b = sum(y > x and y >= 21 and y-x >= 2 or y == 30 and y > x for x, y in zip(a, b))
    # A 30-point game is completed even at 30-29. Normally winner has 2 games.
    if max(wins_a, wins_b) != 2 or (winner == 1 and wins_a != 2) or (winner == 2 and wins_b != 2):
        counters["score_not_completed"] += 1
        return None
    try:
        d = datetime.fromtimestamp(int(m["start_time"]), tz=timezone.utc).date().isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        counters["bad_date"] += 1
        return None
    if not (START_YEAR <= int(d[:4]) <= 2026):
        counters["outside_time_window"] += 1
        return None
    return Match(key=f"{tid}:{m['id']}", date=d, tourney=tname,
                 round=str(m.get("round_name") or ""),
                 p1_id=str(p1["id"]), p1_name=str(p1.get("name_display") or f"Player {p1['id']}").strip(),
                 p2_id=str(p2["id"]), p2_name=str(p2.get("name_display") or f"Player {p2['id']}").strip(),
                 winner=winner, score=" ".join(f"{x}-{y}" for x, y in zip(a,b)))


def load_matches(data_dir):
    idx = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
    tournament_by_id = {str(t["id"]): t for t in idx if isinstance(t, dict) and t.get("id") is not None}
    filenames = sorted((data_dir / "matches").glob("*.json.gz"))
    if not filenames:
        raise ValueError("No BWF match archives available")
    counters = Counter()
    results = {}
    for filename in filenames:
        with gzip.open(filename, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        tid = filename.name.split(".")[0]
        name = (tournament_by_id.get(tid) or {}).get("name") or f"BWF tournament {tid}"
        for m in _matches(raw):
            entry = _make_match(tid, name, m, counters)
            if entry is not None:
                results[entry.key] = entry
    matches = sorted(results.values(), key=lambda m: (m.date, m.key))
    counters["archives_read"] = len(filenames)
    counters["clean_MS_matches"] = len(matches)
    return matches, dict(counters)


def elo_p(a, b):
    return 1.0 / (1.0 + 10.0 ** ((b-a) / SCALE))


def simulate(matches, k):
    """Predict ALL matches in a day from previous-day Elo before updates.

    Returns (2024 predictions, 2025 predictions, latest ratings/name maps,
    per-year match counts). Model knows results of all past dates only.
    """
    rating = defaultdict(lambda: INITIAL_ELO)
    names = {}
    seen = Counter()
    stats = Counter()
    p24, p25 = [], []
    pos = 0
    while pos < len(matches):
        start = pos
        today = matches[pos].date
        while pos < len(matches) and matches[pos].date == today:
            pos += 1
        batch = matches[start:pos]
        stored = []
        for m in batch:
            r1, r2 = rating[m.p1_id], rating[m.p2_id]
            probability = elo_p(r1, r2)
            cold = int(seen[m.p1_id] == 0 or seen[m.p2_id] == 0)
            record = {
                "match_id": m.key, "date": m.date,
                "tournament": m.tourney, "round": m.round,
                "player_a_id": m.p1_id, "player_a": m.p1_name,
                "player_b_id": m.p2_id, "player_b": m.p2_name,
                "rating_a_pre": round(r1, 4), "rating_b_pre": round(r2, 4),
                "p_a": round(probability, 8),
                "winner": m.winner, "score": m.score, "cold_start": cold,
            }
            year = int(m.date[:4])
            stats[str(year)] += 1
            if year == VALIDATION_YEAR:
                p24.append(record)
            elif year == TEST_YEAR:
                p25.append(record)
            stored.append((m, probability))
        # Apply changes after all forecasts from this date have been logged.
        # If a player plays twice on the same day, calculate total change
        # from the common morning snapshot (still no same-day leakage).
        delta = defaultdict(float)
        for m, p in stored:
            y = 1 if m.winner == 1 else 0
            shift = k * (y - p)
            delta[m.p1_id] += shift
            delta[m.p2_id] -= shift
            seen[m.p1_id] += 1
            seen[m.p2_id] += 1
            names[m.p1_id] = m.p1_name
            names[m.p2_id] = m.p2_name
        for pid, change in delta.items():
            rating[pid] += change
    return p24, p25, rating, names, seen, dict(stats)


def metrics(pred):
    n = len(pred)
    if n == 0:
        return None
    brier = sum((x["p_a"] - (x["winner"] == 1)) ** 2 for x in pred) / n
    logloss = sum(-(1 if x["winner"] == 1 else 0) * math.log(max(1e-10, x["p_a"]))
                  - (1 if x["winner"] == 2 else 0) * math.log(max(1e-10, 1-x["p_a"])) for x in pred) / n
    correct = sum((x["p_a"] >= 0.5) == (x["winner"] == 1) for x in pred)
    groups = []
    for lower in range(0, 10):
        selected = [x for x in pred if lower/10 <= x["p_a"] < (lower+1)/10 or (lower == 9 and x["p_a"] == 1)]
        if selected:
            groups.append({"bucket": f"{lower*10}-{(lower+1)*10}%",
                           "n": len(selected),
                           "avg_p": round(sum(x["p_a"] for x in selected)/len(selected), 4),
                           "win_rate": round(sum(x["winner"] == 1 for x in selected)/len(selected), 4)})
    return {"n": n, "brier": round(brier, 6), "log_loss": round(logloss, 6),
            "accuracy": round(correct/n, 6),
            "cold_start_games": sum(x["cold_start"] for x in pred),
            "calibration": groups}


def save_csv(dest, rows, fields):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run(data_dir, output_dir):
    games, quality = load_matches(data_dir)
    years = Counter(int(m.date[:4]) for m in games)
    for year in range(2018, 2026):
        if years[year] < 100:
            raise ValueError(f"Too few MS games in {year}: {years[year]} (archive issue?)")
    trial = []
    for k in K_CANDIDATES:
        p24, _, _, _, _, _ = simulate(games, k)
        report = metrics(p24)
        trial.append((report["brier"], report["log_loss"], k, report))
    best = min(trial, key=lambda x: (x[0], x[1], x[2]))
    k = best[2]
    valid, test, ratings, names, seen, per_year = simulate(games, k)
    test_metric = metrics(test)
    valid_metric = metrics(valid)
    if not test_metric or test_metric["n"] < 100:
        raise ValueError("No meaningful 2025 holdout sample available")
    report = {
        "model": MODEL_NAME,
        "source": "BWF match cache collected with bwf_sync.py",
        "training_years": "2018-2023", "validation_year": 2024,
        "holdout_test_year": 2025, "latest_rating_updates": "2026 archive included; not T0 predictions",
        "discipline": "MS", "utc_date_grouping": True,
        "initial_elo": INITIAL_ELO, "elo_scale": SCALE,
        "chosen_k": k, "k_selection": "min 2024 Brier, tiebreak 2024 log loss",
        "trial_validation": [{"k": t[2], "brier": t[3]["brier"],
                              "log_loss": t[3]["log_loss"], "n": t[3]["n"]} for t in trial],
        "validation_2024": valid_metric,
        "test_2025": test_metric,
        "baselines": {"constant_0_5_brier": 0.25,
                      "constant_0_5_log_loss": round(math.log(2),6)},
        "match_counts_by_year": per_year,
        "data_quality": quality,
        "notes": [
            "Test 2025 is untouched by hyperparameter selection; outcomes are used only after forecasts.",
            "All matches on same UTC date are predicted from prior-date state; match order never leaks same-date outcomes.",
            "No bookmaker odds, ROI or true live 2026 T0 predictions are part of this backtest.",
            "Match source has no independent proof of completeness; cancellations omitted.",
            "This baseline is not yet probability-calibrated."
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "elo_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fields = ["match_id", "date", "tournament", "round", "player_a_id", "player_a", "player_b_id", "player_b", "rating_a_pre", "rating_b_pre", "p_a", "winner", "score", "cold_start"]
    save_csv(output_dir / "holdout_2025.csv", test, fields)
    ranking = [{"player_id": p, "name": names[p], "elo": round(r, 2), "n_played_2018_on": seen[p]}
               for p, r in sorted(ratings.items(), key=lambda item: (-item[1], item[0]))]
    save_csv(output_dir / "ratings_latest.csv", ranking, ["player_id", "name", "elo", "n_played_2018_on"])
    print(json.dumps({"model": MODEL_NAME, "clean_MS":len(games),
                      "k":k,"validation":valid_metric and {z:valid_metric[z] for z in ("n","brier","accuracy")},
                      "test":{z:test_metric[z] for z in ("n","brier","log_loss","accuracy")},
                      "ratings":len(ranking)}, ensure_ascii=False))


def self_test():
    assert abs(elo_p(1500,1500)-0.5) < 1e-12
    assert abs(elo_p(1700,1500) + elo_p(1500,1700) - 1) < 1e-12
    fake = lambda key,day,a,b,w: Match(key,day,"T","R16",a,a,b,b,w,"21-18 21-18")
    sample = [fake("1","2023-01-01","A","B",1),
              fake("2","2023-01-02","A","B",1),
              fake("3","2024-06-01","A","B",1),
              fake("4","2024-06-01","A","B",2),
              fake("5","2025-06-01","A","B",1)]
    v, t, ratings, _, _, by_year = simulate(sample, 32)
    assert len(v) == 2 and len(t) == 1 and by_year["2024"] == 2
    assert v[0]["p_a"] == v[1]["p_a"], "same-date leak"
    assert v[0]["p_a"] > 0.5, "prior victories must raise Elo"
    assert metrics(v)["n"] == 2 and len(ratings) == 2
    # Realistic BWF object shape, including a duplicated by_court mirror.
    example = {"id":42,"draw_name":"MS", "winner":1, "start_time":1650000000,
      "t1p1_detail":{"id":111,"name_display":"TEST A"},
      "t2p1_detail":{"id":222,"name_display":"TEST B"},
      "team1Score":"<span>21</span><span>21</span>",
      "team2Score":"<span>19</span><span>16</span>"}
    d={"results":{"by_time":{"time_group":[example]},"by_court":{"A":{"1":example}}}}
    counts=Counter()
    assert len(_matches(d)) == 1
    m=_make_match("888","Fixture",_matches(d)[0],counts)
    assert m is not None and m.winner==1 and m.p1_id=="111"
    bad=dict(example);bad["t2p1_detail"]={"id":111};
    assert _make_match("888","Fixture",bad,counts) is None
    retired=dict(example); retired["team1Score"]="<span>21</span><span>3</span>"
    retired["team2Score"]="<span>19</span><span>2</span>"
    assert _make_match("888","Fixture",retired,counts) is None
    with tempfile.TemporaryDirectory() as td:
        p=Path(td);(p/"matches").mkdir()
        (p/"index.json").write_text(json.dumps([{"id":888,"name":"Fixture"}]),encoding="utf-8")
        with gzip.open(p/"matches"/"888.json.gz", "wt", encoding="utf-8") as f:json.dump(d,f)
        loaded,_=load_matches(p)
        assert len(loaded)==1 and loaded[0].key=="888:42"
    print("SELF TEST PASS: parser, duplicates, scores, player IDs, Elo symmetry, same-day no-leak, Brier")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--data-dir",type=Path,default=Path("data/bwf"))
    parser.add_argument("--output-dir",type=Path,default=Path("data/model"))
    options=parser.parse_args()
    if options.self_test:
        self_test()
    else:
        run(options.data_dir,options.output_dir)


if __name__ == "__main__":
    main()
