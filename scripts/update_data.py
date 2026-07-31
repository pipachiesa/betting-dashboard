#!/usr/bin/env python3
"""Refresh the Liga Profesional betting dashboard snapshot from FotMob."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data.json"
BASE = "https://www.fotmob.com/api/data"
LEAGUE_ID = 112
TEAM_MATCH_LIMIT = 20
WORKERS = int(os.getenv("FETCH_WORKERS", "4"))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betting-dashboard/1.0)"}


def fetch_json(path: str, params: dict[str, object], attempts: int = 4) -> dict:
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.load(response)
        except Exception as exc:  # network and transient HTTP failures
            error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed after {attempts} attempts: {url}") from error


def current_teams() -> list[dict]:
    season = dt.datetime.now(dt.timezone.utc).year
    league = fetch_json("leagues", {"id": LEAGUE_ID, "ccode3": "ARG", "season": season})
    teams: dict[int, str] = {}
    for section in league.get("table", []):
        for subgroup in section.get("data", {}).get("tables", []):
            for row in subgroup.get("table", {}).get("all", []):
                teams[int(row["id"])] = row["name"]
    if not teams:
        raise RuntimeError(f"No teams found for Liga Profesional season {season}")
    return [{"id": team_id, "name": name} for team_id, name in sorted(teams.items())]


def team_matches(team: dict) -> dict:
    payload = fetch_json("teams", {"id": team["id"], "ccode3": "ARG"})
    fixtures = payload.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
    finished = [f for f in fixtures if f.get("status", {}).get("finished")]
    team["matches"] = [int(f["id"]) for f in finished[-TEAM_MATCH_LIMIT:]]
    return team


def nested_player_stat(player: dict, key: str, default=None):
    for group in player.get("stats", []):
        for item in group.get("stats", {}).values():
            if item.get("key") == key:
                return item.get("stat", {}).get("value", default)
    return default


def team_stat(payload: dict, key: str) -> list:
    content = payload.get("content") or {}
    groups = (
        (content.get("stats") or {})
        .get("Periods", {})
        .get("All", {})
        .get("stats", [])
    )
    for group in groups or []:
        for item in group.get("stats", []) or []:
            if item.get("key") == key:
                return item.get("stats", [None, None])
    return [None, None]


def match_detail(match_id: int) -> dict:
    payload = fetch_json("matchDetails", {"matchId": match_id})
    general = payload.get("general", {})
    header_teams = payload.get("header", {}).get("teams", [{}, {}])
    card_events: dict[int, int] = {}
    lineup = payload.get("content", {}).get("lineup") or {}
    for lineup_team in (lineup.get("homeTeam") or {}, lineup.get("awayTeam") or {}):
        for player in (lineup_team.get("starters") or []) + (lineup_team.get("subs") or []):
            events = (player.get("performance") or {}).get("events") or []
            card_events[int(player["id"])] = sum(
                1 for event in events if event.get("type") in {"yellowCard", "redCard", "secondYellowCard"}
            )
    players = []
    for player in (payload.get("content", {}).get("playerStats") or {}).values():
        minutes = nested_player_stat(player, "minutes_played", 0) or 0
        if minutes <= 0:
            continue
        players.append(
            {
                "id": int(player["id"]),
                "name": player["name"],
                "team": int(player["teamId"]),
                "shots": nested_player_stat(player, "total_shots", 0) or 0,
                "shotsOnTarget": nested_player_stat(player, "ShotsOnTarget", 0) or 0,
                "saves": nested_player_stat(player, "saves") if player.get("isGoalkeeper") else None,
                "cards": card_events.get(int(player["id"]), 0),
                "goalkeeper": bool(player.get("isGoalkeeper")),
            }
        )
    return {
        "id": int(general["matchId"]),
        "date": general.get("matchTimeUTCDate") or payload.get("header", {}).get("status", {}).get("utcTime"),
        "home": {"id": int(general["homeTeam"]["id"]), "name": general["homeTeam"]["name"], "goals": header_teams[0].get("score")},
        "away": {"id": int(general["awayTeam"]["id"]), "name": general["awayTeam"]["name"], "goals": header_teams[1].get("score")},
        "shots": team_stat(payload, "total_shots"),
        "shotsOnTarget": team_stat(payload, "ShotsOnTarget"),
        "corners": team_stat(payload, "corners"),
        "yellowCards": team_stat(payload, "yellow_cards"),
        "redCards": team_stat(payload, "red_cards"),
        "players": players,
    }


def pack(teams: list[dict], details: dict[int, dict], previous: dict) -> list[dict]:
    previous_teams = {team["i"]: team for team in previous.get("teams", [])}
    packed = []
    for team in teams:
        old_team = previous_teams.get(team["id"], {})
        roster: list[str] = list(old_team.get("r", []))
        roster_index = {name: index for index, name in enumerate(roster)}
        goalkeeper_names = {
            roster[index] for index in old_team.get("g", []) if 0 <= index < len(roster)
        }
        old_matches = {match["i"]: match for match in old_team.get("m", [])}
        matches = []

        def player_index(player: dict) -> int:
            name = player["name"]
            if name not in roster_index:
                roster_index[name] = len(roster)
                roster.append(player["name"])
            return roster_index[name]

        for match_id in team["matches"]:
            detail = details.get(match_id)
            if not detail:
                if match_id in old_matches:
                    matches.append(old_matches[match_id])
                continue
            is_home = detail["home"]["id"] == team["id"]
            side = 0 if is_home else 1
            opponent = detail["away"] if is_home else detail["home"]
            own = detail["home"] if is_home else detail["away"]
            rows = []
            for player in detail["players"]:
                if player["team"] != team["id"]:
                    continue
                index = player_index(player)
                if player["goalkeeper"]:
                    goalkeeper_names.add(player["name"])
                rows.append([index, player["shots"], player["shotsOnTarget"], player["saves"], player["cards"]])
            own_yellow = detail["yellowCards"][side]
            own_red = detail["redCards"][side]
            other_side = 1 - side
            rival_yellow = detail["yellowCards"][other_side]
            rival_red = detail["redCards"][other_side]
            own_cards = None if own_yellow is None and own_red is None else (own_yellow or 0) + (own_red or 0)
            rival_cards = None if rival_yellow is None and rival_red is None else (rival_yellow or 0) + (rival_red or 0)
            matches.append(
                {
                    "i": match_id,
                    "d": detail["date"][:10],
                    "o": opponent["name"],
                    "h": 1 if is_home else 0,
                    "x": [
                        detail["shots"][side],
                        detail["shotsOnTarget"][side],
                        detail["corners"][side],
                        own["goals"],
                        opponent["goals"],
                        own_cards,
                        rival_cards,
                    ],
                    "p": rows,
                }
            )
        used = sorted({row[0] for match in matches for row in match["p"]})
        remap = {old: new for new, old in enumerate(used)}
        compact_roster = [roster[index] for index in used]
        for match in matches:
            for row in match["p"]:
                row[0] = remap[row[0]]
        goalkeepers = sorted(
            index for index, name in enumerate(compact_roster) if name in goalkeeper_names
        )
        packed.append({"i": team["id"], "n": team["name"], "r": compact_roster, "g": goalkeepers, "m": matches})
    return packed


def main() -> None:
    previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {"teams": []}
    teams = current_teams()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        teams = list(pool.map(team_matches, teams))
    ids = sorted({match_id for team in teams for match_id in team["matches"]})
    known_ids = {
        match["i"] for team in previous.get("teams", []) for match in team.get("m", [])
    }
    new_ids = [match_id for match_id in ids if match_id not in known_ids]
    details: dict[int, dict] = {}
    failures: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(match_detail, match_id): match_id for match_id in new_ids}
        for future in concurrent.futures.as_completed(futures):
            match_id = futures[future]
            try:
                details[match_id] = future.result()
            except Exception as exc:
                failures.append(match_id)
                print(f"warning: match {match_id}: {exc}")
    if new_ids and len(details) < int(len(new_ids) * 0.5):
        raise RuntimeError(f"Coverage too low for new matches: {len(details)}/{len(new_ids)}")
    packed_teams = pack(teams, details, previous)
    if packed_teams == previous.get("teams", []) and sorted(failures) == sorted(previous.get("failedMatchIds", [])):
        print(f"no changes; checked {len(teams)} teams and fetched 0 match details")
        return
    snapshot = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "FotMob",
        "failedMatchIds": failures,
        "teams": packed_teams,
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"updated {len(teams)} teams; fetched {len(details)}/{len(new_ids)} new match details")


if __name__ == "__main__":
    main()
