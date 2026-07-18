"""WAL-mode authoritative persistence for Presence Runtime v0.2."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import ConflictError, NotFoundError, ValidationError
from .models import EffectiveSnapshot
from .paths import normalize_project_root, state_database_path
from .validation import validate_patch


LEASE_REFRESH_SECONDS = 15
LEASE_EXPIRY_SECONDS = 45


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    provider TEXT NOT NULL DEFAULT 'cpu',
    microphone_permission INTEGER NOT NULL DEFAULT 0 CHECK (microphone_permission IN (0, 1)),
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    normalized_root TEXT NOT NULL UNIQUE,
    display_root TEXT NOT NULL,
    registered INTEGER NOT NULL DEFAULT 1 CHECK (registered IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bindings (
    binding_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    scope TEXT NOT NULL CHECK (scope IN ('project', 'session')),
    session_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'dormant' CHECK (state IN ('active', 'dormant', 'deleted')),
    effective_revision INTEGER NOT NULL DEFAULT 0,
    candidate_revision INTEGER,
    current_activity TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, scope, session_id)
);

CREATE TABLE IF NOT EXISTS project_defaults (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE CASCADE,
    patch_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_overrides (
    binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id) ON DELETE CASCADE,
    patch_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL,
    lease_token_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    connected INTEGER NOT NULL DEFAULT 1 CHECK (connected IN (0, 1)),
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sources_binding_active
    ON sources(binding_id, connected, expires_at);

CREATE TABLE IF NOT EXISTS effective_snapshots (
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'superseded', 'failed')),
    diagnostic TEXT,
    requires_voice INTEGER NOT NULL DEFAULT 1 CHECK (requires_voice IN (0, 1)),
    requires_renderer INTEGER NOT NULL DEFAULT 1 CHECK (requires_renderer IN (0, 1)),
    voice_ack INTEGER NOT NULL DEFAULT 0 CHECK (voice_ack IN (0, 1)),
    renderer_ack INTEGER NOT NULL DEFAULT 0 CHECK (renderer_ack IN (0, 1)),
    created_at REAL NOT NULL,
    acknowledged_at REAL,
    PRIMARY KEY(binding_id, revision)
);

CREATE TABLE IF NOT EXISTS configuration_transactions (
    transaction_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('project', 'session', 'activity', 'migration')),
    target_id TEXT NOT NULL,
    previous_json TEXT,
    candidate_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'committed', 'failed', 'rolled_back')),
    diagnostic TEXT,
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS renderer_geometry (
    binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id) ON DELETE CASCADE,
    geometry_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event_dedup (
    event_id TEXT PRIMARY KEY,
    source_id TEXT,
    binding_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS speech_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id) ON DELETE RESTRICT,
    effective_revision INTEGER NOT NULL,
    utterance_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    tts_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'claimed', 'playing', 'paused', 'finished', 'cancelled', 'failed')),
    cancel_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_speech_queue_status
    ON speech_queue(status, queue_id);

CREATE TABLE IF NOT EXISTS attention_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    binding_id TEXT,
    utterance_id TEXT,
    state TEXT NOT NULL DEFAULT 'idle',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_references (
    kind TEXT NOT NULL CHECK (kind IN ('avatar', 'profile', 'preset')),
    reference TEXT NOT NULL,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    PRIMARY KEY(kind, reference, binding_id, source)
);

CREATE TABLE IF NOT EXISTS migration_ledger (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE RESTRICT,
    source_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed', 'failed', 'rolled_back')),
    details_json TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS input_transcripts (
    input_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id) ON DELETE CASCADE,
    capture_id TEXT NOT NULL UNIQUE,
    transcript TEXT,
    status TEXT NOT NULL CHECK (status IN ('transcribing', 'ready', 'delivered', 'failed')),
    diagnostic TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_input_transcripts_ready
    ON input_transcripts(binding_id, status, created_at);
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decode_json(value: str) -> Any:
    return json.loads(value)


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _validate_session_id(session_id: str) -> str:
    try:
        parsed = uuid.UUID(session_id)
    except (ValueError, AttributeError) as exc:
        raise ValidationError("session id must be a UUID", path="session_id") from exc
    canonical = str(parsed)
    if session_id.lower() != canonical:
        raise ValidationError("session id must use canonical UUID form", path="session_id")
    return canonical


class PresenceStore:
    """The only persistent owner for runtime/project/session configuration."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or state_database_path()).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=5,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise ConflictError(f"Could not enable SQLite WAL mode: {mode}")
            self._connection.executescript(SCHEMA)
            project_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(projects)")
            }
            if "registered" not in project_columns:
                self._connection.execute(
                    "ALTER TABLE projects ADD COLUMN registered INTEGER NOT NULL DEFAULT 1"
                )
            now = time.time()
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_meta(key, value)
                VALUES('schema', 'presence/state/v0.2')
                """
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO runtime_settings(singleton, provider, microphone_permission, updated_at)
                VALUES(1, 'cpu', 0, ?)
                """,
                (now,),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO attention_state(singleton, state, updated_at)
                VALUES(1, 'idle', ?)
                """,
                (now,),
            )

    @property
    def journal_mode(self) -> str:
        return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "PresenceStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def runtime_settings(self) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT provider, microphone_permission, updated_at FROM runtime_settings WHERE singleton=1"
        ).fetchone()
        return {
            "provider": row["provider"],
            "microphone_permission": bool(row["microphone_permission"]),
            "updated_at": _utc_iso(row["updated_at"]),
        }

    def set_runtime_policy(
        self,
        *,
        provider: str | None = None,
        microphone_permission: bool | None = None,
    ) -> dict[str, Any]:
        allowed_providers = {"cpu", "cuda", "directml", "openvino"}
        if provider is not None and provider not in allowed_providers:
            raise ValidationError(
                f"provider must be one of {sorted(allowed_providers)}",
                path="provider",
            )
        if microphone_permission is not None and not isinstance(microphone_permission, bool):
            raise ValidationError("must be a boolean", path="microphone_permission")
        current = self.runtime_settings()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE runtime_settings
                SET provider=?, microphone_permission=?, updated_at=?
                WHERE singleton=1
                """,
                (
                    provider if provider is not None else current["provider"],
                    int(
                        microphone_permission
                        if microphone_permission is not None
                        else current["microphone_permission"]
                    ),
                    time.time(),
                ),
            )
        return self.runtime_settings()

    def register_project(self, root: str | Path) -> dict[str, Any]:
        normalized, display = normalize_project_root(root)
        with self.transaction() as connection:
            return self._register_project(connection, normalized, display)

    def _register_project(
        self,
        connection: sqlite3.Connection,
        normalized: str,
        display: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM projects WHERE normalized_root=? AND registered=1",
            (normalized,),
        ).fetchone()
        if row is None:
            now = time.time()
            project_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, normalized_root, display_root, registered, created_at, updated_at
                )
                VALUES(?, ?, ?, 1, ?, ?)
                """,
                (project_id, normalized, display, now, now),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return self._project_document(row)

    @staticmethod
    def _project_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "project_instance_id": row["project_id"],
            "normalized_root": row["normalized_root"],
            "project_root": row["display_root"],
            "registered": bool(row["registered"]),
            "created_at": _utc_iso(row["created_at"]),
            "updated_at": _utc_iso(row["updated_at"]),
        }

    def project(self, project_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project instance {project_id!r} is not registered")
        return self._project_document(row)

    def project_for_root(self, root: str | Path) -> dict[str, Any]:
        normalized, _display = normalize_project_root(root)
        row = self._connection.execute(
            "SELECT * FROM projects WHERE normalized_root=? AND registered=1",
            (normalized,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project root is not registered: {root}")
        return self._project_document(row)

    def relocate_project(self, project_id: str, new_root: str | Path) -> dict[str, Any]:
        normalized, display = normalize_project_root(new_root)
        now = time.time()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT project_id FROM projects WHERE normalized_root=? AND registered=1",
                (normalized,),
            ).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                raise ConflictError(f"Project root is already registered: {display}")
            changed = connection.execute(
                """
                UPDATE projects SET normalized_root=?, display_root=?, updated_at=?
                WHERE project_id=? AND registered=1
                """,
                (normalized, display, now, project_id),
            ).rowcount
            if not changed:
                raise NotFoundError(f"Project instance {project_id!r} is not registered")
        return self.project(project_id)

    def list_projects(self, *, include_unregistered: bool = False) -> list[dict[str, Any]]:
        where = "" if include_unregistered else "WHERE registered=1"
        rows = self._connection.execute(
            f"SELECT * FROM projects {where} ORDER BY created_at"
        ).fetchall()
        return [self._project_document(row) for row in rows]

    def unregister_project(self, project_id: str, *, force: bool = False) -> int:
        active = [
            source
            for source in self.active_sources()
            if source["project_instance_id"] == project_id
        ]
        if active and not force:
            raise ConflictError(
                f"Project {project_id} has active sources: "
                + ", ".join(source["source_id"] for source in active)
            )
        bindings = self.list_bindings(project_id=project_id)
        cancelled = 0
        for binding in bindings:
            cancelled += self.remove_binding(binding["binding_id"])
        now = time.time()
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE projects
                SET normalized_root=?, registered=0, updated_at=?
                WHERE project_id=? AND registered=1
                """,
                (f"unregistered:{project_id}", now, project_id),
            ).rowcount
            if not changed:
                raise NotFoundError(
                    f"Registered project instance {project_id!r} does not exist"
                )
        return cancelled

    def ensure_binding(
        self,
        project_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            return self._ensure_binding(connection, project_id, session_id)

    def _ensure_binding(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        project = connection.execute(
            "SELECT project_id FROM projects WHERE project_id=? AND registered=1",
            (project_id,),
        ).fetchone()
        if project is None:
            raise NotFoundError(f"Project instance {project_id!r} is not registered")
        scope = "session" if session_id is not None else "project"
        normalized_session = _validate_session_id(session_id) if session_id is not None else ""
        row = connection.execute(
            """
            SELECT * FROM bindings
            WHERE project_id=? AND scope=? AND session_id=?
            """,
            (project_id, scope, normalized_session),
        ).fetchone()
        if row is None:
            now = time.time()
            binding_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO bindings(
                    binding_id, project_id, scope, session_id, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'dormant', ?, ?)
                """,
                (binding_id, project_id, scope, normalized_session, now, now),
            )
            row = connection.execute(
                "SELECT * FROM bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        elif row["state"] == "deleted":
            raise ConflictError(
                f"Binding {row['binding_id']} was removed and cannot be resurrected implicitly"
            )
        return self._binding_document(row)

    @staticmethod
    def _binding_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "binding_id": row["binding_id"],
            "project_instance_id": row["project_id"],
            "scope": row["scope"],
            "session_id": row["session_id"] or None,
            "state": row["state"],
            "effective_revision": row["effective_revision"],
            "candidate_revision": row["candidate_revision"],
            "current_activity": row["current_activity"],
        }

    def binding(self, binding_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM bindings WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Binding {binding_id!r} does not exist")
        return self._binding_document(row)

    def list_bindings(
        self,
        *,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        if not include_deleted:
            clauses.append("state <> 'deleted'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM bindings {where} ORDER BY created_at",
            parameters,
        ).fetchall()
        return [self._binding_document(row) for row in rows]

    def set_project_default(self, project_id: str, patch: Mapping[str, Any]) -> None:
        normalized = validate_patch(patch, path="project")
        now = time.time()
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND registered=1",
                (project_id,),
            ).fetchone() is None:
                raise NotFoundError(f"Project instance {project_id!r} is not registered")
            connection.execute(
                """
                INSERT INTO project_defaults(project_id, patch_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    patch_json=excluded.patch_json,
                    updated_at=excluded.updated_at
                """,
                (project_id, _canonical_json(normalized), now),
            )

    def project_default(self, project_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT patch_json FROM project_defaults WHERE project_id=?",
            (project_id,),
        ).fetchone()
        return _decode_json(row["patch_json"]) if row else {}

    def set_session_override(
        self,
        binding_id: str,
        patch: Mapping[str, Any],
    ) -> None:
        normalized = validate_patch(patch, path="session")
        binding = self.binding(binding_id)
        if binding["scope"] != "session":
            raise ValidationError("session overrides require a session binding")
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO session_overrides(binding_id, patch_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    patch_json=excluded.patch_json,
                    updated_at=excluded.updated_at
                """,
                (binding_id, _canonical_json(normalized), now),
            )

    def session_override(self, binding_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT patch_json FROM session_overrides WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        return _decode_json(row["patch_json"]) if row else {}

    def clear_session_override(self, binding_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM session_overrides WHERE binding_id=?",
                (binding_id,),
            )

    def register_source(
        self,
        *,
        adapter: str,
        project_root: str | Path,
        session_id: str | None,
        capabilities: list[str],
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(adapter, str) or not adapter.strip():
            raise ValidationError("adapter must be a non-empty string", path="adapter")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item for item in capabilities
        ):
            raise ValidationError("capabilities must be a string list", path="capabilities")
        normalized, display = normalize_project_root(project_root)
        timestamp = time.time() if now is None else now
        expires_at = timestamp + LEASE_EXPIRY_SECONDS
        lease_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        source_id = str(uuid.uuid4())
        with self.transaction() as connection:
            project = self._register_project(connection, normalized, display)
            binding = self._ensure_binding(
                connection,
                project["project_instance_id"],
                session_id,
            )
            connection.execute(
                """
                INSERT INTO sources(
                    source_id, adapter, project_id, binding_id, session_id,
                    capabilities_json, lease_token_hash, expires_at, connected,
                    created_at, last_seen
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    source_id,
                    adapter.strip(),
                    project["project_instance_id"],
                    binding["binding_id"],
                    binding["session_id"] or "",
                    _canonical_json(sorted(set(capabilities))),
                    token_hash,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE bindings SET state='active', updated_at=? WHERE binding_id=?",
                (timestamp, binding["binding_id"]),
            )
        return {
            "source_id": source_id,
            "project_instance_id": project["project_instance_id"],
            "binding_id": binding["binding_id"],
            "lease_token": lease_token,
            "expires_at": _utc_iso(expires_at),
            "lease_refresh_seconds": LEASE_REFRESH_SECONDS,
            "lease_expiry_seconds": LEASE_EXPIRY_SECONDS,
        }

    def refresh_lease(
        self,
        source_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        expires_at = timestamp + LEASE_EXPIRY_SECONDS
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT binding_id, lease_token_hash FROM sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None or not secrets.compare_digest(row["lease_token_hash"], token_hash):
                raise ValidationError("invalid source lease", path="lease_token")
            connection.execute(
                """
                UPDATE sources
                SET connected=1, expires_at=?, last_seen=?
                WHERE source_id=?
                """,
                (expires_at, timestamp, source_id),
            )
            connection.execute(
                "UPDATE bindings SET state='active', updated_at=? WHERE binding_id=? AND state<>'deleted'",
                (timestamp, row["binding_id"]),
            )
        return {"source_id": source_id, "expires_at": _utc_iso(expires_at)}

    def expire_leases(self, *, now: float | None = None) -> list[str]:
        timestamp = time.time() if now is None else now
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT source_id, binding_id FROM sources
                WHERE connected=1 AND expires_at <= ?
                """,
                (timestamp,),
            ).fetchall()
            source_ids = [row["source_id"] for row in rows]
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                connection.execute(
                    f"UPDATE sources SET connected=0 WHERE source_id IN ({placeholders})",
                    source_ids,
                )
            for binding_id in {row["binding_id"] for row in rows}:
                active = connection.execute(
                    """
                    SELECT 1 FROM sources
                    WHERE binding_id=? AND connected=1 AND expires_at>?
                    LIMIT 1
                    """,
                    (binding_id, timestamp),
                ).fetchone()
                if active is None:
                    connection.execute(
                        """
                        UPDATE bindings SET state='dormant', updated_at=?
                        WHERE binding_id=? AND state<>'deleted'
                        """,
                        (timestamp, binding_id),
                    )
        return source_ids

    def assert_source_active(
        self,
        source_id: str,
        *,
        binding_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        row = self._connection.execute(
            "SELECT * FROM sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if (
            row is None
            or not row["connected"]
            or row["expires_at"] <= timestamp
            or (binding_id is not None and row["binding_id"] != binding_id)
        ):
            raise ValidationError("source is unregistered, foreign, or expired")
        return {
            "source_id": row["source_id"],
            "project_instance_id": row["project_id"],
            "binding_id": row["binding_id"],
            "session_id": row["session_id"] or None,
            "adapter": row["adapter"],
            "capabilities": _decode_json(row["capabilities_json"]),
            "expires_at": _utc_iso(row["expires_at"]),
        }

    def active_sources(self) -> list[dict[str, Any]]:
        now = time.time()
        rows = self._connection.execute(
            """
            SELECT source_id FROM sources
            WHERE connected=1 AND expires_at>?
            ORDER BY created_at
            """,
            (now,),
        ).fetchall()
        return [self.assert_source_active(row["source_id"], now=now) for row in rows]

    def disconnect_source(self, source_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT binding_id FROM sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE sources SET connected=0, last_seen=? WHERE source_id=?",
                (timestamp, source_id),
            )
            active = connection.execute(
                """
                SELECT 1 FROM sources
                WHERE binding_id=? AND connected=1 AND expires_at>?
                LIMIT 1
                """,
                (row["binding_id"], timestamp),
            ).fetchone()
            if active is None:
                connection.execute(
                    """
                    UPDATE bindings SET state='dormant', updated_at=?
                    WHERE binding_id=? AND state<>'deleted'
                    """,
                    (timestamp, row["binding_id"]),
                )

    def next_revision(self, binding_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT MAX(revision) AS revision
            FROM effective_snapshots WHERE binding_id=?
            """,
            (binding_id,),
        ).fetchone()
        return int(row["revision"] or 0) + 1

    def stage_snapshot(
        self,
        snapshot: EffectiveSnapshot,
        *,
        require_voice: bool = True,
        require_renderer: bool = True,
    ) -> None:
        binding = self.binding(snapshot.binding_id)
        if binding["state"] == "deleted":
            raise ConflictError(f"Binding {snapshot.binding_id} was removed")
        expected = self.next_revision(snapshot.binding_id)
        if snapshot.revision != expected:
            raise ConflictError(
                f"Snapshot revision {snapshot.revision} is not next revision {expected}"
            )
        now = time.time()
        with self.transaction() as connection:
            if connection.execute(
                """
                SELECT 1 FROM effective_snapshots
                WHERE binding_id=? AND status='candidate'
                """,
                (snapshot.binding_id,),
            ).fetchone():
                raise ConflictError(f"Binding {snapshot.binding_id} already has a candidate")
            connection.execute(
                """
                INSERT INTO effective_snapshots(
                    binding_id, revision, snapshot_json, status,
                    requires_voice, requires_renderer, created_at
                ) VALUES(?, ?, ?, 'candidate', ?, ?, ?)
                """,
                (
                    snapshot.binding_id,
                    snapshot.revision,
                    _canonical_json(snapshot.to_document()),
                    int(require_voice),
                    int(require_renderer),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE bindings SET candidate_revision=?, updated_at=?
                WHERE binding_id=?
                """,
                (snapshot.revision, now, snapshot.binding_id),
            )

    def acknowledge_snapshot(
        self,
        binding_id: str,
        revision: int,
        consumer: str,
        *,
        promote: bool = True,
    ) -> bool:
        if consumer not in {"voice", "renderer"}:
            raise ValidationError("consumer must be voice or renderer")
        column = "voice_ack" if consumer == "voice" else "renderer_ack"
        now = time.time()
        with self.transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE effective_snapshots SET {column}=1
                WHERE binding_id=? AND revision=? AND status='candidate'
                """,
                (binding_id, revision),
            ).rowcount
            if not changed:
                raise NotFoundError(
                    f"Candidate snapshot {binding_id}@{revision} was not found"
                )
            row = connection.execute(
                """
                SELECT requires_voice, requires_renderer, voice_ack, renderer_ack
                FROM effective_snapshots
                WHERE binding_id=? AND revision=?
                """,
                (binding_id, revision),
            ).fetchone()
            ready = bool(
                (not row["requires_voice"] or row["voice_ack"])
                and (not row["requires_renderer"] or row["renderer_ack"])
            )
            if ready and promote:
                self._promote_candidate(connection, binding_id, revision, now=now)
        return ready

    def _promote_candidate(
        self,
        connection: sqlite3.Connection,
        binding_id: str,
        revision: int,
        *,
        now: float,
    ) -> EffectiveSnapshot:
        row = connection.execute(
            """
            SELECT * FROM effective_snapshots
            WHERE binding_id=? AND revision=? AND status='candidate'
            """,
            (binding_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Candidate snapshot {binding_id}@{revision} was not found"
            )
        ready = bool(
            (not row["requires_voice"] or row["voice_ack"])
            and (not row["requires_renderer"] or row["renderer_ack"])
        )
        if not ready:
            raise ConflictError(
                f"Candidate snapshot {binding_id}@{revision} lacks required acknowledgements"
            )
        connection.execute(
            """
            UPDATE effective_snapshots
            SET status='superseded'
            WHERE binding_id=? AND status='active'
            """,
            (binding_id,),
        )
        connection.execute(
            """
            UPDATE effective_snapshots
            SET status='active', acknowledged_at=?
            WHERE binding_id=? AND revision=?
            """,
            (now, binding_id, revision),
        )
        connection.execute(
            """
            UPDATE bindings
            SET effective_revision=?, candidate_revision=NULL, updated_at=?
            WHERE binding_id=?
            """,
            (revision, now, binding_id),
        )
        snapshot = self._snapshot_from_row(row)
        self._replace_binding_references(connection, snapshot)
        return snapshot

    def promote_candidates(
        self,
        candidates: Mapping[str, int],
        *,
        project_update: tuple[str, Mapping[str, Any]] | None = None,
        session_update: tuple[str, Mapping[str, Any] | None] | None = None,
    ) -> list[EffectiveSnapshot]:
        if not candidates:
            raise ValidationError("at least one candidate is required")
        normalized_project: tuple[str, dict[str, Any]] | None = None
        if project_update is not None:
            normalized_project = (
                project_update[0],
                validate_patch(project_update[1], path="project"),
            )
        normalized_session: tuple[str, dict[str, Any] | None] | None = None
        if session_update is not None:
            normalized_session = (
                session_update[0],
                (
                    validate_patch(session_update[1], path="session")
                    if session_update[1] is not None
                    else None
                ),
            )
        now = time.time()
        promoted: list[EffectiveSnapshot] = []
        with self.transaction() as connection:
            if normalized_project is not None:
                project_id, patch = normalized_project
                if connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=? AND registered=1",
                    (project_id,),
                ).fetchone() is None:
                    raise NotFoundError(
                        f"Project instance {project_id!r} is not registered"
                    )
                connection.execute(
                    """
                    INSERT INTO project_defaults(project_id, patch_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        patch_json=excluded.patch_json,
                        updated_at=excluded.updated_at
                    """,
                    (project_id, _canonical_json(patch), now),
                )
            if normalized_session is not None:
                binding_id, patch = normalized_session
                if patch is None:
                    connection.execute(
                        "DELETE FROM session_overrides WHERE binding_id=?",
                        (binding_id,),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO session_overrides(binding_id, patch_json, updated_at)
                        VALUES(?, ?, ?)
                        ON CONFLICT(binding_id) DO UPDATE SET
                            patch_json=excluded.patch_json,
                            updated_at=excluded.updated_at
                        """,
                        (binding_id, _canonical_json(patch), now),
                    )
            for binding_id, revision in candidates.items():
                promoted.append(
                    self._promote_candidate(
                        connection,
                        binding_id,
                        revision,
                        now=now,
                    )
                )
        return promoted

    def fail_snapshot(self, binding_id: str, revision: int, diagnostic: str) -> None:
        now = time.time()
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE effective_snapshots
                SET status='failed', diagnostic=?, acknowledged_at=?
                WHERE binding_id=? AND revision=? AND status='candidate'
                """,
                (diagnostic, now, binding_id, revision),
            ).rowcount
            if not changed:
                raise NotFoundError(
                    f"Candidate snapshot {binding_id}@{revision} was not found"
                )
            connection.execute(
                """
                UPDATE bindings SET candidate_revision=NULL, updated_at=?
                WHERE binding_id=?
                """,
                (now, binding_id),
            )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> EffectiveSnapshot:
        return EffectiveSnapshot.from_document(_decode_json(row["snapshot_json"]))

    def effective_snapshot(self, binding_id: str) -> EffectiveSnapshot | None:
        row = self._connection.execute(
            """
            SELECT snapshot_json FROM effective_snapshots
            WHERE binding_id=? AND status='active'
            """,
            (binding_id,),
        ).fetchone()
        return self._snapshot_from_row(row).acknowledged() if row else None

    def candidate_snapshot(self, binding_id: str) -> EffectiveSnapshot | None:
        row = self._connection.execute(
            """
            SELECT snapshot_json FROM effective_snapshots
            WHERE binding_id=? AND status='candidate'
            """,
            (binding_id,),
        ).fetchone()
        return self._snapshot_from_row(row) if row else None

    @staticmethod
    def _replace_binding_references(
        connection: sqlite3.Connection,
        snapshot: EffectiveSnapshot,
    ) -> None:
        connection.execute(
            "DELETE FROM catalog_references WHERE binding_id=?",
            (snapshot.binding_id,),
        )
        references = [
            ("avatar", snapshot.avatar_ref, "effective"),
        ]
        if snapshot.profile_ref:
            references.append(("profile", snapshot.profile_ref, "effective"))
        if snapshot.preset_ref:
            references.append(("preset", snapshot.preset_ref, "effective"))
        connection.executemany(
            """
            INSERT INTO catalog_references(kind, reference, binding_id, source)
            VALUES(?, ?, ?, ?)
            """,
            [
                (kind, reference, snapshot.binding_id, source)
                for kind, reference, source in references
            ],
        )

    def catalog_references(self, kind: str, reference: str) -> list[str]:
        identifier = reference.split("@", 1)[0]
        rows = self._connection.execute(
            """
            SELECT kind, reference, binding_id FROM catalog_references
            WHERE kind=?
            """,
            (kind,),
        ).fetchall()
        return [
            row["binding_id"]
            for row in rows
            if row["reference"] == reference
            or row["reference"].split("@", 1)[0] == identifier
        ]

    def set_geometry(self, binding_id: str, geometry: Mapping[str, Any]) -> None:
        allowed = {"x", "y", "width", "height", "display_id", "visible"}
        if set(geometry) - allowed:
            raise ValidationError("geometry contains unknown fields")
        for field in ("x", "y", "width", "height"):
            if field in geometry and (
                isinstance(geometry[field], bool)
                or not isinstance(geometry[field], (int, float))
            ):
                raise ValidationError(f"geometry.{field} must be numeric")
        self.binding(binding_id)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO renderer_geometry(binding_id, geometry_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    geometry_json=excluded.geometry_json,
                    updated_at=excluded.updated_at
                """,
                (binding_id, _canonical_json(dict(geometry)), time.time()),
            )

    def geometry(self, binding_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT geometry_json FROM renderer_geometry WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        return _decode_json(row["geometry_json"]) if row else None

    def record_event(
        self,
        *,
        event_id: str,
        source_id: str | None,
        binding_id: str,
        event_type: str,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        if not event_id or not event_type:
            raise ValidationError("event id and type must be non-empty")
        target = connection or self._connection
        changed = target.execute(
            """
            INSERT OR IGNORE INTO event_dedup(
                event_id, source_id, binding_id, event_type, created_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (event_id, source_id, binding_id, event_type, time.time()),
        ).rowcount
        return bool(changed)

    def enqueue_speech(
        self,
        *,
        source_id: str,
        binding_id: str,
        effective_revision: int,
        utterance_id: str,
        event_id: str,
        text: str,
        kind: str,
        tts: Mapping[str, Any],
    ) -> int | None:
        self.assert_source_active(source_id, binding_id=binding_id)
        binding = self.binding(binding_id)
        if binding["effective_revision"] != effective_revision:
            raise ConflictError(
                f"Binding {binding_id} is at revision {binding['effective_revision']}, "
                f"not {effective_revision}"
            )
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("speech text must be non-empty")
        if kind not in {"commentary", "final", "announcement"}:
            raise ValidationError("unsupported speech kind")
        now = time.time()
        with self.transaction() as connection:
            if not self.record_event(
                event_id=event_id,
                source_id=source_id,
                binding_id=binding_id,
                event_type=f"speech:{kind}",
                connection=connection,
            ):
                return None
            cursor = connection.execute(
                """
                INSERT INTO speech_queue(
                    binding_id, effective_revision, utterance_id, event_id,
                    text, kind, tts_json, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    binding_id,
                    effective_revision,
                    utterance_id,
                    event_id,
                    text,
                    kind,
                    _canonical_json(dict(tts)),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def claim_next_speech(self) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT q.* FROM speech_queue q
                JOIN bindings b ON b.binding_id=q.binding_id
                WHERE q.status='queued' AND b.state='active'
                ORDER BY q.queue_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE speech_queue SET status='claimed', updated_at=? WHERE queue_id=?",
                (time.time(), row["queue_id"]),
            )
            return self._speech_document(row, status="claimed")

    @staticmethod
    def _speech_document(row: sqlite3.Row, *, status: str | None = None) -> dict[str, Any]:
        return {
            "queue_id": row["queue_id"],
            "binding_id": row["binding_id"],
            "effective_revision": row["effective_revision"],
            "utterance_id": row["utterance_id"],
            "event_id": row["event_id"],
            "text": row["text"],
            "kind": row["kind"],
            "tts": _decode_json(row["tts_json"]),
            "status": status or row["status"],
            "cancel_reason": row["cancel_reason"],
        }

    def speech_items(self, *, binding_id: str | None = None) -> list[dict[str, Any]]:
        if binding_id is None:
            rows = self._connection.execute(
                "SELECT * FROM speech_queue ORDER BY queue_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM speech_queue WHERE binding_id=? ORDER BY queue_id",
                (binding_id,),
            ).fetchall()
        return [self._speech_document(row) for row in rows]

    def speech_item(self, queue_id: int) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM speech_queue WHERE queue_id=?", (queue_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Speech queue item {queue_id} does not exist")
        return self._speech_document(row)

    def speech_statuses(
        self, binding_id: str, event_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not event_ids:
            return {}
        if len(event_ids) > 1024 or any(
            not isinstance(item, str) or not item for item in event_ids
        ):
            raise ValidationError("speech status event_ids must be a bounded string list")
        placeholders = ",".join("?" for _ in event_ids)
        rows = self._connection.execute(
            f"""
            SELECT * FROM speech_queue
            WHERE binding_id=? AND event_id IN ({placeholders})
            """,
            (binding_id, *event_ids),
        ).fetchall()
        return {row["event_id"]: self._speech_document(row) for row in rows}

    def update_speech_status(
        self,
        queue_id: int,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        allowed = {"claimed", "playing", "paused", "finished", "cancelled", "failed"}
        if status not in allowed:
            raise ValidationError(f"unsupported speech status: {status}")
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE speech_queue
                SET status=?, cancel_reason=?, updated_at=?
                WHERE queue_id=?
                """,
                (status, reason, time.time(), queue_id),
            ).rowcount
            if not changed:
                raise NotFoundError(f"Speech queue item {queue_id} does not exist")

    def transition_speech_status(
        self,
        queue_id: int,
        status: str,
        *,
        from_statuses: tuple[str, ...],
        reason: str | None = None,
    ) -> bool:
        allowed = {"claimed", "playing", "paused", "finished", "cancelled", "failed"}
        if status not in allowed or not from_statuses or any(
            item not in allowed | {"queued"} for item in from_statuses
        ):
            raise ValidationError("speech transition contains an unsupported status")
        placeholders = ",".join("?" for _ in from_statuses)
        with self.transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE speech_queue
                SET status=?, cancel_reason=?, updated_at=?
                WHERE queue_id=? AND status IN ({placeholders})
                """,
                (status, reason, time.time(), queue_id, *from_statuses),
            ).rowcount
        return bool(changed)

    def cancel_speech_events(self, binding_id: str, event_ids: list[str]) -> int:
        if not event_ids or any(not isinstance(item, str) or not item for item in event_ids):
            raise ValidationError("speech cancellation requires non-empty event ids")
        with self.transaction() as connection:
            placeholders = ",".join("?" for _ in event_ids)
            changed = connection.execute(
                f"""
                UPDATE speech_queue
                SET status='cancelled', cancel_reason='source stream cancelled', updated_at=?
                WHERE binding_id=? AND event_id IN ({placeholders})
                  AND status IN ('queued', 'claimed', 'playing', 'paused')
                """,
                (time.time(), binding_id, *event_ids),
            ).rowcount
        return int(changed)

    def remove_binding(self, binding_id: str) -> int:
        now = time.time()
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE bindings
                SET state='deleted', candidate_revision=NULL, updated_at=?
                WHERE binding_id=? AND state<>'deleted'
                """,
                (now, binding_id),
            ).rowcount
            if not changed:
                raise NotFoundError(f"Active binding {binding_id!r} does not exist")
            connection.execute(
                "UPDATE sources SET connected=0 WHERE binding_id=?",
                (binding_id,),
            )
            connection.execute(
                "DELETE FROM catalog_references WHERE binding_id=?",
                (binding_id,),
            )
            cancelled = connection.execute(
                """
                UPDATE speech_queue
                SET status='cancelled',
                    cancel_reason='binding removed',
                    updated_at=?
                WHERE binding_id=? AND status IN ('queued', 'claimed', 'playing', 'paused')
                """,
                (now, binding_id),
            ).rowcount
            connection.execute(
                """
                UPDATE input_transcripts
                SET status='failed', diagnostic='binding removed', updated_at=?
                WHERE binding_id=? AND status IN ('transcribing', 'ready')
                """,
                (now, binding_id),
            )
        return int(cancelled)

    def set_activity(
        self,
        *,
        source_id: str,
        binding_id: str,
        event_id: str,
        activity: str,
    ) -> bool:
        allowed = {"idle", "thinking", "tool", "skill", "cli", "waiting", "error"}
        if activity not in allowed:
            raise ValidationError(f"unsupported activity: {activity}")
        self.assert_source_active(source_id, binding_id=binding_id)
        with self.transaction() as connection:
            if not self.record_event(
                event_id=event_id,
                source_id=source_id,
                binding_id=binding_id,
                event_type=f"activity:{activity}",
                connection=connection,
            ):
                return False
            connection.execute(
                """
                UPDATE bindings SET current_activity=?, updated_at=?
                WHERE binding_id=?
                """,
                (activity, time.time(), binding_id),
            )
        return True

    def focus_binding(self, binding_id: str) -> None:
        binding = self.binding(binding_id)
        if binding["state"] == "deleted":
            raise ConflictError(f"Binding {binding_id} was removed")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE attention_state
                SET binding_id=?, state='focused', updated_at=?
                WHERE singleton=1
                """,
                (binding_id, time.time()),
            )

    def attention(self) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT binding_id, utterance_id, state, updated_at FROM attention_state WHERE singleton=1"
        ).fetchone()
        return {
            "binding_id": row["binding_id"],
            "utterance_id": row["utterance_id"],
            "state": row["state"],
            "updated_at": _utc_iso(row["updated_at"]),
        }

    def begin_playback(self, binding_id: str, utterance_id: str) -> None:
        binding = self.binding(binding_id)
        if binding["state"] == "deleted":
            raise ConflictError(f"Binding {binding_id} was removed")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE attention_state
                SET binding_id=?, utterance_id=?, state='speaking', updated_at=?
                WHERE singleton=1
                """,
                (binding_id, utterance_id, time.time()),
            )

    def set_playback_attention(self, binding_id: str, state: str) -> dict[str, Any]:
        if state not in {"paused", "speaking"}:
            raise ValidationError("playback attention state must be paused or speaking")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT binding_id, utterance_id, state FROM attention_state WHERE singleton=1"
            ).fetchone()
            if row["binding_id"] != binding_id or row["state"] not in {"speaking", "paused"}:
                return {
                    "changed": False,
                    "binding_id": row["binding_id"],
                    "utterance_id": row["utterance_id"],
                    "state": row["state"],
                }
            connection.execute(
                "UPDATE attention_state SET state=?, updated_at=? WHERE singleton=1",
                (state, time.time()),
            )
        return {"changed": True, **self.attention()}

    def finish_playback(self, binding_id: str, utterance_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE attention_state
                SET binding_id=NULL, utterance_id=NULL, state='idle', updated_at=?
                WHERE singleton=1 AND binding_id=? AND utterance_id=?
                """,
                (time.time(), binding_id, utterance_id),
            )

    def begin_input(self, binding_id: str, capture_id: str) -> str:
        binding = self.binding(binding_id)
        if binding["scope"] != "session" or binding["state"] == "deleted":
            raise ValidationError("voice input requires an active session binding")
        input_id = str(uuid.uuid4())
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO input_transcripts(
                    input_id, binding_id, capture_id, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'transcribing', ?, ?)
                """,
                (input_id, binding_id, capture_id, now, now),
            )
        return input_id

    def finish_input(
        self,
        input_id: str,
        *,
        transcript: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        if transcript is not None and (not isinstance(transcript, str) or not transcript.strip()):
            raise ValidationError("voice input transcript must be non-empty")
        status = "ready" if transcript is not None else "failed"
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE input_transcripts
                SET transcript=?, status=?, diagnostic=?, updated_at=?
                WHERE input_id=? AND status='transcribing'
                """,
                (transcript.strip() if transcript else None, status, diagnostic, time.time(), input_id),
            ).rowcount
            if not changed:
                raise NotFoundError(f"Active voice input {input_id!r} was not found")

    def pending_inputs(self, binding_id: str) -> list[dict[str, Any]]:
        self.binding(binding_id)
        rows = self._connection.execute(
            """
            SELECT input_id, capture_id, transcript, created_at
            FROM input_transcripts
            WHERE binding_id=? AND status='ready'
            ORDER BY created_at
            """,
            (binding_id,),
        ).fetchall()
        return [
            {
                "input_id": row["input_id"],
                "capture_id": row["capture_id"],
                "transcript": row["transcript"],
                "created_at": _utc_iso(row["created_at"]),
            }
            for row in rows
        ]

    def acknowledge_input(self, binding_id: str, input_id: str) -> dict[str, str]:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT capture_id FROM input_transcripts
                WHERE binding_id=? AND input_id=? AND status='ready'
                """,
                (binding_id, input_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Ready voice input {input_id!r} was not found")
            changed = connection.execute(
                """
                UPDATE input_transcripts SET status='delivered', updated_at=?
                WHERE binding_id=? AND input_id=? AND status='ready'
                """,
                (time.time(), binding_id, input_id),
            ).rowcount
            if not changed:
                raise NotFoundError(f"Ready voice input {input_id!r} was not found")
        return {
            "binding_id": binding_id,
            "input_id": input_id,
            "capture_id": row["capture_id"],
        }

    def begin_configuration_transaction(
        self,
        *,
        scope: str,
        target_id: str,
        previous: Mapping[str, Any] | None,
        candidate: Mapping[str, Any],
    ) -> str:
        if scope not in {"project", "session", "activity", "migration"}:
            raise ValidationError(f"unsupported transaction scope: {scope}")
        transaction_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO configuration_transactions(
                    transaction_id, scope, target_id, previous_json,
                    candidate_json, status, created_at
                ) VALUES(?, ?, ?, ?, ?, 'staged', ?)
                """,
                (
                    transaction_id,
                    scope,
                    target_id,
                    _canonical_json(previous) if previous is not None else None,
                    _canonical_json(candidate),
                    time.time(),
                ),
            )
        return transaction_id

    def finish_configuration_transaction(
        self,
        transaction_id: str,
        *,
        status: str,
        diagnostic: str | None = None,
    ) -> None:
        if status not in {"committed", "failed", "rolled_back"}:
            raise ValidationError(f"unsupported transaction status: {status}")
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE configuration_transactions
                SET status=?, diagnostic=?, completed_at=?
                WHERE transaction_id=? AND status='staged'
                """,
                (status, diagnostic, time.time(), transaction_id),
            ).rowcount
            if not changed:
                raise NotFoundError(
                    f"Staged configuration transaction {transaction_id!r} was not found"
                )

    def migration_record(self, project_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM migration_ledger WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "project_instance_id": row["project_id"],
            "source_hash": row["source_hash"],
            "status": row["status"],
            "details": _decode_json(row["details_json"]),
            "started_at": _utc_iso(row["started_at"]),
            "completed_at": _utc_iso(row["completed_at"]) if row["completed_at"] else None,
        }

    def set_migration_record(
        self,
        *,
        project_id: str,
        source_hash: str,
        status: str,
        details: Mapping[str, Any],
    ) -> None:
        if status not in {"pending", "committed", "failed", "rolled_back"}:
            raise ValidationError(f"unsupported migration status: {status}")
        now = time.time()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT started_at FROM migration_ledger WHERE project_id=?",
                (project_id,),
            ).fetchone()
            started = existing["started_at"] if existing else now
            completed = now if status != "pending" else None
            connection.execute(
                """
                INSERT INTO migration_ledger(
                    project_id, source_hash, status, details_json, started_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    source_hash=excluded.source_hash,
                    status=excluded.status,
                    details_json=excluded.details_json,
                    completed_at=excluded.completed_at
                """,
                (
                    project_id,
                    source_hash,
                    status,
                    _canonical_json(dict(details)),
                    started,
                    completed,
                ),
            )

    def import_legacy_configuration(
        self,
        *,
        project_id: str,
        source_hash: str,
        project_patch: Mapping[str, Any],
        session_patches: Mapping[str, Mapping[str, Any]],
        geometry: Mapping[str, Mapping[str, Any]],
        speech: list[Mapping[str, Any]],
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically persist one fully validated v0.1 import candidate.

        Consumer acknowledgement happens after this database transaction.  The
        returned checkpoint lets the migrator restore the exact prior sparse
        configuration if either consumer rejects the candidate.
        """

        normalized_project = validate_patch(project_patch, path="migration.project")
        normalized_sessions = {
            _validate_session_id(session_id): validate_patch(
                patch, path=f"migration.sessions.{session_id}"
            )
            for session_id, patch in session_patches.items()
        }
        for session_id, value in geometry.items():
            _validate_session_id(session_id)
            if set(value) - {"x", "y", "width", "height", "display_id", "visible"}:
                raise ValidationError(
                    f"migration geometry for {session_id} contains unknown fields"
                )
        now = time.time()
        checkpoint: dict[str, Any] = {}
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND registered=1",
                (project_id,),
            ).fetchone() is None:
                raise NotFoundError(f"Project instance {project_id!r} is not registered")
            prior_default = connection.execute(
                "SELECT patch_json FROM project_defaults WHERE project_id=?",
                (project_id,),
            ).fetchone()
            prior_bindings = connection.execute(
                "SELECT * FROM bindings WHERE project_id=? AND state<>'deleted'",
                (project_id,),
            ).fetchall()
            checkpoint = {
                "project_default": (
                    _decode_json(prior_default["patch_json"]) if prior_default else None
                ),
                "binding_ids": [row["binding_id"] for row in prior_bindings],
                "session_overrides": {},
                "geometry": {},
                "imported_event_ids": [],
            }
            for binding in prior_bindings:
                override = connection.execute(
                    "SELECT patch_json FROM session_overrides WHERE binding_id=?",
                    (binding["binding_id"],),
                ).fetchone()
                position = connection.execute(
                    "SELECT geometry_json FROM renderer_geometry WHERE binding_id=?",
                    (binding["binding_id"],),
                ).fetchone()
                checkpoint["session_overrides"][binding["binding_id"]] = (
                    _decode_json(override["patch_json"]) if override else None
                )
                checkpoint["geometry"][binding["binding_id"]] = (
                    _decode_json(position["geometry_json"]) if position else None
                )

            connection.execute(
                """
                INSERT INTO project_defaults(project_id, patch_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    patch_json=excluded.patch_json,
                    updated_at=excluded.updated_at
                """,
                (project_id, _canonical_json(normalized_project), now),
            )
            binding_by_session: dict[str, str] = {}
            for session_id, patch in normalized_sessions.items():
                binding = self._ensure_binding(connection, project_id, session_id)
                binding_id = binding["binding_id"]
                binding_by_session[session_id] = binding_id
                connection.execute(
                    """
                    INSERT INTO session_overrides(binding_id, patch_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        patch_json=excluded.patch_json,
                        updated_at=excluded.updated_at
                    """,
                    (binding_id, _canonical_json(patch), now),
                )
            for session_id, position in geometry.items():
                binding_id = binding_by_session.get(session_id)
                if binding_id is None:
                    binding = self._ensure_binding(connection, project_id, session_id)
                    binding_id = binding["binding_id"]
                    binding_by_session[session_id] = binding_id
                connection.execute(
                    """
                    INSERT INTO renderer_geometry(binding_id, geometry_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        geometry_json=excluded.geometry_json,
                        updated_at=excluded.updated_at
                    """,
                    (binding_id, _canonical_json(dict(position)), now),
                )
            project_binding = self._ensure_binding(connection, project_id, None)["binding_id"]
            imported = 0
            for item in speech:
                session_id = item.get("session_id")
                if session_id is None:
                    binding_id = project_binding
                else:
                    normalized_session = _validate_session_id(str(session_id))
                    binding_id = binding_by_session.get(normalized_session)
                    if binding_id is None:
                        binding_id = self._ensure_binding(
                            connection, project_id, normalized_session
                        )["binding_id"]
                        binding_by_session[normalized_session] = binding_id
                event_id = str(item.get("event_id") or "")
                utterance_id = str(item.get("utterance_id") or "")
                text = item.get("text")
                kind = item.get("kind")
                tts = item.get("tts")
                if not event_id or not utterance_id or not isinstance(text, str) or not text.strip():
                    raise ValidationError("legacy speech item is incomplete")
                if kind not in {"final", "announcement"} or not isinstance(tts, Mapping):
                    raise ValidationError("legacy speech item has unsupported kind or TTS data")
                if not self.record_event(
                    event_id=event_id,
                    source_id=None,
                    binding_id=binding_id,
                    event_type="speech:migrated",
                    connection=connection,
                ):
                    continue
                connection.execute(
                    """
                    INSERT INTO speech_queue(
                        binding_id, effective_revision, utterance_id, event_id,
                        text, kind, tts_json, status, created_at, updated_at
                    ) VALUES(?, 0, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        binding_id,
                        utterance_id,
                        event_id,
                        text,
                        kind,
                        _canonical_json(dict(tts)),
                        now,
                        now,
                    ),
                )
                checkpoint["imported_event_ids"].append(event_id)
                imported += 1
            connection.execute(
                """
                INSERT INTO migration_ledger(
                    project_id, source_hash, status, details_json, started_at, completed_at
                ) VALUES(?, ?, 'pending', ?, ?, NULL)
                ON CONFLICT(project_id) DO UPDATE SET
                    source_hash=excluded.source_hash,
                    status='pending',
                    details_json=excluded.details_json,
                    started_at=excluded.started_at,
                    completed_at=NULL
                """,
                (
                    project_id,
                    source_hash,
                    _canonical_json({**dict(details), "checkpoint": checkpoint}),
                    now,
                ),
            )
        return {
            "checkpoint": checkpoint,
            "bindings": binding_by_session,
            "project_binding_id": project_binding,
            "imported_speech": imported,
        }

    def finalize_migrated_speech(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self.transaction() as connection:
            for event_id in event_ids:
                connection.execute(
                    """
                    UPDATE speech_queue
                    SET effective_revision=(
                        SELECT effective_revision FROM bindings
                        WHERE bindings.binding_id=speech_queue.binding_id
                    ), updated_at=?
                    WHERE event_id=?
                    """,
                    (time.time(), event_id),
                )

    def rollback_legacy_configuration(
        self,
        *,
        project_id: str,
        checkpoint: Mapping[str, Any],
    ) -> None:
        """Restore the sparse state captured by import_legacy_configuration."""

        now = time.time()
        prior_ids = set(checkpoint.get("binding_ids", ()))
        with self.transaction() as connection:
            # A process can exit while a consumer is acknowledging a staged
            # snapshot.  Rollback must release that candidate before retry can
            # stage the next revision for the same binding.
            connection.execute(
                """
                UPDATE effective_snapshots
                SET status='failed',
                    diagnostic=COALESCE(diagnostic, 'migration interrupted'),
                    acknowledged_at=?
                WHERE status='candidate' AND binding_id IN (
                    SELECT binding_id FROM bindings WHERE project_id=?
                )
                """,
                (now, project_id),
            )
            connection.execute(
                """
                UPDATE bindings SET candidate_revision=NULL, updated_at=?
                WHERE project_id=? AND candidate_revision IS NOT NULL
                """,
                (now, project_id),
            )
            previous_default = checkpoint.get("project_default")
            if previous_default is None:
                connection.execute(
                    "DELETE FROM project_defaults WHERE project_id=?", (project_id,)
                )
            else:
                connection.execute(
                    """
                    INSERT INTO project_defaults(project_id, patch_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        patch_json=excluded.patch_json, updated_at=excluded.updated_at
                    """,
                    (project_id, _canonical_json(previous_default), now),
                )
            for binding_id, previous in checkpoint.get("session_overrides", {}).items():
                if previous is None:
                    connection.execute(
                        "DELETE FROM session_overrides WHERE binding_id=?", (binding_id,)
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO session_overrides(binding_id, patch_json, updated_at)
                        VALUES(?, ?, ?)
                        ON CONFLICT(binding_id) DO UPDATE SET
                            patch_json=excluded.patch_json, updated_at=excluded.updated_at
                        """,
                        (binding_id, _canonical_json(previous), now),
                    )
            for binding_id, previous in checkpoint.get("geometry", {}).items():
                if previous is None:
                    connection.execute(
                        "DELETE FROM renderer_geometry WHERE binding_id=?", (binding_id,)
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO renderer_geometry(binding_id, geometry_json, updated_at)
                        VALUES(?, ?, ?)
                        ON CONFLICT(binding_id) DO UPDATE SET
                            geometry_json=excluded.geometry_json, updated_at=excluded.updated_at
                        """,
                        (binding_id, _canonical_json(previous), now),
                    )
            for event_id in checkpoint.get("imported_event_ids", ()):
                connection.execute("DELETE FROM speech_queue WHERE event_id=?", (event_id,))
                connection.execute("DELETE FROM event_dedup WHERE event_id=?", (event_id,))
            current = connection.execute(
                "SELECT binding_id FROM bindings WHERE project_id=? AND state<>'deleted'",
                (project_id,),
            ).fetchall()
            for row in current:
                binding_id = row["binding_id"]
                if binding_id not in prior_ids:
                    connection.execute(
                        "DELETE FROM session_overrides WHERE binding_id=?", (binding_id,)
                    )
                    connection.execute(
                        "DELETE FROM renderer_geometry WHERE binding_id=?", (binding_id,)
                    )
                    has_source = connection.execute(
                        "SELECT 1 FROM sources WHERE binding_id=? LIMIT 1", (binding_id,)
                    ).fetchone()
                    if has_source is None:
                        # This binding did not exist before migration and has
                        # no live source. Remove it transactionally so retry can
                        # create the same logical project/session cleanly.
                        connection.execute(
                            "DELETE FROM speech_queue WHERE binding_id=?", (binding_id,)
                        )
                        connection.execute(
                            "DELETE FROM event_dedup WHERE binding_id=?", (binding_id,)
                        )
                        connection.execute(
                            "DELETE FROM bindings WHERE binding_id=?", (binding_id,)
                        )
