"""Local SQLite storage: API response cache, video snapshots, Studio metrics and quota usage.

SQLite runs in WAL mode so reads never block writes. All queries are tiny, so a single
connection guarded by a lock is faster than a pool for this workload.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sqlite3
import threading
import time
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cache (
  cache_key   TEXT PRIMARY KEY,
  payload     TEXT NOT NULL,
  expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
  video_id    TEXT PRIMARY KEY,
  channel_id  TEXT NOT NULL,
  payload     TEXT NOT NULL,
  fetched_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE TABLE IF NOT EXISTS video_snapshots (
  video_id    TEXT NOT NULL,
  captured_at REAL NOT NULL,
  views       INTEGER, likes INTEGER, comments INTEGER,
  PRIMARY KEY (video_id, captured_at)
);
CREATE TABLE IF NOT EXISTS shorts_flags (
  video_id    TEXT PRIMARY KEY,
  is_short    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_metrics (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id    TEXT NOT NULL,
  recorded_at REAL NOT NULL,
  viewed_pct  REAL, swiped_pct REAL, impressions INTEGER, ctr_pct REAL,
  notes       TEXT
);
CREATE TABLE IF NOT EXISTS quota_usage (
  day         TEXT PRIMARY KEY,
  units       INTEGER NOT NULL
);
"""

# Snapshots closer together than this are merged, so repeated calls don't bloat the table.
_MIN_SNAPSHOT_GAP_SECONDS = 15 * 60


def _pacific_day() -> str:
    """YouTube quota resets at midnight Pacific time; approximate with UTC-8."""
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=8)
    return now.strftime("%Y-%m-%d")


class Storage:
    """Thread-safe wrapper around one SQLite database file."""

    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute_many(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany(sql, rows)
            self._conn.execute("COMMIT")

    # ---- response cache -------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        rows = self._query("SELECT payload, expires_at FROM api_cache WHERE cache_key=?", (key,))
        if not rows or rows[0][1] < time.time():
            return None
        return json.loads(rows[0][0])

    def cache_put(self, key: str, payload: Any, ttl_seconds: int) -> None:
        self._query(
            "INSERT OR REPLACE INTO api_cache VALUES (?,?,?)",
            (key, json.dumps(payload), time.time() + ttl_seconds),
        )

    def cache_purge_expired(self) -> int:
        return len(self._query("DELETE FROM api_cache WHERE expires_at < ? RETURNING 1", (time.time(),)))

    # ---- videos + snapshots ----------------------------------------------
    def save_videos(self, videos: list[dict[str, Any]]) -> None:
        """Upserts video records and appends a stats snapshot for growth tracking."""
        now = time.time()
        self._execute_many(
            "INSERT OR REPLACE INTO videos VALUES (?,?,?,?)",
            [(v["video_id"], v["channel_id"], json.dumps(v), now) for v in videos],
        )
        latest = {
            row[0]: row[1]
            for row in self._query("SELECT video_id, MAX(captured_at) FROM video_snapshots GROUP BY video_id")
        }
        fresh = [
            (v["video_id"], now, v.get("views"), v.get("likes"), v.get("comments"))
            for v in videos
            if now - latest.get(v["video_id"], 0) >= _MIN_SNAPSHOT_GAP_SECONDS
        ]
        if fresh:
            self._execute_many("INSERT OR REPLACE INTO video_snapshots VALUES (?,?,?,?,?)", fresh)

    def snapshots(self, video_ids: list[str]) -> dict[str, list[tuple[float, int]]]:
        """Returns {video_id: [(captured_at, views), ...]} oldest first."""
        if not video_ids:
            return {}
        marks = ",".join("?" * len(video_ids))
        out: dict[str, list[tuple[float, int]]] = {}
        for vid, at, views in self._query(
            f"SELECT video_id, captured_at, views FROM video_snapshots WHERE video_id IN ({marks}) "  # noqa: S608 (only ? marks)
            "ORDER BY captured_at",
            tuple(video_ids),
        ):
            out.setdefault(vid, []).append((at, views or 0))
        return out

    # ---- Shorts flags ----------------------------------------------------
    def shorts_flags(self, video_ids: list[str]) -> dict[str, bool]:
        if not video_ids:
            return {}
        marks = ",".join("?" * len(video_ids))
        sql = f"SELECT video_id, is_short FROM shorts_flags WHERE video_id IN ({marks})"  # noqa: S608 (only ? marks)
        rows = self._query(sql, tuple(video_ids))
        return {vid: bool(flag) for vid, flag in rows}

    def save_shorts_flags(self, flags: dict[str, bool]) -> None:
        if flags:
            self._execute_many(
                "INSERT OR REPLACE INTO shorts_flags VALUES (?,?)", [(k, int(v)) for k, v in flags.items()]
            )

    # ---- Studio-only metrics (entered by the creator) ---------------------
    def add_studio_metrics(self, video_id: str, **fields: Any) -> int:
        rows = self._query(
            "INSERT INTO studio_metrics (video_id, recorded_at, viewed_pct, swiped_pct, impressions, ctr_pct, notes) "
            "VALUES (?,?,?,?,?,?,?) RETURNING id",
            (
                video_id,
                time.time(),
                fields.get("viewed_pct"),
                fields.get("swiped_pct"),
                fields.get("impressions"),
                fields.get("ctr_pct"),
                fields.get("notes"),
            ),
        )
        return rows[0][0]

    def studio_metrics(self, video_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT video_id, recorded_at, viewed_pct, swiped_pct, impressions, ctr_pct, notes FROM studio_metrics"
        params: tuple = ()
        if video_id:
            sql += " WHERE video_id=?"
            params = (video_id,)
        keys = ("video_id", "recorded_at", "viewed_pct", "swiped_pct", "impressions", "ctr_pct", "notes")
        result = []
        for row in self._query(sql + " ORDER BY recorded_at DESC LIMIT 500", params):
            item = dict(zip(keys, row, strict=True))
            item["recorded_at"] = datetime.datetime.fromtimestamp(item["recorded_at"], datetime.UTC).isoformat()
            result.append(item)
        return result

    # ---- quota ------------------------------------------------------------
    def quota_used_today(self) -> int:
        rows = self._query("SELECT units FROM quota_usage WHERE day=?", (_pacific_day(),))
        return rows[0][0] if rows else 0

    def add_quota(self, units: int) -> int:
        rows = self._query(
            "INSERT INTO quota_usage VALUES (?,?) ON CONFLICT(day) DO UPDATE SET units=units+excluded.units "
            "RETURNING units",
            (_pacific_day(), units),
        )
        return rows[0][0]
