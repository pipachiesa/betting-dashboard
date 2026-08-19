#!/usr/bin/env python3
"""Walk-forward backtest of the team markets.

Nothing is fitted on data that would not have existed at kick-off: the model is
refitted every block of days on strictly prior matches, then scored on the
matches in that block.  Two baselines are scored alongside it -- the league
average, and the team's own trailing average -- because a model that cannot
beat a trailing average is not a model.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_model import DEFAULT_K, SHRINK_K, TEAM_MARKETS, fit_market, load

LINES = {
    "shots": [8.5, 10.5, 12.5, 14.5],
    "sot": [2.5, 3.5, 4.5, 5.5],
    "corners": [3.5, 4.5, 5.5, 6.5],
    "goals": [0.5, 1.5, 2.5],
    "cards": [1.5, 2.5, 3.5],
    "fouls": [9.5, 11.5, 13.5],
    "tackles": [11.5, 14.5, 17.5],
}


def nb_tail(lam: float, line: float, r: float | None) -> float:
    """P(X > line) for a negative binomial with mean lam, or Poisson if r is None."""
    lam = max(1e-9, lam)
    threshold = int(math.floor(line))
    if r is None or r <= 0:
        total, term = 0.0, math.exp(-lam)
        for k in range(threshold + 1):
            if k:
                term *= lam / k
            total += term
        return min(1.0, max(0.0, 1.0 - total))
    p = r / (r + lam)
    total, term = 0.0, p ** r          # P(X=0)
    for k in range(threshold + 1):
        if k:
            term *= (r + k - 1) / k * (1 - p)
        total += term
    return min(1.0, max(0.0, 1.0 - total))


def score(predictions: list[tuple[float, int]]) -> dict:
    if not predictions:
        return {}
    probabilities = np.array([p for p, _ in predictions])
    outcomes = np.array([o for _, o in predictions], dtype=float)
    clipped = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return {
        "n": len(predictions),
        "brier": round(float(np.mean((probabilities - outcomes) ** 2)), 5),
        "logloss": round(float(-np.mean(outcomes * np.log(clipped) + (1 - outcomes) * np.log(1 - clipped))), 5),
        "base_rate": round(float(outcomes.mean()), 4),
    }


def calibration(predictions: list[tuple[float, int]], bins: int = 10) -> list[dict]:
    if not predictions:
        return []
    probabilities = np.array([p for p, _ in predictions])
    outcomes = np.array([o for _, o in predictions], dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for i in range(bins):
        mask = (probabilities >= edges[i]) & (probabilities < edges[i + 1] if i < bins - 1 else probabilities <= 1)
        if mask.sum() < 10:
            continue
        rows.append({
            "bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}",
            "n": int(mask.sum()),
            "pred": round(float(probabilities[mask].mean()), 4),
            "obs": round(float(outcomes[mask].mean()), 4),
        })
    return rows


def run_league(frame: pd.DataFrame, block_days: int, min_train: int) -> dict:
    frame = frame.sort_values("date").reset_index(drop=True)
    dates = frame["date"]
    start = dates.iloc[min_train] if len(frame) > min_train else None
    if start is None:
        return {}

    results = {m: defaultdict(list) for m in TEAM_MARKETS}
    cursor = start
    end = dates.max()
    blocks = 0
    while cursor <= end:
        block_end = cursor + pd.Timedelta(days=block_days)
        train = frame[frame["date"] < cursor]
        test = frame[(frame["date"] >= cursor) & (frame["date"] < block_end)]
        cursor = block_end
        if len(train) < min_train or test.empty:
            continue
        blocks += 1
        # trailing average per team, as the baseline to beat
        trailing = train.groupby("team_id")

        for market, (produced, conceded) in TEAM_MARKETS.items():
            if market not in LINES or produced not in frame.columns:
                continue
            fitted = fit_market(train, produced, conceded, SHRINK_K.get(market, DEFAULT_K))
            if not fitted:
                continue
            mu, home, dispersion = fitted["mu"], fitted["home"], fitted["r"]
            factors = fitted["teams"]
            means = trailing[produced].mean().to_dict()
            for row in test.itertuples():
                observed = getattr(row, produced)
                if pd.isna(observed):
                    continue
                attack = factors.get(int(row.team_id), [1.0, 1.0, 0])[0]
                defence = factors.get(int(row.opp_id), [1.0, 1.0, 0])[1]
                venue = home if row.is_home else 1.0 / home
                lam_model = mu * attack * defence * venue
                lam_team = means.get(int(row.team_id), mu)
                for line in LINES[market]:
                    outcome = int(observed > line)
                    results[market]["model"].append((nb_tail(lam_model, line, dispersion), outcome))
                    results[market]["league"].append((nb_tail(mu, line, dispersion), outcome))
                    results[market]["trailing"].append((nb_tail(lam_team, line, dispersion), outcome))
    return {
        "blocks": blocks,
        "markets": {
            market: {
                variant: score(predictions) for variant, predictions in variants.items()
            } | {"calibration": calibration(variants["model"])}
            for market, variants in results.items() if variants["model"]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--block-days", type=int, default=14)
    parser.add_argument("--min-train", type=int, default=300)
    parser.add_argument("--leagues", default="")
    parser.add_argument("--out", default="/tmp/backtest.json")
    args = parser.parse_args()

    teams = load("teams")
    wanted = [k.strip() for k in args.leagues.split(",") if k.strip()] or None
    report = {}
    for league, frame in teams.groupby("league"):
        if wanted and league not in wanted:
            continue
        result = run_league(frame, args.block_days, args.min_train)
        if not result:
            print(f"{league}: datos insuficientes")
            continue
        report[league] = result
        print(f"\n=== {league} ({result['blocks']} bloques) ===")
        print("%-9s %6s  %-22s %-22s %-22s" % ("mercado", "n", "modelo", "prom. del equipo", "media de liga"))
        for market, variants in result["markets"].items():
            model, trailing, league_base = variants["model"], variants["trailing"], variants["league"]
            gain = (league_base["brier"] - model["brier"]) / league_base["brier"] * 100
            print("%-9s %6d  brier %.4f ll %.4f  brier %.4f ll %.4f  brier %.4f ll %.4f  (%+.1f%% vs liga)" % (
                market, model["n"], model["brier"], model["logloss"],
                trailing["brier"], trailing["logloss"],
                league_base["brier"], league_base["logloss"], gain))
    pathlib.Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
