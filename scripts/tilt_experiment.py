#!/usr/bin/env python3
"""Does territorial control add anything on top of the attack/defence model?

True expected threat needs event-level data -- every pass and carry with start
and end coordinates -- which the source does not provide.  Field tilt is the
closest computable stand-in: the share of opposition-half passes a team takes,
which is a ratio and therefore not collinear with its own shot volume the way
xG and touches in the box are.

The adjustment is multiplicative and fitted, not assumed:

    lambda_adj = lambda_base * (tilt_own / tilt_opp) ** beta

beta is estimated by walk-forward search, so beta ~ 0 means tilt adds nothing.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_model import DEFAULT_K, SHRINK_K, TEAM_MARKETS, fit_market, load
from backtest import LINES, calibration, nb_tail, score


def add_tilt(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["team_id", "date"]).copy()
    total = (frame["passes_opp_half"] + frame["passes_opp_half_ag"]).clip(lower=1)
    frame["tilt"] = frame["passes_opp_half"] / total
    # trailing only: the tilt of the match being predicted is not knowable
    frame["own_tilt"] = frame.groupby("team_id")["tilt"].transform(
        lambda s: s.shift(1).rolling(10, min_periods=5).mean()
    )
    lookup = frame[["match_id", "team_id", "own_tilt"]].rename(
        columns={"team_id": "opp_id", "own_tilt": "opp_tilt"}
    )
    return frame.merge(lookup, on=["match_id", "opp_id"], how="left")


def run(frame: pd.DataFrame, market: str, betas: list[float],
        block_days: int = 14, min_train: int = 400) -> dict:
    produced, conceded = TEAM_MARKETS[market]
    frame = frame.sort_values("date").reset_index(drop=True)
    results = {beta: [] for beta in betas}
    cursor = frame["date"].iloc[min_train]
    end = frame["date"].max()
    while cursor <= end:
        block_end = cursor + pd.Timedelta(days=block_days)
        train = frame[frame["date"] < cursor]
        test = frame[(frame["date"] >= cursor) & (frame["date"] < block_end)]
        cursor = block_end
        if len(train) < min_train or test.empty:
            continue
        fitted = fit_market(train, produced, conceded, SHRINK_K.get(market, DEFAULT_K))
        if not fitted:
            continue
        mu, home, dispersion, factors = fitted["mu"], fitted["home"], fitted["r"], fitted["teams"]
        for row in test.itertuples():
            observed = getattr(row, produced)
            if pd.isna(observed) or pd.isna(row.own_tilt) or pd.isna(row.opp_tilt):
                continue
            attack = factors.get(int(row.team_id), [1.0, 1.0, 0])[0]
            defence = factors.get(int(row.opp_id), [1.0, 1.0, 0])[1]
            venue = home if row.is_home else 1.0 / home
            base = mu * attack * defence * venue
            ratio = max(0.2, min(5.0, row.own_tilt / max(1e-6, row.opp_tilt)))
            for beta in betas:
                lam = base * ratio ** beta
                for line in LINES[market]:
                    results[beta].append((nb_tail(lam, line, dispersion), int(observed > line)))
    return {beta: (score(rows) | {"cal": max((abs(b["obs"] - b["pred"]) for b in calibration(rows)), default=9)})
            for beta, rows in results.items() if rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="corners,shots,sot,goals")
    parser.add_argument("--betas", default="0,0.15,0.3,0.5,0.8")
    args = parser.parse_args()
    betas = [float(b) for b in args.betas.split(",")]

    teams = load("teams")
    teams = teams[teams["passes_opp_half"].notna()]
    for market in args.markets.split(","):
        print(f"\n=== {market} (beta=0 es el modelo actual) ===")
        print("%6s %8s %9s %8s %10s" % ("beta", "brier", "logloss", "cal err", "vs beta=0"))
        for league, frame in teams.groupby("league"):
            frame = add_tilt(frame)
            if frame["own_tilt"].notna().sum() < 500:
                continue
            out = run(frame, market, betas)
            if not out or 0.0 not in out:
                continue
            reference = out[0.0]["brier"]
            print(f"  [{league}] n={out[0.0]['n']}")
            for beta in betas:
                if beta not in out:
                    continue
                row = out[beta]
                gain = (reference - row["brier"]) / reference * 100
                print("%6.2f %8.5f %9.5f %8.4f %9.2f%%" % (
                    beta, row["brier"], row["logloss"], row["cal"], gain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
