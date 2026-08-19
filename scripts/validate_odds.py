#!/usr/bin/env python3
"""Score the model against real closing odds -- the only benchmark that counts.

Beating a trailing average proves the model is not noise.  It does not prove
there is anything to bet on.  The market price already contains team news,
injuries and motivation, none of which this model sees, so the honest question
is not whether the model wins but how far behind it lands.

Odds come from football-data.co.uk, which publishes closing prices per match.
Only totals over/under 2.5 goals are available for free, which is awkward: it
is the market where the model is weakest.  Shots and corners, where it is
strongest, have no free closing prices anywhere.
"""

from __future__ import annotations

import argparse
import glob
import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backtest import score, calibration
from build_model import DEFAULT_K, SHRINK_K, fit_market, load

LEAGUE_CODE = {"E0": "eng", "SP1": "esp", "I1": "ita", "F1": "fra"}


def poisson_over(lam: float, line: float) -> float:
    """P(total > line) for the sum of two independent Poisson scoring rates."""
    threshold = int(math.floor(line))
    total, term = 0.0, math.exp(-lam)
    for k in range(threshold + 1):
        if k:
            term *= lam / k
        total += term
    return min(1.0, max(0.0, 1.0 - total))


def devig(over: float, under: float) -> float | None:
    """Strip the bookmaker margin proportionally."""
    if not over or not under or over <= 1 or under <= 1:
        return None
    a, b = 1.0 / over, 1.0 / under
    return a / (a + b)


def load_odds(folder: str) -> pd.DataFrame:
    frames = []
    for path in glob.glob(f"{folder}/*.csv"):
        code = pathlib.Path(path).name.split("_")[0]
        if code not in LEAGUE_CODE:
            continue
        frame = pd.read_csv(path, encoding="latin-1")
        frame["league"] = LEAGUE_CODE[code]
        frames.append(frame)
    odds = pd.concat(frames, ignore_index=True)
    odds["date"] = pd.to_datetime(odds["Date"], dayfirst=True, errors="coerce")
    return odds.dropna(subset=["date", "FTHG", "FTAG"])


def walk_forward(frame: pd.DataFrame, block_days: int, min_train: int) -> pd.DataFrame:
    """Predicted home/away scoring rates for every match, fitted on prior data."""
    frame = frame.sort_values("date").reset_index(drop=True)
    rows = []
    cursor = frame["date"].iloc[min_train]
    end = frame["date"].max()
    while cursor <= end:
        block_end = cursor + pd.Timedelta(days=block_days)
        train = frame[frame["date"] < cursor]
        test = frame[(frame["date"] >= cursor) & (frame["date"] < block_end)]
        cursor = block_end
        if len(train) < min_train or test.empty:
            continue
        fitted = fit_market(train, "gf", "ga", SHRINK_K.get("goals", DEFAULT_K))
        if not fitted:
            continue
        mu, home, factors = fitted["mu"], fitted["home"], fitted["teams"]
        for row in test.itertuples():
            if row.is_home != 1:
                continue
            attack_h = factors.get(int(row.team_id), [1.0, 1.0, 0])
            attack_a = factors.get(int(row.opp_id), [1.0, 1.0, 0])
            rows.append({
                "match_id": row.match_id,
                "lam_home": mu * attack_h[0] * attack_a[1] * home,
                "lam_away": mu * attack_a[0] * attack_h[1] / home,
            })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--odds-dir", default="/tmp/fd")
    parser.add_argument("--line", type=float, default=2.5)
    parser.add_argument("--block-days", type=int, default=14)
    parser.add_argument("--min-train", type=int, default=400)
    args = parser.parse_args()

    history = load("teams")
    history = history[history["league"].isin(LEAGUE_CODE.values())]
    odds = load_odds(args.odds_dir)

    predictions = []
    for league, frame in history.groupby("league"):
        predictions.append(walk_forward(frame, args.block_days, args.min_train))
    predictions = pd.concat(predictions, ignore_index=True)

    home = history[history["is_home"] == 1]
    merged = home.merge(predictions, on="match_id")
    # join to odds on league + exact score + date within a day
    merged["key"] = (merged["league"] + "_" + merged["gf"].astype(int).astype(str)
                     + "_" + merged["ga"].astype(int).astype(str))
    odds["key"] = (odds["league"] + "_" + odds["FTHG"].astype(int).astype(str)
                   + "_" + odds["FTAG"].astype(int).astype(str))
    joined = merged.merge(odds, on="key", suffixes=("", "_o"))
    joined = joined[(joined["date_o"] - joined["date"]).dt.days.abs() <= 1]
    joined = joined.drop_duplicates("match_id")

    over_col, under_col = f"Avg>{args.line}", f"Avg<{args.line}"
    joined["p_market"] = [devig(o, u) for o, u in zip(joined[over_col], joined[under_col])]
    joined = joined.dropna(subset=["p_market"])
    joined["total"] = joined["FTHG"] + joined["FTAG"]
    joined["outcome"] = (joined["total"] > args.line).astype(int)
    joined["p_model"] = [poisson_over(h + a, args.line)
                         for h, a in zip(joined["lam_home"], joined["lam_away"])]

    print(f"partidos evaluados: {len(joined)}  (over {args.line} ocurrio el "
          f"{joined['outcome'].mean()*100:.1f}% de las veces)\n")
    model = score(list(zip(joined["p_model"], joined["outcome"])))
    market = score(list(zip(joined["p_market"], joined["outcome"])))
    blend = score(list(zip((joined["p_model"] + joined["p_market"]) / 2, joined["outcome"])))
    print("%-22s %9s %9s" % ("", "brier", "logloss"))
    for name, result in (("modelo", model), ("mercado (sin margen)", market),
                         ("promedio de los dos", blend)):
        print("%-22s %9.5f %9.5f" % (name, result["brier"], result["logloss"]))
    gap = (model["brier"] - market["brier"]) / market["brier"] * 100
    print(f"\nel modelo esta {gap:+.1f}% respecto del mercado en Brier")

    print("\ncalibracion del modelo:")
    for b in calibration(list(zip(joined["p_model"], joined["outcome"]))):
        print("  %-9s n=%4d pred=%.3f obs=%.3f  %+.3f" % (b["bin"], b["n"], b["pred"], b["obs"], b["obs"] - b["pred"]))

    # does disagreeing with the market pay?  bet the model's side when the gap
    # is wide enough, price at the offered odds, and count the money
    print("\napostando cuando el modelo discrepa del mercado (cuota real, stake 1):")
    print("%8s %7s %9s %9s" % ("umbral", "apuestas", "aciertos", "ROI"))
    for edge in (0.03, 0.05, 0.08, 0.12):
        # itertuples mangles column names like "Avg>2.5", so stay vectorised
        diff = joined["p_model"] - joined["p_market"]
        picked = joined[diff.abs() >= edge]
        if picked.empty:
            continue
        back_over = (picked["p_model"] - picked["p_market"]) > 0
        price = np.where(back_over, picked[over_col], picked[under_col])
        won = np.where(back_over, picked["outcome"] == 1, picked["outcome"] == 0)
        profit = np.where(won, price - 1.0, -1.0).sum()
        print("%8.2f %7d %8.1f%% %8.1f%%" % (edge, len(picked), won.mean() * 100,
                                             profit / len(picked) * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
