#!/usr/bin/env python3
"""Backfill several completed seasons of league matches into history/.

One request per league-season returns every fixture id, then each match detail
is fetched through the caching client.  The cache doubles as the resume log, so
the job can be killed and restarted at any point without losing work or
re-downloading anything.

    python3 scripts/backfill.py --seasons 5
    python3 scripts/backfill.py --seasons 5 --leagues esp,eng --reparse
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import pathlib
import sys
import threading
import time

from fotmob import Fotmob
from parse import parse_match, player_columns, team_columns

ROOT = pathlib.Path(__file__).resolve().parents[1]
HISTORY = ROOT / "history"

LEAGUES = {
    "arg": {"id": 112, "ccode": "ARG", "name": "Liga Profesional"},
    "bra": {"id": 268, "ccode": "BRA", "name": "Brasileirao"},
    "usa": {"id": 130, "ccode": "USA", "name": "MLS"},
    "eng": {"id": 47, "ccode": "ENG", "name": "Premier League"},
    "esp": {"id": 87, "ccode": "ESP", "name": "LaLiga"},
    "ita": {"id": 55, "ccode": "ITA", "name": "Serie A"},
    "fra": {"id": 53, "ccode": "FRA", "name": "Ligue 1"},
}


def slug(season: str) -> str:
    return season.replace("/", "-")


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def collect_season(client: Fotmob, key: str, config: dict, season: str,
                   workers: int, reparse: bool) -> dict:
    team_path = HISTORY / "teams" / f"{key}_{slug(season)}.csv"
    player_path = HISTORY / "players" / f"{key}_{slug(season)}.csv"

    fixtures = client.season_matches(config["id"], config["ccode"], season)
    finished = [
        int(f["id"]) for f in fixtures
        if (f.get("status") or {}).get("finished") and not (f.get("status") or {}).get("cancelled")
    ]
    if not finished:
        return {"league": key, "season": season, "fixtures": 0, "matches": 0, "skipped": True}

    if team_path.exists() and not reparse and all(client.cached(m) for m in finished):
        with team_path.open(encoding="utf-8") as handle:
            existing = sum(1 for _ in handle) - 1
        return {"league": key, "season": season, "fixtures": len(finished),
                "matches": existing // 2, "cached": True}

    team_rows: list[dict] = []
    player_rows: list[dict] = []
    failures: list[int] = []
    no_stats: list[int] = []
    lock = threading.Lock()
    done = [0]
    started_at = time.time()

    def handle_match(match_id: int) -> None:
        try:
            payload = client.match_details(match_id)
            teams, players = parse_match(payload, key, season, config["name"])
        except Exception as exc:
            with lock:
                failures.append(match_id)
            print(f"    warn {key} {season} match {match_id}: {exc}", flush=True)
            return
        with lock:
            if not teams:
                no_stats.append(match_id)
            team_rows.extend(teams)
            player_rows.extend(players)
            done[0] += 1
            if done[0] % 100 == 0:
                rate = done[0] / max(1e-9, time.time() - started_at)
                print(f"    {key} {season}: {done[0]}/{len(finished)} ({rate:.1f}/s)", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(handle_match, finished))

    team_rows.sort(key=lambda r: (r["date"], r["match_id"], -r["is_home"]))
    player_rows.sort(key=lambda r: (r["date"], r["match_id"], r["player_id"]))
    write_csv(team_path, team_columns(), team_rows)
    write_csv(player_path, player_columns(), player_rows)
    return {
        "league": key, "season": season, "fixtures": len(finished),
        "matches": len(team_rows) // 2, "players": len(player_rows),
        "failed": failures, "no_stats": len(no_stats),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", type=int, default=5,
                        help="completed seasons to pull, newest first")
    parser.add_argument("--leagues", default=",".join(LEAGUES))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--interval", type=float, default=0.40,
                        help="minimum seconds between requests")
    parser.add_argument("--reparse", action="store_true",
                        help="rebuild CSVs from the cache without re-downloading")
    parser.add_argument("--include-current", action="store_true", default=True)
    args = parser.parse_args()

    keys = [k.strip() for k in args.leagues.split(",") if k.strip() in LEAGUES]
    client = Fotmob(min_interval=0.0 if args.reparse else args.interval)
    summary = []
    started_at = time.time()

    for key in keys:
        config = LEAGUES[key]
        seasons = client.available_seasons(config["id"], config["ccode"])
        # Newest first; the first entry is the season in progress.
        wanted = seasons[: args.seasons + (1 if args.include_current else 0)]
        print(f"[{key}] {config['name']}: {wanted}", flush=True)
        for season in wanted:
            try:
                result = collect_season(client, key, config, season, args.workers, args.reparse)
            except Exception as exc:
                print(f"  ERROR {key} {season}: {exc}", flush=True)
                summary.append({"league": key, "season": season, "error": str(exc)})
                continue
            summary.append(result)
            flag = " (cached)" if result.get("cached") else ""
            print(f"  {key} {season}: {result['matches']} partidos"
                  f" de {result['fixtures']} fixtures{flag}"
                  f"  [cache hits {client.stats['hits']}, descargas {client.stats['downloads']}]",
                  flush=True)

    HISTORY.mkdir(parents=True, exist_ok=True)
    (HISTORY / "manifest.json").write_text(
        json.dumps({
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsedSeconds": round(time.time() - started_at),
            "client": client.stats,
            "seasons": summary,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    total = sum(s.get("matches", 0) for s in summary)
    print(f"\nlisto: {total} partidos en {(time.time()-started_at)/60:.1f} min; {client.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
