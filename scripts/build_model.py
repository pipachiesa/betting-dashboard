#!/usr/bin/env python3
"""Fit the predictive layer from history/ and emit model.json.

Team counts are modelled with the classic multiplicative rate structure

    lambda(A vs B) = mu_league * attack_A * defence_B * home^(+/-1)

where attack and defence are solved by iterative proportional fitting, so a
team that racks up shots against weak defences is not credited for it.  Both
factors are shrunk towards 1 by k pseudo-matches, which is what keeps a team
with four matches played from dominating the table.

Counts are overdispersed relative to Poisson, so each market also carries a
negative-binomial dispersion fitted by method of moments on the residuals.

Player props are a share of the team total: the share is estimated per 90,
shrunk towards a positional prior, and multiplied by expected minutes.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
HISTORY = ROOT / "history"
OUTPUT = ROOT / "model.json"

# market -> (produced column, conceded column)
TEAM_MARKETS = {
    "shots": ("shots", "shots_ag"),
    "sot": ("sot", "sot_ag"),
    "corners": ("corners", "corners_ag"),
    "goals": ("gf", "ga"),
    "fouls": ("fouls", "fouls_ag"),
    "cards": ("yellow", "yellow_ag"),
    "tackles": ("tackles", "tackles_ag"),
    "xg": ("xg", "xg_ag"),
    "saves": ("saves", "saves_ag"),
}

# player market -> (player column, team column the share is taken from)
PLAYER_MARKETS = {
    "shots": ("shots", "shots"),
    "sot": ("sot", "sot"),
    # a keeper's saves come from the shots the opponent puts on target
    "saves": ("saves", "sot_ag"),
    "fouls": ("fouls", "fouls"),
    "fouled": ("fouled", "fouls_ag"),
    # a player's tackles are a share of his own team's tackles, not the rival's
    "tackles": ("tackles", "tackles"),
    "cards": ("yellow", "yellow"),
}

# Tuned by walk-forward sweep over four leagues and ~64k scored predictions.
# The earlier single-league tuning was overfit: on Liga Profesional alone goals
# wanted k=28, but with the full archive the optimum drops to 7.  Where the
# Brier-optimal k left calibration above 3 points, the next k that fixes
# calibration was taken instead -- a well-calibrated 70% matters more than a
# 0.0004 Brier gain.
SHRINK_K = {"shots": 7.0, "sot": 10.0, "corners": 20.0, "goals": 7.0,
            "fouls": 10.0, "cards": 10.0, "tackles": 10.0, "xg": 10.0,
            "saves": 10.0}
DEFAULT_K = 7.0
PLAYER_K = 4.0          # pseudo-matches of shrinkage on a player's share
HALF_LIFE = 12.0        # recency half-life, in matches
MIN_MINUTES = 15        # cameos make per-90 shares explode, so ignore them
MIN_POSITION_ROWS = 25  # below this a positional prior is noise, not a prior

# Markets only some positions can record.  Without this a defender who once
# went in goal contaminates the keeper prior.
ELIGIBLE = {"saves": lambda f: f["is_gk"] == 1}


def load(kind: str) -> pd.DataFrame:
    files = sorted((HISTORY / kind).glob("*.csv"))
    if not files:
        raise SystemExit(f"no hay datos en {HISTORY / kind}; corre backfill.py primero")
    frame = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return frame.dropna(subset=["date"]).sort_values("date")


def recency_weights(dates: pd.Series, latest: pd.Timestamp) -> np.ndarray:
    """Exponential decay in match-equivalents (a league match every ~7 days)."""
    age_matches = (latest - dates).dt.days.to_numpy() / 7.0
    return np.power(0.5, age_matches / HALF_LIFE)


def fit_market(frame: pd.DataFrame, produced: str, conceded: str, k: float,
               iterations: int = 12) -> dict:
    """Iterative proportional fitting of attack/defence factors."""
    data = frame[[produced, conceded, "team_id", "opp_id", "is_home", "date"]].dropna()
    if len(data) < 40:
        return {}
    latest = data["date"].max()
    weight = recency_weights(data["date"], latest)
    produced_values = data[produced].to_numpy(dtype=float)
    home = data["is_home"].to_numpy(dtype=float)

    total_weight = weight.sum()
    mu = float((produced_values * weight).sum() / total_weight)
    if mu <= 0:
        return {}
    # Multiplicative home factor: home mean = mu*h, away mean = mu/h.
    home_mean = float((produced_values * weight * home).sum() / max(1e-9, (weight * home).sum()))
    away_mean = float((produced_values * weight * (1 - home)).sum() / max(1e-9, (weight * (1 - home)).sum()))
    home_factor = math.sqrt(max(1e-6, home_mean) / max(1e-6, away_mean))
    mu = math.sqrt(max(1e-6, home_mean) * max(1e-6, away_mean))

    teams = pd.unique(pd.concat([data["team_id"], data["opp_id"]]))
    index = {team: i for i, team in enumerate(teams)}
    team_idx = data["team_id"].map(index).to_numpy()
    opp_idx = data["opp_id"].map(index).to_numpy()
    venue = np.where(home > 0, home_factor, 1.0 / home_factor)

    attack = np.ones(len(teams))
    defence = np.ones(len(teams))
    for _ in range(iterations):
        for factors, own, other, values, venue_term in (
            (attack, team_idx, opp_idx, produced_values, venue),
            (defence, opp_idx, team_idx, produced_values, venue),
        ):
            numerator = np.bincount(own, weights=weight * values, minlength=len(teams))
            partner = defence if factors is attack else attack
            expected = mu * partner[other] * venue_term
            denominator = np.bincount(own, weights=weight * expected, minlength=len(teams))
            with np.errstate(divide="ignore", invalid="ignore"):
                raw = np.where(denominator > 0, numerator / denominator, 1.0)
            counts = np.bincount(own, weights=weight, minlength=len(teams))
            # shrink towards 1 by k pseudo-matches
            factors[:] = (raw * counts + k) / (counts + k)
        # keep the parameterisation identified
        attack /= attack.mean()
        defence /= defence.mean()

    expected = mu * attack[team_idx] * defence[opp_idx] * venue
    residual_var = float((weight * (produced_values - expected) ** 2).sum() / total_weight)
    mean_expected = float((weight * expected).sum() / total_weight)
    # Var = mu + mu^2/r  ->  r = mu^2 / (Var - mu); r = None means Poisson is fine
    excess = residual_var - mean_expected
    dispersion = (mean_expected ** 2 / excess) if excess > 0.05 else None

    counts = np.bincount(team_idx, minlength=len(teams))
    return {
        "mu": round(mu, 4),
        "home": round(home_factor, 4),
        "r": round(dispersion, 3) if dispersion and dispersion < 500 else None,
        "teams": {
            int(team): [round(float(attack[i]), 4), round(float(defence[i]), 4), int(counts[i])]
            for team, i in index.items()
        },
    }


def player_shares(players: pd.DataFrame, teams: pd.DataFrame) -> dict:
    """Per-90 share of the team total, shrunk towards a positional prior."""
    team_cols = sorted({column for _, column in PLAYER_MARKETS.values()})
    reference = teams[["match_id", "team_id", *team_cols]].copy()
    merged = players.merge(reference, on=["match_id", "team_id"], how="left",
                           suffixes=("", "_team"))
    merged = merged[merged["minutes"] >= MIN_MINUTES].copy()
    if merged.empty:
        return {}
    latest = merged["date"].max()
    merged["w"] = recency_weights(merged["date"], latest)
    merged["p90"] = 90.0 / merged["minutes"].clip(lower=1)

    # expected minutes needs the matches a player sat out, so count every match
    # his team played inside the player's own active span
    squad_matches = (
        teams.groupby("team_id")["match_id"].nunique().rename("team_matches")
    )

    out: dict[str, dict] = {}
    priors: dict[str, dict[str, float]] = {}
    for market, (player_col, team_col) in PLAYER_MARKETS.items():
        team_reference = f"{team_col}_team" if f"{team_col}_team" in merged.columns else team_col
        pool = merged[ELIGIBLE[market](merged)] if market in ELIGIBLE else merged
        valid = pool[[player_col, team_reference, "w", "p90", "position", "player_id"]].dropna()
        valid = valid[valid[team_reference] > 0]
        if valid.empty:
            continue
        # share of the team total that this player produced, scaled to 90 minutes
        share = (valid[player_col] / valid[team_reference]) * valid["p90"]
        valid = valid.assign(share=share.clip(upper=1.5))
        overall = float(np.average(valid["share"], weights=valid["w"]))
        priors[market] = {
            position: float(np.average(group["share"], weights=group["w"]))
            for position, group in valid.groupby("position")
            if len(group) >= MIN_POSITION_ROWS
        }
        priors[market]["_"] = overall
        grouped = valid.groupby("player_id")
        for player_id, group in grouped:
            prior = priors[market].get(group["position"].iloc[0], priors[market]["_"])
            weight_sum = float(group["w"].sum())
            observed = float(np.average(group["share"], weights=group["w"]))
            shrunk = (observed * weight_sum + prior * PLAYER_K) / (weight_sum + PLAYER_K)
            out.setdefault(str(int(player_id)), {})[market] = round(shrunk, 5)
    return {"shares": out, "priors": priors}


ACTIVE_DAYS = 150       # a player who has not appeared since then is not bettable


def player_profiles(players: pd.DataFrame) -> dict:
    latest = players["date"].max()
    players = players.copy()
    players["w"] = recency_weights(players["date"], latest)
    # the browser only ever needs players who might actually start on Saturday;
    # keeping five seasons of departed players triples the payload
    cutoff = latest - pd.Timedelta(days=ACTIVE_DAYS)
    profiles = {}
    for player_id, group in players.groupby("player_id"):
        if group["date"].max() < cutoff:
            continue
        recent = group.sort_values("date").tail(10)
        weight = recent["w"].to_numpy()
        minutes = float(np.average(recent["minutes"], weights=weight)) if len(recent) else 0.0
        started = float(np.average(recent["started"].fillna(0), weights=weight)) if len(recent) else 0.0
        last = group.iloc[-1]
        profiles[str(int(player_id))] = {
            "name": last["player"],
            "team": int(last["team_id"]),
            "pos": last["position"] if isinstance(last["position"], str) else None,
            "gk": int(last["is_gk"]),
            "min": round(minutes, 1),
            "start": round(started, 3),
            "n": int(len(group)),
        }
    return profiles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--half-life", type=float, default=HALF_LIFE)
    parser.add_argument("--out", default=str(OUTPUT))
    args = parser.parse_args()
    globals()["HALF_LIFE"] = args.half_life

    teams = load("teams")
    players = load("players")
    print(f"team-partidos: {len(teams)}  jugador-partidos: {len(players)}")

    model: dict = {"leagues": {}, "players": {}, "teamNames": {}}
    for league, frame in teams.groupby("league"):
        markets = {}
        for market, (produced, conceded) in TEAM_MARKETS.items():
            if produced not in frame.columns:
                continue
            fitted = fit_market(frame, produced, conceded, SHRINK_K.get(market, DEFAULT_K))
            if fitted:
                markets[market] = fitted
        model["leagues"][league] = {
            "markets": markets,
            "matches": int(frame["match_id"].nunique()),
            "from": str(frame["date"].min().date()),
            "to": str(frame["date"].max().date()),
        }
        names = frame.sort_values("date").groupby("team_id")["team"].last()
        model["teamNames"].update({str(int(k)): v for k, v in names.items()})
        print(f"  {league}: {frame['match_id'].nunique()} partidos, "
              f"{len(markets)} mercados, {frame['team_id'].nunique()} equipos")

    shares = player_shares(players, teams)
    profiles = player_profiles(players)
    model["players"] = {
        "profiles": profiles,
        # shares for departed players are dead weight in the payload
        "shares": {k: v for k, v in shares.get("shares", {}).items() if k in profiles},
        "priors": shares.get("priors", {}),
    }
    model["generatedAt"] = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    model["halfLife"] = args.half_life

    path = pathlib.Path(args.out)
    path.write_text(json.dumps(model, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"\n{path.name}: {path.stat().st_size/1e6:.2f} MB, "
          f"{len(model['players']['profiles'])} jugadores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
