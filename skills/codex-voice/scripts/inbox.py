"""Durable project-local message inbox and focus-control store for Codex Voice."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = "codex-voice/message/v0.1"
DB_NAME = "inbox.sqlite3"


def now_seconds() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_event_id(*parts: object) -> str:
    """Return a deterministic event id without persisting message content in logs."""
    import hashlib

    value = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def database_path(voice_root: Path) -> Path:
    return voice_root / DB_NAME


class Inbox:
    """SQLite-backed queue shared by the rollout watcher and short-lived helpers."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    schema TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    session_id TEXT,
                    thread_id TEXT,
                    turn_id TEXT,
                    profile_id TEXT,
                    avatar_id TEXT,
                    route_key TEXT,
                    tts_voice TEXT,
                    tts_speed REAL,
                    tts_mode TEXT,
                    session_label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sequence INTEGER,
                    volume INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    replay_count INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    last_error TEXT,
                    announced_key TEXT,
                    resume_text TEXT,
                    resume_offset INTEGER
                );
                CREATE INDEX IF NOT EXISTS messages_ready_idx
                    ON messages(status, available_at, id);
                CREATE INDEX IF NOT EXISTS messages_session_idx
                    ON messages(session_id, status, id);
                CREATE TABLE IF NOT EXISTS controls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS controls_pending_idx
                    ON controls(consumed_at, id);
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "resume_text" not in columns:
                connection.execute("ALTER TABLE messages ADD COLUMN resume_text TEXT")
            if "resume_offset" not in columns:
                connection.execute("ALTER TABLE messages ADD COLUMN resume_offset INTEGER")
            profile_columns = {
                "profile_id": "TEXT",
                "avatar_id": "TEXT",
                "route_key": "TEXT",
                "tts_voice": "TEXT",
                "tts_speed": "REAL",
                "tts_mode": "TEXT",
            }
            for name, column_type in profile_columns.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {column_type}")

    def enqueue(self, message: dict[str, object]) -> bool:
        required = ("event_id", "project_root", "kind", "text")
        if any(not isinstance(message.get(key), str) or not str(message[key]).strip() for key in required):
            raise ValueError("message requires non-empty event_id, project_root, kind, and text")
        schema = message.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise ValueError(f"unsupported message schema: {schema}")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    event_id, schema, project_root, session_id, thread_id, turn_id,
                    profile_id, avatar_id, route_key, tts_voice, tts_speed, tts_mode,
                    session_label, kind, text, sequence, volume, created_at,
                    status, available_at, announced_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    message["event_id"],
                    schema,
                    message["project_root"],
                    message.get("session_id"),
                    message.get("thread_id"),
                    message.get("turn_id"),
                    message.get("profile_id"),
                    message.get("avatar_id"),
                    message.get("route_key"),
                    message.get("tts_voice"),
                    message.get("tts_speed"),
                    message.get("tts_mode"),
                    message.get("session_label") or "Codex",
                    message["kind"],
                    message["text"],
                    message.get("sequence"),
                    max(0, min(100, int(message.get("volume", 100)))),
                    message.get("created_at") or iso_now(),
                    now_seconds(),
                    message.get("announced_key"),
                ),
            )
            return cursor.rowcount == 1

    def recover_inflight(self) -> int:
        """Return items abandoned by a watcher/process restart to the retry queue."""
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE messages
                SET status = 'retry', replay_count = replay_count + 1,
                    available_at = ?, last_error = 'recovered_after_watcher_restart'
                WHERE status = 'playing'
                """,
                (now_seconds(),),
            )
            return cursor.rowcount

    def discard_legacy_updates(self) -> int:
        """Retire commentary rows created before the ephemeral update lane.

        Older watcher revisions stored progress commentary as durable inbox
        messages. They are no longer replayable output, so a restart must
        clear them instead of speaking an old backlog or leaving a stale
        ``playing`` row behind.
        """
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE messages
                SET status = 'played',
                    last_error = 'discarded_legacy_ephemeral_update',
                    resume_text = NULL,
                    resume_offset = NULL
                WHERE kind IN ('commentary', 'update')
                  AND status IN ('queued', 'retry', 'playing')
                """
            )
            return cursor.rowcount

    def recover_input_state(self) -> dict[str, object] | None:
        """Release a capture lock that cannot survive a watcher restart."""
        focus = self.get_state("focus", {})
        focus = focus if isinstance(focus, dict) else {}
        state = str(focus.get("state", "idle"))
        if state == "idle":
            return None
        self.set_state("focus", {"state": "idle"})
        self.set_state("input", {"state": "idle"})
        return focus

    def claim_next(self, focused_session_id: str | None = None) -> dict[str, object] | None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if focused_session_id:
                row = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE status IN ('queued', 'retry') AND available_at <= ?
                      AND session_id = ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (now_seconds(), focused_session_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE status IN ('queued', 'retry') AND available_at <= ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (now_seconds(),),
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE messages SET status = 'playing', attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            return dict(row)

    def has_pending(self, focused_session_id: str | None = None) -> bool:
        """Return whether a durable message is waiting to be played.

        Ephemeral progress updates use a separate in-memory lane.  The
        playback arbiter uses this query to ensure a real message always wins
        over an update, including the short race between publishing an update
        and claiming the next queue item.
        """
        with self.connection() as connection:
            if focused_session_id:
                row = connection.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE status IN ('queued', 'retry') AND available_at <= ?
                      AND session_id = ?
                    LIMIT 1
                    """,
                    (now_seconds(), focused_session_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE status IN ('queued', 'retry') AND available_at <= ?
                    LIMIT 1
                    """,
                    (now_seconds(),),
                ).fetchone()
        return row is not None

    def complete(self, event_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE messages
                SET status = 'played', last_error = NULL,
                    resume_text = NULL, resume_offset = NULL
                WHERE event_id = ?
                """,
                (event_id,),
            )

    def requeue(
        self,
        event_id: str,
        *,
        delay_seconds: float = 0.0,
        error: str | None = None,
        resume_text: str | None = None,
        resume_offset: int | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE messages
                SET status = 'retry', replay_count = replay_count + 1,
                    available_at = ?, last_error = ?,
                    resume_text = COALESCE(?, resume_text),
                    resume_offset = COALESCE(?, resume_offset)
                WHERE event_id = ?
                """,
                (
                    now_seconds() + max(0.0, delay_seconds),
                    error,
                    resume_text,
                    resume_offset,
                    event_id,
                ),
            )

    def fail(self, event_id: str, error: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE messages SET status = 'failed', last_error = ? WHERE event_id = ?",
                (error[:500], event_id),
            )

    def add_control(self, command: str, payload: dict[str, object] | None = None) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO controls(command, payload_json, created_at) VALUES (?, ?, ?)",
                (command, json.dumps(payload or {}, separators=(",", ":")), now_seconds()),
            )
            return int(cursor.lastrowid)

    def consume_controls(self, limit: int = 16) -> list[dict[str, object]]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM controls WHERE consumed_at IS NULL ORDER BY id ASC LIMIT ?",
                (max(1, min(100, limit)),),
            ).fetchall()
            if not rows:
                return []
            ids = [int(row["id"]) for row in rows]
            connection.executemany(
                "UPDATE controls SET consumed_at = ? WHERE id = ?",
                [(now_seconds(), control_id) for control_id in ids],
            )
            result: list[dict[str, object]] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError:
                    payload = {}
                result.append({"id": row["id"], "command": row["command"], "payload": payload})
            return result

    def set_state(self, key: str, value: object) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, separators=(",", ":")), now_seconds()),
            )

    def next_counter(self, key: str) -> int:
        """Atomically increment a durable non-negative runtime counter."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
            try:
                current = int(json.loads(row["value_json"])) if row is not None else 0
            except (TypeError, ValueError, json.JSONDecodeError):
                current = 0
            value = max(0, current) + 1
            connection.execute(
                """
                INSERT INTO runtime_state(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), now_seconds()),
            )
            return value

    def get_state(self, key: str, default: object = None) -> object:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    def status(self) -> dict[str, object]:
        with self.connection() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM messages GROUP BY status"
                ).fetchall()
            }
        return {
            "schema": SCHEMA,
            "database": str(self.path),
            "messages": counts,
            "service": self.get_state("presence_service", {"state": "unknown"}),
            "attention": self.get_state("presence_attention", {"state": "unassigned"}),
            "focus": self.get_state("focus", {"state": "idle"}),
            "input": self.get_state("input", {"state": "idle"}),
        }


def new_input_id() -> str:
    return str(uuid.uuid4())
