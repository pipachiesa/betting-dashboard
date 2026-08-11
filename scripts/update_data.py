#!/usr/bin/env python3
"""Refresh the multi-league betting dashboard snapshot from FotMob."""

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
TEAM_MATCH_LIMIT = 20
STORED_MATCH_LIMIT = 60
WORKERS = int(os.getenv("FETCH_WORKERS", "4"))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betting-dashboard/1.0)"}
SCHEMA_VERSION = 2
LEAGUES = (
    {"key": "arg", "name": "Liga Profesional", "country": "Argentina", "id": 112, "ccode": "ARG", "season": "2026", "groups": ("Zona A", "Zona B"), "start": "2026-01-01", "per_run": 12},
    # Controlled backfills keep already-started leagues from causing a large burst.
    {"key": "bra", "name": "Brasileirão", "country": "Brasil", "id": 268, "ccode": "BRA", "season": "2026", "groups": None, "start": "2026-01-01", "per_run": 40},
    {"key": "usa", "name": "MLS", "country": "Estados Unidos", "id": 130, "ccode": "USA", "season": "2026", "groups": ("Conferencia Este", "Conferencia Oeste"), "start": "2026-02-21", "per_run": 40},
    {"key": "eng", "name": "Premier League", "country": "Inglaterra", "id": 47, "ccode": "ENG", "season": "2026/2027", "groups": None, "start": "2026-08-21", "per_run": 12},
    {"key": "esp", "name": "LaLiga", "country": "España", "id": 87, "ccode": "ESP", "season": "2026/2027", "groups": None, "start": "2026-08-15", "per_run": 12},
    {"key": "ita", "name": "Serie A", "country": "Italia", "id": 55, "ccode": "ITA", "season": "2026/2027", "groups": None, "start": "2026-08-22", "per_run": 12},
    {"key": "fra", "name": "Ligue 1", "country": "Francia", "id": 53, "ccode": "FRA", "season": "2026/2027", "groups": None, "start": "2026-08-21", "per_run": 12},
)
COMPETITIONS = {
    "arg": {
        "Liga Profesional Apertura": ("liga-profesional", "Liga Profesional"),
        "Liga Profesional Apertura Playoff": ("liga-profesional", "Liga Profesional"),
        "Liga Profesional Clausura": ("liga-profesional", "Liga Profesional"),
        "Copa Libertadores": ("libertadores", "Copa Libertadores"),
        "Copa Sudamericana": ("sudamericana", "Copa Sudamericana"),
        "Copa Argentina": ("copa-argentina", "Copa Argentina"),
    },
    "bra": {
        "Serie A": ("brasileirao", "Brasileirão"),
        "Copa Libertadores": ("libertadores", "Copa Libertadores"),
        "Copa Sudamericana": ("sudamericana", "Copa Sudamericana"),
        "Copa do Brasil": ("copa-do-brasil", "Copa do Brasil"),
    },
    "usa": {
        "Major League Soccer": ("mls", "MLS"),
        "US Open Cup": ("us-open-cup", "US Open Cup"),
        "Leagues Cup": ("leagues-cup", "Leagues Cup"),
        "CONCACAF Champions Cup": ("concacaf-champions-cup", "CONCACAF Champions Cup"),
    },
    "eng": {
        "Premier League": ("premier-league", "Premier League"),
        "FA Cup": ("fa-cup", "FA Cup"),
        "EFL Cup": ("efl-cup", "EFL Cup"),
        "Champions League": ("champions-league", "Champions League"),
        "Europa League": ("europa-league", "Europa League"),
        "Conference League": ("conference-league", "Conference League"),
    },
    "esp": {
        "LaLiga": ("la-liga", "LaLiga"),
        "Copa del Rey": ("copa-del-rey", "Copa del Rey"),
        "Champions League": ("champions-league", "Champions League"),
        "Europa League": ("europa-league", "Europa League"),
        "Conference League": ("conference-league", "Conference League"),
    },
    "ita": {
        "Serie A": ("serie-a", "Serie A"),
        "Coppa Italia": ("coppa-italia", "Coppa Italia"),
        "Champions League": ("champions-league", "Champions League"),
        "Europa League": ("europa-league", "Europa League"),
        "Conference League": ("conference-league", "Conference League"),
    },
    "fra": {
        "Ligue 1": ("ligue-1", "Ligue 1"),
        "Coupe de France": ("coupe-de-france", "Coupe de France"),
        "Champions League": ("champions-league", "Champions League"),
        "Europa League": ("europa-league", "Europa League"),
        "Conference League": ("conference-league", "Conference League"),
    },
}


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


