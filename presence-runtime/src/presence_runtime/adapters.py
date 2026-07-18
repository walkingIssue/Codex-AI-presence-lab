"""Lifecycle for thin project adapters owned by the user Presence Runtime."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .errors import ConflictError, ValidationError
from .paths import adapter_code_path, codex_home, runtime_python


MANAGED_SCHEMA = "presence/project-adapter/v0.2"
MANAGED_NAMES = {
    ".gitignore",
    "adapter.log",
    "adapter.lock",
    "adapter.pid",
    "managed.json",
    "rollout-cursors.json",
    "rollout-cursors.tmp",
}


@dataclass
class AdapterProcess:
    project_id: str
    project_root: Path
    state_root: Path
    process: subprocess.Popen[str]
    log_handle: IO[str]
    started_at: float


class ProjectAdapterManager:
    """Supervise one cursor-only rollout source for every registered project."""

    def __init__(
        self,
        store: Any,
        *,
        python: Path | None = None,
        script: Path | None = None,
    ) -> None:
        self.store = store
        self.python = (python or runtime_python()).resolve()
        self.script = (
            script or (adapter_code_path() / "codex" / "rollout_adapter.py")
        ).resolve()
        self._processes: dict[str, AdapterProcess] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._restart_after: dict[str, float] = {}

    @staticmethod
    def state_root(project_root: str | Path) -> Path:
        return Path(project_root).expanduser().resolve() / ".codex-voice" / "v0.2"

    @staticmethod
    def _atomic_json(path: Path, document: dict[str, Any]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _prepare(self, project: dict[str, Any]) -> Path:
        project_root = Path(project["project_root"]).expanduser().resolve()
        if not project_root.is_dir():
            raise ValidationError(f"registered project root is not a directory: {project_root}")
        state_root = self.state_root(project_root)
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
        self._atomic_json(
            state_root / "managed.json",
            {
                "schema": MANAGED_SCHEMA,
                "project_instance_id": project["project_instance_id"],
                "project_root": str(project_root),
                "managed_files": sorted(MANAGED_NAMES),
            },
        )
        return state_root

    @staticmethod
    def _tail(path: Path, limit: int = 12) -> list[str]:
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            return []

    def start_project(self, project: dict[str, Any]) -> dict[str, Any]:
        project_id = str(project["project_instance_id"])
        with self._lock:
            current = self._processes.get(project_id)
            if current is not None and current.process.poll() is None:
                return self.status(project_id)
            if current is not None:
                self._release(current)
                self._processes.pop(project_id, None)
            if not self.python.is_file():
                raise ConflictError(f"managed Presence Python is missing: {self.python}")
            if not self.script.is_file():
                raise ConflictError(f"managed Codex rollout adapter is missing: {self.script}")
            state_root = self._prepare(project)
            log_path = state_root / "adapter.log"
            log_handle = log_path.open("a", encoding="utf-8")
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                [
                    str(self.python),
                    str(self.script),
                    "--project-root",
                    str(Path(project["project_root"]).resolve()),
                    "--state-root",
                    str(state_root),
                    "--parent-pid",
                    str(os.getpid()),
                ],
                cwd=str(project["project_root"]),
                env={**os.environ, "CODEX_HOME": str(codex_home())},
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
            )
            managed = AdapterProcess(
                project_id=project_id,
                project_root=Path(project["project_root"]).resolve(),
                state_root=state_root,
                process=process,
                log_handle=log_handle,
                started_at=time.time(),
            )
            self._processes[project_id] = managed
        time.sleep(0.05)
        if process.poll() is not None:
            with self._lock:
                self._processes.pop(project_id, None)
                self._release(managed)
            detail = " | ".join(self._tail(log_path)) or f"exit code {process.returncode}"
            raise ConflictError(f"Codex rollout adapter failed to start: {detail}")
        return self.status(project_id)

    @staticmethod
    def _release(managed: AdapterProcess) -> None:
        try:
            managed.log_handle.close()
        except OSError:
            pass

    def stop_project(
        self,
        project_id: str,
        *,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            managed = self._processes.pop(project_id, None)
        if managed is not None:
            if managed.process.poll() is None:
                managed.process.terminate()
                try:
                    managed.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    managed.process.kill()
                    managed.process.wait(timeout=2)
            self._release(managed)
            project_root = managed.project_root
        else:
            try:
                project_root = Path(self.store.project(project_id)["project_root"])
            except BaseException:
                project_root = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            active = [
                source
                for source in self.store.active_sources()
                if source["project_instance_id"] == project_id
                and source["adapter"] == "codex-rollout-v0.2"
            ]
            if not active:
                break
            time.sleep(0.05)
        if cleanup and project_root is not None:
            self.cleanup_project_files(project_root, project_id=project_id)
        return {"project_instance_id": project_id, "stopped": True, "cleaned": cleanup}

    @classmethod
    def cleanup_project_files(
        cls,
        project_root: str | Path,
        *,
        project_id: str | None = None,
    ) -> bool:
        state_root = cls.state_root(project_root)
        manifest = state_root / "managed.json"
        if not manifest.is_file():
            return False
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConflictError(f"project adapter manifest is unreadable: {manifest}") from exc
        if document.get("schema") != MANAGED_SCHEMA:
            raise ConflictError(f"project adapter manifest has unknown ownership: {manifest}")
        if project_id is not None and document.get("project_instance_id") != project_id:
            raise ConflictError(f"project adapter manifest belongs to another project: {manifest}")
        for name in MANAGED_NAMES - {"managed.json"}:
            (state_root / name).unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        try:
            state_root.rmdir()
        except OSError:
            # Unknown files are user-owned and intentionally prevent directory removal.
            pass
        return True

    def status(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            managed = self._processes.get(project_id)
            running = managed is not None and managed.process.poll() is None
            if managed is not None:
                state_root = managed.state_root
                pid = managed.process.pid
            else:
                project = self.store.project(project_id)
                state_root = self.state_root(project["project_root"])
                pid = None
        sources = [
            source
            for source in self.store.active_sources()
            if source["project_instance_id"] == project_id
            and source["adapter"] == "codex-rollout-v0.2"
        ]
        return {
            "project_instance_id": project_id,
            "running": running,
            "pid": pid,
            "state_root": str(state_root),
            "active_sources": len(sources),
            "diagnostic_tail": self._tail(state_root / "adapter.log"),
        }

    def start_all(self) -> None:
        for project in self.store.list_projects():
            try:
                self.start_project(project)
            except BaseException as exc:
                print(
                    f"presence adapter start failed project={project['project_instance_id']}: {exc}",
                    flush=True,
                )

    def start_monitor(self) -> None:
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._stop.clear()
        self.start_all()
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="presence-project-adapters",
            daemon=True,
        )
        self._monitor.start()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(2.0):
            projects = {
                project["project_instance_id"]: project
                for project in self.store.list_projects()
            }
            with self._lock:
                known = set(self._processes)
            for project_id in known - set(projects):
                self.stop_project(project_id)
            for project_id, project in projects.items():
                with self._lock:
                    managed = self._processes.get(project_id)
                    running = managed is not None and managed.process.poll() is None
                    if managed is not None and not running:
                        self._processes.pop(project_id, None)
                        self._release(managed)
                        self._restart_after[project_id] = time.monotonic() + 2.0
                if running or time.monotonic() < self._restart_after.get(project_id, 0.0):
                    continue
                try:
                    self.start_project(project)
                    self._restart_after.pop(project_id, None)
                except BaseException as exc:
                    self._restart_after[project_id] = time.monotonic() + 5.0
                    print(f"presence adapter restart failed project={project_id}: {exc}", flush=True)

    def close(self) -> None:
        self._stop.set()
        monitor = self._monitor
        self._monitor = None
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=3)
        with self._lock:
            project_ids = tuple(self._processes)
        for project_id in project_ids:
            self.stop_project(project_id)
