#!/usr/bin/env python3
"""Shared FotMob client: polite rate limiting, backoff, and an on-disk cache.

The cache is the backbone of the backfill.  Downloading ~14k match details is a
multi-hour job, so a pruned copy of every payload is kept on disk: re-parsing
the whole archive after a schema change costs seconds instead of hours, and an
interrupted backfill resumes exactly where it stopped.
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import random
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.fotmob.com/api/data"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
# The cache lives outside the repository: it grows to hundreds of megabytes and
# must never reach git.  The default has to stay portable because the daily
# collector also runs on a Linux CI runner.
CACHE_ROOT = pathlib.Path(
    os.getenv("FOTMOB_CACHE") or (pathlib.Path(tempfile.gettempdir()) / "fotmob-cache")
)

# Bulky payload branches that carry nothing the model uses.
_DROP_CONTENT = (
    "liveticker", "superlive", "buzz", "momentum", "table", "h2h",
    "hasPlayoff", "weather",
)
_DROP_MATCHFACTS = (
    "highlights", "matchesInRound", "topPlayers", "poll", "playerOfTheMatch",
    "insights", "events",
)


class RateLimiter:
    """Thread-safe minimum spacing between requests."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if sleep_for:
            time.sleep(sleep_for)

    def penalise(self, seconds: float) -> None:
        """Push every waiting thread back after a rate-limit response."""
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)


def prune_match(payload: dict) -> dict:
    """Strip a matchDetails payload down to the branches the model reads.

    Cuts roughly 80% of the bytes while keeping stats, player stats, the
    shotmap, lineups and the info box (referee, stadium).
    """
    content = payload.get("content") or {}
    kept_content: dict = {}
    for key in ("stats", "playerStats", "shotmap", "lineup", "matchFacts"):
        if key in content:
            kept_content[key] = content[key]
    facts = kept_content.get("matchFacts")
    if isinstance(facts, dict):
        kept_content["matchFacts"] = {
            k: v for k, v in facts.items() if k not in _DROP_MATCHFACTS
        }
    # Per-player shotmaps duplicate content.shotmap.
    players = kept_content.get("playerStats")
    if isinstance(players, dict):
        for player in players.values():
            if isinstance(player, dict):
                player.pop("shotmap", None)
    return {
        "general": payload.get("general"),
        "header": payload.get("header"),
        "content": kept_content,
    }


class Fotmob:
    def __init__(self, min_interval: float = 0.45, attempts: int = 5,
                 cache_root: pathlib.Path | None = None):
        self.limiter = RateLimiter(min_interval)
        self.attempts = attempts
        self.cache_root = cache_root or CACHE_ROOT
        self.stats = {"hits": 0, "downloads": 0, "errors": 0}
        self._stats_lock = threading.Lock()

    # ---------------------------------------------------------------- network
    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{BASE}/{path}?{query}"
        error: Exception | None = None
        for attempt in range(self.attempts):
            self.limiter.wait()
            try:
                request = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(request, timeout=40) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                error = exc
                if exc.code in (429, 503):
                    # Back off hard and globally; the whole pool is throttled.
                    self.limiter.penalise(20.0 * (attempt + 1))
                elif exc.code in (404, 410):
                    raise
            except Exception as exc:  # transient network failures
                error = exc
            time.sleep(min(30.0, 1.5 * 2 ** attempt) + random.uniform(0, 0.6))
        with self._stats_lock:
            self.stats["errors"] += 1
        raise RuntimeError(f"failed after {self.attempts} attempts: {url}") from error

    # ------------------------------------------------------------------ cache
    def _cache_path(self, match_id: int) -> pathlib.Path:
        # Shard so no directory holds more than a few hundred files.
        return self.cache_root / "matches" / f"{int(match_id) % 256:03d}" / f"{match_id}.json.gz"

    def cached(self, match_id: int) -> bool:
        return self._cache_path(match_id).exists()

    def match_details(self, match_id: int, refresh: bool = False) -> dict:
        path = self._cache_path(match_id)
        if path.exists() and not refresh:
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
                with self._stats_lock:
                    self.stats["hits"] += 1
                return payload
            except Exception:
                path.unlink(missing_ok=True)  # corrupt entry, re-download
        payload = prune_match(self.get("matchDetails", matchId=match_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
        temporary.replace(path)  # atomic: a killed run never leaves a half file
        with self._stats_lock:
            self.stats["downloads"] += 1
        return payload

    # ---------------------------------------------------------------- helpers
    def season_matches(self, league_id: int, ccode: str, season: str | None) -> list[dict]:
        """Every fixture of a league season, from one request."""
        payload = self.get("leagues", id=league_id, ccode3=ccode, season=season)
        return (payload.get("fixtures") or {}).get("allMatches") or []

    def available_seasons(self, league_id: int, ccode: str) -> list[str]:
        payload = self.get("leagues", id=league_id, ccode3=ccode)
        return payload.get("allAvailableSeasons") or []
