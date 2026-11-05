"""SQLite storage for package sightings."""

import os
import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    name       TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    hits       INTEGER NOT NULL,
    last_proc  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(slots=True)
class Sighting:
    first_seen: int
    last_seen: int
    hits: int
    last_proc: str


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=5)
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def write_sightings(self, sightings: dict[str, Sighting]) -> None:
        """Merge sightings in one transaction. Raises on failure, writing nothing."""
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO packages (name, first_seen, last_seen, hits, last_proc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (name) DO UPDATE SET
                    last_seen = MAX(last_seen, excluded.last_seen),
                    hits = hits + excluded.hits,
                    last_proc = excluded.last_proc
                """,
                [
                    (name, s.first_seen, s.last_seen, s.hits, s.last_proc)
                    for name, s in sightings.items()
                ],
            )

    def record_start(self, now: int) -> None:
        """Keep the first start forever: the report measures observation from it."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('daemon_started_at', ?)",
                (str(now),),
            )
            self._set_meta("last_started_at", now)

    def record_reindex(self, now: int) -> None:
        with self._conn:
            self._set_meta("last_reindex", now)

    def _set_meta(self, key: str, value: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
        )

    def close(self) -> None:
        self._conn.close()
