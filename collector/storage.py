"""Слой хранения: SQLite (историан + журнал алармов)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from common.alarms import AlarmEvent

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS measurements (
    id      INTEGER PRIMARY KEY,
    ts      TEXT    NOT NULL,          -- ISO-8601 UTC, время источника (SourceTimestamp)
    tag     TEXT    NOT NULL,
    value   REAL,                      -- числовые/булевы значения
    value_s TEXT,                      -- строковые значения
    quality TEXT    NOT NULL DEFAULT 'Good'
);
CREATE INDEX IF NOT EXISTS ix_meas_tag_ts ON measurements(tag, ts);
CREATE INDEX IF NOT EXISTS ix_meas_ts ON measurements(ts);

CREATE TABLE IF NOT EXISTS alarms (
    id       INTEGER PRIMARY KEY,
    ts_on    TEXT NOT NULL,
    ts_off   TEXT,
    tag      TEXT NOT NULL,
    level    TEXT NOT NULL,            -- LL / L / H / HH
    value_on REAL NOT NULL,
    value_off REAL,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alarms_open ON alarms(tag, level) WHERE ts_off IS NULL;

CREATE TABLE IF NOT EXISTS events (
    id   INTEGER PRIMARY KEY,
    ts   TEXT NOT NULL,
    kind TEXT NOT NULL,                -- connect / disconnect / error / info
    text TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit; транзакции вручную
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def write_measurements(self, rows: list[tuple[datetime, str, object, str]]) -> None:
        """rows: (ts, tag, value, quality). Пакетная вставка в одной транзакции."""
        if not rows:
            return
        payload = []
        for ts, tag, value, quality in rows:
            if isinstance(value, str):
                payload.append((ts.isoformat(), tag, None, value, quality))
            else:
                payload.append((ts.isoformat(), tag, float(value) if value is not None else None, None, quality))
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(
                "INSERT INTO measurements(ts, tag, value, value_s, quality) VALUES (?,?,?,?,?)", payload)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def write_alarm(self, ev: AlarmEvent) -> None:
        if ev.active:
            self.conn.execute(
                "INSERT INTO alarms(ts_on, tag, level, value_on, message) VALUES (?,?,?,?,?)",
                (ev.ts.isoformat(), ev.tag, ev.level.value, ev.value, ev.message))
        else:
            self.conn.execute(
                "UPDATE alarms SET ts_off=?, value_off=? WHERE tag=? AND level=? AND ts_off IS NULL",
                (ev.ts.isoformat(), ev.value, ev.tag, ev.level.value))

    def write_event(self, kind: str, text: str, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        self.conn.execute("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (ts.isoformat(), kind, text))

    def close_all_open_alarms(self, ts: datetime, reason: str) -> None:
        """При потере связи все активные алармы закрываем, чтобы не висели вечно."""
        self.conn.execute(
            "UPDATE alarms SET ts_off=?, message = message || ' [' || ? || ']' WHERE ts_off IS NULL",
            (ts.isoformat(), reason))