def current_teams(league_config: dict) -> list[dict]:
    league = fetch_json("leagues", {"id": league_config["id"], "ccode3": league_config["ccode"], "season": league_config["season"]})
    teams: dict[int, dict] = {}
    for section in league.get("table", []):
        data = section.get("data", {})
        group_names = league_config.get("groups")
        subgroups = data.get("tables", [])[:len(group_names)] if group_names else [{"table": data.get("table", {})}]
        for table_index, subgroup in enumerate(subgroups):
            zone = group_names[table_index] if group_names else ""
            for row in subgroup.get("table", {}).get("all", []):
                teams[int(row["id"])] = {"id": int(row["id"]), "name": row["name"], "zone": zone, "league": league_config["key"], "league_id": league_config["id"], "ccode": league_config["ccode"]}
    if not teams:
        raise RuntimeError(f"No teams found for {league_config['name']} season {league_config['season']}")
    return sorted(teams.values(), key=lambda team: (team["zone"], team["name"]))


def team_matches(team: dict) -> dict:
    payload = fetch_json("teams", {"id": team["id"], "ccode3": team["ccode"]})
    fixtures = payload.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
    competition_map = COMPETITIONS[team["league"]]
    start = next(config["start"] for config in LEAGUES if config["key"] == team["league"])
    tracked_fixtures = [
        f for f in fixtures
        if (f.get("tournament") or {}).get("name") in competition_map
        and (((f.get("status") or {}).get("utcTime") or "")[:10] >= start)
    ]
    finished = [f for f in tracked_fixtures if f.get("status", {}).get("finished")]
    team["matches"] = [int(f["id"]) for f in finished[-STORED_MATCH_LIMIT:]]
    team["date_by_match"] = {
        int(f["id"]): ((f.get("status") or {}).get("utcTime") or "")[:10]
        for f in finished
    }
    team["competition_by_match"] = {
        int(f["id"]): competition_map[(f.get("tournament") or {})["name"]]
        for f in finished
    }
    team["upcoming"] = [
        {
            "id": int(f["id"]),
            "date": (f.get("status") or {}).get("utcTime"),
            "home": int((f.get("home") or {})["id"]),
            "away": int((f.get("away") or {})["id"]),
            "league": team["league"],
            "competition": competition_map[(f.get("tournament") or {})["name"]][0],
        }
        for f in tracked_fixtures
        if not (f.get("status") or {}).get("finished") and not (f.get("status") or {}).get("cancelled")
    ]
    squad = {}
    for group in (payload.get("squad") or {}).get("squad", []):
        role = (group.get("title") or "").lower()
        if role == "attackers":
            role = "forwards"
        if role not in {"keepers", "defenders", "midfielders", "forwards"}:
            continue
        for member in group.get("members") or []:
            if member.get("name"):
                squad[member["name"]] = role
    team["squad"] = squad
    return team


def nested_player_stat(player: dict, key: str, default=None):
    for group in player.get("stats", []):
        for item in group.get("stats", {}).values():
            if item.get("key") == key:
                return item.get("stat", {}).get("value", default)
    return default


