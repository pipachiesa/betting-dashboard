#!/usr/bin/env python3
"""Turn a FotMob matchDetails payload into model-ready rows.

Two outputs per match:

* two team rows (one per side) in long format.  Every statistic is stored both
  as produced and as conceded, because an attack/defence rate model needs both
  halves of the equation and the payload already carries them.
* one player row per participant.

Shot coordinates are metres on a 105x68 pitch, always attacking towards x=105,
so shots bin into fixed pitch zones without any per-match normalisation.
"""

from __future__ import annotations

import re

PITCH_LENGTH = 105.0
PITCH_WIDTH = 68.0
GOAL_X, GOAL_Y = PITCH_LENGTH, PITCH_WIDTH / 2
BOX_X = PITCH_LENGTH - 16.5          # 88.5
BOX_Y_HALF = 20.16                   # penalty area half-width
SIX_X = PITCH_LENGTH - 5.5           # 99.5
SIX_Y_HALF = 9.16
CENTRAL_Y_HALF = 10.16               # central corridor, roughly the 6-yard width

ZONES = ("sixyard", "box_c", "box_w", "out_c", "out_w")
SITUATIONS = ("open", "fastbreak", "corner", "freekick", "penalty")

# team stat key -> output column name
TEAM_STATS = {
    "BallPossesion": "poss",
    "total_shots": "shots",
    "ShotsOnTarget": "sot",
    "ShotsOffTarget": "shots_off",
    "blocked_shots": "shots_blocked",
    "shots_woodwork": "woodwork",
    "shots_inside_box": "shots_box",
    "shots_outside_box": "shots_obox",
    "corners": "corners",
    "big_chance": "bigchance",
    "big_chance_missed_title": "bigchance_missed",
    "touches_opp_box": "touches_box",
    "fouls": "fouls",
    "yellow_cards": "yellow",
    "red_cards": "red",
    "Offsides": "offsides",
    "matchstats.headers.tackles": "tackles",
    "interceptions": "interceptions",
    "shot_blocks": "blocks",
    "clearances": "clearances",
    "keeper_saves": "saves",
    "duel_won": "duels_won",
    "ground_duels_won": "ground_duels_won",
    "aerials_won": "aerials_won",
    "dribbles_succeeded": "dribbles",
    "passes": "passes",
    "accurate_passes": "passes_acc",
    "own_half_passes": "passes_own_half",
    "opposition_half_passes": "passes_opp_half",
    "long_balls_accurate": "long_balls_acc",
    "accurate_crosses": "crosses_acc",
    "expected_goals": "xg",
    "expected_goals_open_play": "xg_op",
    "expected_goals_set_play": "xg_sp",
    "expected_goals_non_penalty": "xg_np",
    "expected_goals_on_target": "xgot",
}

