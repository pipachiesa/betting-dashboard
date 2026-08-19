#!/usr/bin/env python3
"""Walk-forward backtest of the player prop markets.

The baseline that matters here is not the league average -- it is the player's
own recent form, because that is exactly what the bar chart on the dashboard
already shows.  If the model cannot beat "look at his last ten games", the
predictive layer earns nothing.

Everything is computed from strictly prior appearances: the share of the team
total, the expected minutes, and the positional prior are all shifted by one
match, so no row ever sees itself.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backtest import calibration, nb_tail, score
from build_model import DEFAULT_K, PLAYER_K, SHRINK_K, fit_market, load

# player market -> (player column, team column it is a share of, whose team)
MARKETS = {
    "shots": ("shots", "shots", "own", [0.5, 1.5, 2.5]),
    "sot": ("sot", "sot", "own", [0.5, 1.5]),
    "saves": ("saves", "sot_ag", "own", [1.5, 2.5, 3.5]),
    "fouls": ("fouls", "fouls", "own", [0.5, 1.5]),
    "fouled": ("fouled", "fouls_ag", "own", [0.5, 1.5, 2.5]),
}
WINDOW = 10
MIN_MINUTES = 15


def team_lambdas(teams: pd.DataFrame, market_col: str, conceded_col: str,
                 block_days: int, min_train: int) -> pd.DataFrame:
    """Walk-forward expected team totals, the denominator every prop rides on."""
    frame = teams.sort_values("date").reset_index(drop=True)
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
        fitted = fit_market(train, market_col, conceded_col,
                            SHRINK_K.get(market_col, DEFAULT_K))
        if not fitted:
            continue
        mu, home, factors = fitted["mu"], fitted["home"], fitted["teams"]
        for row in test.itertuples():
            attack = factors.get(int(row.team_id), [1.0, 1.0, 0])[0]
            defence = factors.get(int(row.opp_id), [1.0, 1.0, 0])[1]
            venue = home if row.is_home else 1.0 / home
            rows.append({"match_id": row.match_id, "team_id": row.team_id,
                         "lam_team": mu * attack * defence * venue})
    return pd.DataFrame(rows)


def run_market(players: pd.DataFrame, teams: pd.DataFrame, market: str,
               block_days: int, min_train: int) -> dict:
    player_col, team_col, _, lines = MARKETS[market]
    conceded = team_col + "_ag" if not team_col.endswith("_ag") else team_col[:-3]
    if team_col not in teams.columns or conceded not in teams.columns:
        return {}

    lam = team_lambdas(teams, team_col, conceded, block_days, min_train)
    if lam.empty:
        return {}

    frame = players[players["minutes"] >= MIN_MINUTES].copy()
    if market == "saves":
        frame = frame[frame["is_gk"] == 1]
    else:
        frame = frame[frame["is_gk"] == 0]
    reference = teams[["match_id", "team_id", team_col]].rename(columns={team_col: "team_total"})
    frame = frame.merge(reference, on=["match_id", "team_id"], how="left")
    frame = frame.merge(lam, on=["match_id", "team_id"], how="inner")
    frame = frame.dropna(subset=[player_col, "team_total"])
    frame = frame[frame["team_total"] > 0].sort_values(["player_id", "date"])
    if len(frame) < 500:
        return {}

    # share of the team total, scaled to 90 minutes, from PAST matches only
    frame["share_obs"] = (frame[player_col] / frame["team_total"]) * (90.0 / frame["minutes"].clip(lower=1))
    grouped = frame.groupby("player_id")
    frame["share_prior"] = grouped["share_obs"].transform(
        lambda s: s.shift(1).rolling(WINDOW, min_periods=2).mean())
    frame["n_prior"] = grouped["share_obs"].transform(
        lambda s: s.shift(1).rolling(WINDOW, min_periods=2).count())
    frame["min_prior"] = grouped["minutes"].transform(
        lambda s: s.shift(1).rolling(WINDOW, min_periods=2).mean())
    # the player's own trailing average -- what the dashboard bar chart shows
    frame["own_avg"] = grouped[player_col].transform(
        lambda s: s.shift(1).rolling(WINDOW, min_periods=2).mean())
    # positional prior from past matches only
    frame = frame.sort_values("date")
    frame["pos_prior"] = (
        frame.groupby("position")["share_obs"]
        .transform(lambda s: s.shift(1).expanding(min_periods=50).mean())
    )
    frame = frame.dropna(subset=["share_prior", "min_prior", "own_avg", "pos_prior"])
    if frame.empty:
        return {}

    shrunk = ((frame["share_prior"] * frame["n_prior"] + frame["pos_prior"] * PLAYER_K)
              / (frame["n_prior"] + PLAYER_K))
    frame["lam_model"] = frame["lam_team"] * shrunk * (frame["min_prior"] / 90.0)

    results = {"model": [], "own_avg": [], "flat": []}
    flat = float(frame[player_col].mean())
    for row in frame.itertuples():
        observed = getattr(row, player_col)
        for line in lines:
            outcome = int(observed > line)
            results["model"].append((nb_tail(row.lam_model, line, None), outcome))
            results["own_avg"].append((nb_tail(row.own_avg, line, None), outcome))
            results["flat"].append((nb_tail(flat, line, None), outcome))
    return {
        "scores": {k: score(v) for k, v in results.items()},
        "calibration": calibration(results["model"]),
        "players": int(frame["player_id"].nunique()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--block-days", type=int, default=14)
    parser.add_argument("--min-train", type=int, default=400)
    parser.add_argument("--leagues", default="")
    args = parser.parse_args()

    teams_all = load("teams")
    players_all = load("players")
    wanted = [k.strip() for k in args.leagues.split(",") if k.strip()]

    pooled: dict[str, dict[str, list]] = {}
    for league, teams in teams_all.groupby("league"):
        if wanted and league not in wanted:
            continue
        players = players_all[players_all["league"] == league]
        print(f"\n=== {league} ===")
        print("%-8s %7s %8s %9s %9s %9s" % ("mercado", "jug.", "n", "modelo",
                                            "prom. propio", "media global"))
        for market in MARKETS:
            result = run_market(players, teams, market, args.block_days, args.min_train)
            if not result:
                print("%-8s  sin datos suficientes" % market)
                continue
            s = result["scores"]
            gain = (s["own_avg"]["brier"] - s["model"]["brier"]) / s["own_avg"]["brier"] * 100
            print("%-8s %7d %8d %9.5f %9.5f %9.5f  (%+.1f%% vs prom. propio)" % (
                market, result["players"], s["model"]["n"], s["model"]["brier"],
                s["own_avg"]["brier"], s["flat"]["brier"], gain))
            bucket = pooled.setdefault(market, {"model": 0.0, "own": 0.0, "flat": 0.0, "n": 0, "win": 0, "tot": 0})
            bucket["model"] += s["model"]["brier"] * s["model"]["n"]
            bucket["own"] += s["own_avg"]["brier"] * s["model"]["n"]
            bucket["flat"] += s["flat"]["brier"] * s["model"]["n"]
            bucket["n"] += s["model"]["n"]
            bucket["tot"] += 1
            bucket["win"] += s["model"]["brier"] < s["own_avg"]["brier"]

    print("\n=== AGREGADO ===")
    print("%-8s %9s %13s %13s %9s" % ("mercado", "modelo", "prom. propio", "media global", "gana en"))
    for market, b in pooled.items():
        print("%-8s %9.5f %13.5f %13.5f %6d/%d  (%+.1f%%)" % (
            market, b["model"] / b["n"], b["own"] / b["n"], b["flat"] / b["n"],
            b["win"], b["tot"], (b["own"] - b["model"]) / b["own"] * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