def count_player_stat(player: dict, key: str, covered: bool):
    """FotMob uses null for a covered counting stat whose value is zero."""
    if not covered:
        return None
    return nested_player_stat(player, key, 0) or 0


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
    fouls = team_stat(payload, "fouls")
    tackles = team_stat(payload, "matchstats.headers.tackles")
    fouls_covered = any(value is not None for value in fouls)
    tackles_covered = any(value is not None for value in tackles)
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
                "foulsReceived": count_player_stat(player, "was_fouled", fouls_covered),
                "foulsCommitted": count_player_stat(player, "fouls", fouls_covered),
                "tackles": count_player_stat(player, "matchstats.headers.tackles", tackles_covered),
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
        "fouls": fouls,
        "tackles": tackles,
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
        default_competition = next(iter(COMPETITIONS[team["league"]].values()))
        matches = []

        def player_index(player: dict) -> int:
            name = player["name"]
            if name not in roster_index:
                roster_index[name] = len(roster)
                roster.append(player["name"])
            return roster_index[name]

        # The squad endpoint is the source of truth for the active roster.  A
        # player must be selectable even when he has not appeared in the
        # currently downloaded match sample yet.
        for name in team.get("squad", {}):
            player_index({"name": name})

        match_order = {match_id: index for index, match_id in enumerate(team["matches"])}
        match_ids = list(dict.fromkeys([*old_matches, *team["matches"]]))
        match_ids.sort(key=lambda match_id: (old_matches.get(match_id, {}).get("d") or team["date_by_match"].get(match_id, ""), match_order.get(match_id, -1)))
        for match_id in match_ids[-STORED_MATCH_LIMIT:]:
            detail = details.get(match_id)
            if not detail:
                if match_id in old_matches:
                    old_match = dict(old_matches[match_id])
                    old_match["c"] = team["competition_by_match"].get(match_id, default_competition)[0]
                    matches.append(old_match)
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
                rows.append([
                    index,
                    player["shots"],
                    player["shotsOnTarget"],
                    player["saves"],
                    player["cards"],
                    player["foulsReceived"],
                    player["foulsCommitted"],
                    player["tackles"],
                ])
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
                    "c": team["competition_by_match"].get(match_id, default_competition)[0],
                    "x": [
                        detail["shots"][side],
                        detail["shotsOnTarget"][side],
                        detail["corners"][side],
                        own["goals"],
                        opponent["goals"],
                        own_cards,
                        rival_cards,
                        detail["fouls"][side],
                        detail["fouls"][other_side],
                        detail["tackles"][side],
                    ],
                    "p": rows,
                }
            )
        used = sorted(
            {row[0] for match in matches for row in match["p"]}
            | {roster_index[name] for name in team.get("squad", {})}
        )
        remap = {old: new for new, old in enumerate(used)}
        compact_roster = [roster[index] for index in used]
        for match in matches:
            for row in match["p"]:
                row[0] = remap[row[0]]
        goalkeepers = sorted(
            index for index, name in enumerate(compact_roster) if name in goalkeeper_names
        )
        active = {name: team.get("squad", {}).get(name) for name in compact_roster if name in team.get("squad", {})}
        packed.append({"i": team["id"], "n": team["name"], "l": team["league"], "z": team["zone"], "r": compact_roster, "g": goalkeepers, "a": active, "m": matches})
    return packed


def main() -> None:
    previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {"teams": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        team_groups = list(pool.map(current_teams, LEAGUES))
    teams = [team for group in team_groups for team in group]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        teams = list(pool.map(team_matches, teams))
    previous_teams = {team["i"]: team for team in previous.get("teams", [])}
    allowed_ids: set[int] = set()
    for config in LEAGUES:
        candidates: set[int] = set()
        for team in (item for item in teams if item["league"] == config["key"]):
            old_matches = {match["i"]: match for match in previous_teams.get(team["id"], {}).get("m", [])}
            candidates.update(match_id for match_id in team["matches"] if match_id not in old_matches)
            # Schema upgrades are gradual too: refresh old details until the new
            # fouls/tackles columns exist, instead of turning missing data into 0.
            candidates.update(
                match_id for match_id, match in old_matches.items()
                if match_id in team["matches"]
                and (len(match.get("x", [])) < 10 or any(len(row) < 8 for row in match.get("p", [])))
            )
        allowed_ids.update(sorted(candidates, reverse=True)[:config["per_run"]])
    new_ids = sorted(allowed_ids)
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
    upcoming_by_league: dict[str, dict[int, dict]] = {}
    for team in teams:
        for fixture in team.get("upcoming", []):
            upcoming_by_league.setdefault(fixture["league"], {})[fixture["id"]] = fixture
    upcoming = [
        fixture
        for league_fixtures in upcoming_by_league.values()
        for fixture in sorted(league_fixtures.values(), key=lambda item: item["date"] or "")[:40]
    ]
    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "FotMob",
        "leagues": [
            {
                "k": config["key"],
                "n": config["name"],
                "country": config["country"],
                "competitions": [
                    {"k": key, "n": name}
                    for key, name in dict(COMPETITIONS[config["key"]].values()).items()
                ],
            }
            for config in LEAGUES
        ],
        "failedMatchIds": failures,
        "teams": packed_teams,
        "fixtures": sorted(upcoming, key=lambda fixture: fixture["date"] or ""),
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"updated {len(teams)} teams; fetched {len(details)}/{len(new_ids)} new match details")


if __name__ == "__main__":
    main()