PLAYER_STATS = {
    "minutes_played": "minutes",
    "rating_title": "rating",
    "goals": "goals",
    "assists": "assists",
    "expected_goals": "xg",
    "expected_assists": "xa",
    "expected_goals_non_penalty": "xg_np",
    "total_shots": "shots",
    "ShotsOnTarget": "sot",
    "ShotsOffTarget": "shots_off",
    "chances_created": "chances_created",
    "touches": "touches",
    "touches_opp_box": "touches_box",
    "dribbles_succeeded": "dribbles",
    "accurate_passes": "passes_acc",
    "passes_into_final_third": "passes_f3",
    "matchstats.headers.tackles": "tackles",
    "interceptions": "interceptions",
    "shot_blocks": "blocks",
    "clearances": "clearances",
    "recoveries": "recoveries",
    "dribbled_past": "dribbled_past",
    "ground_duels_won": "ground_duels_won",
    "aerials_won": "aerials_won",
    "duel_won": "duels_won",
    "duel_lost": "duels_lost",
    "was_fouled": "fouled",
    "fouls": "fouls",
    # goalkeeper
    "saves": "saves",
    "goals_conceded": "goals_conceded",
    "expected_goals_on_target_faced": "xgot_faced",
    "saves_inside_box": "saves_box",
    "keeper_high_claim": "high_claims",
    "punches": "punches",
    "keeper_sweeper": "sweeper",
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# FotMob's usualPosition is a coarse role code; positionId is the pitch slot
# (11 = keeper, 30s = defence, 60-80s = midfield, 100s = attack) and is 0 for
# players who came off the bench.
ROLES = {0: "GK", 1: "DF", 2: "MF", 3: "FW"}


def number(value):
    """FotMob mixes ints, floats, nulls and strings like '370 (81%)'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    match = _NUMBER.search(str(value))
    return float(match.group()) if match else None


def team_stat_map(payload: dict) -> dict[str, list]:
    """Flatten the stat blocks into {key: [home, away]}.

    Several keys appear more than once (a section header carries the same key
    with null values, and some stats are repeated in 'Top stats'), so the first
    occurrence that actually holds data wins.
    """
    groups = (
        ((payload.get("content") or {}).get("stats") or {})
        .get("Periods", {}).get("All", {}).get("stats", [])
    ) or []
    flat: dict[str, list] = {}
    for group in groups:
        for item in group.get("stats") or []:
            key = item.get("key")
            values = item.get("stats") or [None, None]
            if not key:
                continue
            parsed = [number(values[0]), number(values[1])]
            if all(v is None for v in parsed):
                continue
            flat.setdefault(key, parsed)
    return flat


def classify_zone(x: float, y: float) -> str:
    lateral = abs(y - GOAL_Y)
    if x >= SIX_X and lateral <= SIX_Y_HALF:
        return "sixyard"
    if x >= BOX_X and lateral <= BOX_Y_HALF:
        return "box_c" if lateral <= CENTRAL_Y_HALF else "box_w"
    return "out_c" if lateral <= CENTRAL_Y_HALF else "out_w"


def classify_situation(situation: str | None) -> str:
    mapping = {
        "RegularPlay": "open",
        "FastBreak": "fastbreak",
        "FromCorner": "corner",
        "FreeKick": "freekick",
        "SetPiece": "freekick",
        "Penalty": "penalty",
        "ThrowInSetPiece": "freekick",
    }
    return mapping.get(situation or "", "open")


def shot_breakdown(payload: dict, team_ids: tuple[int, int]) -> dict[int, dict]:
    """Per team: shot counts and xG summed by pitch zone and by situation."""
    empty = lambda: {
        **{f"z_{z}": 0 for z in ZONES},
        **{f"z_{z}_xg": 0.0 for z in ZONES},
        **{f"s_{s}": 0 for s in SITUATIONS},
        **{f"s_{s}_xg": 0.0 for s in SITUATIONS},
        "shot_dist_sum": 0.0, "shotmap_n": 0,
    }
    out = {team_id: empty() for team_id in team_ids}
    shots = ((payload.get("content") or {}).get("shotmap") or {}).get("shots") or []
    for shot in shots:
        team_id = shot.get("teamId")
        if team_id not in out or shot.get("isOwnGoal"):
            continue
        x, y = shot.get("x"), shot.get("y")
        if x is None or y is None:
            continue
        bucket = out[team_id]
        xg = float(shot.get("expectedGoals") or 0.0)
        zone = classify_zone(float(x), float(y))
        situation = classify_situation(shot.get("situation"))
        bucket[f"z_{zone}"] += 1
        bucket[f"z_{zone}_xg"] += xg
        bucket[f"s_{situation}"] += 1
        bucket[f"s_{situation}_xg"] += xg
        bucket["shot_dist_sum"] += ((GOAL_X - float(x)) ** 2 + (GOAL_Y - float(y)) ** 2) ** 0.5
        bucket["shotmap_n"] += 1
    for bucket in out.values():
        for zone in ZONES:
            bucket[f"z_{zone}_xg"] = round(bucket[f"z_{zone}_xg"], 4)
        for situation in SITUATIONS:
            bucket[f"s_{situation}_xg"] = round(bucket[f"s_{situation}_xg"], 4)
        bucket["shot_dist_avg"] = (
            round(bucket["shot_dist_sum"] / bucket["shotmap_n"], 2)
            if bucket["shotmap_n"] else None
        )
        bucket.pop("shot_dist_sum")
    return out


def _player_stat(player: dict, key: str):
    for group in player.get("stats") or []:
        for item in (group.get("stats") or {}).values():
            if item.get("key") == key:
                return number((item.get("stat") or {}).get("value"))
    return None


def lineup_info(payload: dict) -> dict[int, dict]:
    """Starter flag and card counts, which live in the lineup, not playerStats."""
    info: dict[int, dict] = {}
    lineup = (payload.get("content") or {}).get("lineup") or {}
    for side in ("homeTeam", "awayTeam"):
        team = lineup.get(side) or {}
        for group, started in (("starters", 1), ("subs", 0)):
            for player in team.get(group) or []:
                try:
                    player_id = int(player["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                events = (player.get("performance") or {}).get("events") or []
                info[player_id] = {
                    "started": started,
                    "yellow": sum(1 for e in events if e.get("type") == "yellowCard"),
                    "red": sum(1 for e in events if e.get("type") in {"redCard", "secondYellowCard"}),
                    "position": player.get("positionStringShort") or player.get("role"),
                }
    return info


def parse_match(payload: dict, league: str, season: str, competition: str = "") -> tuple[list[dict], list[dict]]:
    general = payload.get("general") or {}
    if not general.get("finished"):
        return [], []
    home_id, away_id = int(general["homeTeam"]["id"]), int(general["awayTeam"]["id"])
    header_teams = ((payload.get("header") or {}).get("teams") or [{}, {}])
    goals = [header_teams[0].get("score"), header_teams[1].get("score")]
    if goals[0] is None or goals[1] is None:
        return [], []

    match_id = int(general["matchId"])
    date = (general.get("matchTimeUTCDate") or "")[:10]
    info_box = ((payload.get("content") or {}).get("matchFacts") or {}).get("infoBox") or {}
    referee = ((info_box.get("Referee") or {}).get("text") or "") if isinstance(info_box.get("Referee"), dict) else ""
    flat = team_stat_map(payload)
    zones = shot_breakdown(payload, (home_id, away_id))

    team_rows = []
    for side, (team_id, opp_id) in enumerate(((home_id, away_id), (away_id, home_id))):
        other = 1 - side
        row = {
            "match_id": match_id, "date": date, "league": league, "season": season,
            "competition": competition or general.get("leagueName", ""),
            "round": general.get("matchRound"), "referee": referee,
            "team_id": team_id,
            "team": general["homeTeam"]["name"] if side == 0 else general["awayTeam"]["name"],
            "opp_id": opp_id,
            "opp": general["awayTeam"]["name"] if side == 0 else general["homeTeam"]["name"],
            "is_home": 1 - side,
            "gf": goals[side], "ga": goals[other],
        }
        for key, name in TEAM_STATS.items():
            values = flat.get(key)
            row[name] = values[side] if values else None
            row[f"{name}_ag"] = values[other] if values else None
        own_zone, opp_zone = zones[team_id], zones[opp_id]
        for name, value in own_zone.items():
            row[name] = value
        for name, value in opp_zone.items():
            row[f"{name}_ag"] = value
        team_rows.append(row)

    lineups = lineup_info(payload)
    player_rows = []
    for player in ((payload.get("content") or {}).get("playerStats") or {}).values():
        minutes = _player_stat(player, "minutes_played")
        if not minutes:
            continue
        player_id = int(player["id"])
        team_id = int(player["teamId"])
        extra = lineups.get(player_id, {})
        row = {
            "match_id": match_id, "date": date, "league": league, "season": season,
            "team_id": team_id, "opp_id": away_id if team_id == home_id else home_id,
            "is_home": 1 if team_id == home_id else 0,
            "player_id": player_id, "player": player.get("name"),
            "position": ROLES.get(player.get("usualPosition")),
            "pos_slot": player.get("positionId") or None,
            "is_gk": 1 if player.get("isGoalkeeper") else 0,
            "started": extra.get("started"),
            "yellow": extra.get("yellow"), "red": extra.get("red"),
        }
        for key, name in PLAYER_STATS.items():
            row[name] = _player_stat(player, key)
        player_rows.append(row)
    return team_rows, player_rows


TEAM_COLUMNS = None  # filled on first parse by callers that need a stable header


def team_columns() -> list[str]:
    base = [
        "match_id", "date", "league", "season", "competition", "round", "referee",
        "team_id", "team", "opp_id", "opp", "is_home", "gf", "ga",
    ]
    for name in TEAM_STATS.values():
        base += [name, f"{name}_ag"]
    zone_cols = [f"z_{z}" for z in ZONES] + [f"z_{z}_xg" for z in ZONES]
    zone_cols += [f"s_{s}" for s in SITUATIONS] + [f"s_{s}_xg" for s in SITUATIONS]
    zone_cols += ["shotmap_n", "shot_dist_avg"]
    for name in zone_cols:
        base += [name, f"{name}_ag"]
    return base


def player_columns() -> list[str]:
    return [
        "match_id", "date", "league", "season", "team_id", "opp_id", "is_home",
        "player_id", "player", "position", "pos_slot", "is_gk", "started",
        "yellow", "red",
    ] + list(PLAYER_STATS.values())
