"""Install and remove the manifest-owned user-level Presence Runtime."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable

from .errors import ConflictError, ValidationError
from .managed import (
    INSTALLATION_SCHEMA,
    atomic_json,
    file_inventory,
    read_installation,
    sha256_file,
    verify_managed_file,
)
from .paths import (
    adapter_code_path,
    bin_path,
    catalog_path,
    installation_path,
    live2d_code_path,
    presence_home,
    provider_python,
    renderer_host_path,
    runtime_code_path,
    runtime_python,
    state_database_path,
    worker_path,
)


MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.int8.onnx"
)
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)
DIRECTML_KOKORO_URL = (
    "git+https://github.com/walkingIssue/kokoro-onnx-intel-arc.git@intel-arc-directml"
)
OPENVINO_KOKORO_URL = (
    "git+https://github.com/walkingIssue/kokoro-onnx-intel-arc.git@main"
)
SUPPORTED_PROVIDERS = {"cpu", "cuda", "directml", "openvino"}
MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 13)


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConflictError(f"Command failed ({' '.join(command)}): {detail}")


def _python_version(command: list[str]) -> tuple[int, int] | None:
    result = subprocess.run(
        [*command, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    try:
        major, minor = result.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except ValueError:
        return None


def _select_python(requested: Path | None) -> list[str]:
    candidates: list[list[str]] = []
    if requested is not None:
        candidates.append([str(requested.expanduser().resolve())])
    else:
        candidates.append([sys.executable])
        launcher = shutil.which("py") if os.name == "nt" else None
        if launcher:
            candidates.extend([[launcher, "-3.12"], [launcher, "-3.11"]])
        elif shutil.which("python3.12"):
            candidates.append([str(shutil.which("python3.12"))])
        elif shutil.which("python3.11"):
            candidates.append([str(shutil.which("python3.11"))])
    rejected: list[str] = []
    for candidate in candidates:
        version = _python_version(candidate)
        if version is not None and MIN_PYTHON <= version < MAX_PYTHON:
            return candidate
        rejected.append(f"{' '.join(candidate)}={version or 'unavailable'}")
    raise ConflictError(
        "Presence requires Python 3.11 or 3.12; rejected " + ", ".join(rejected)
    )


def _environment_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _ensure_environment(root: Path, base_python: list[str]) -> Path:
    candidate = _environment_python(root)
    version = _python_version([str(candidate)]) if candidate.is_file() else None
    if version is not None and MIN_PYTHON <= version < MAX_PYTHON:
        return candidate
    if root.exists():
        _safe_rmtree(root)
    _run([*base_python, "-m", "venv", str(root)])
    if not candidate.is_file():
        raise ConflictError(f"Could not create managed environment: {root}")
    return candidate


def _source_layout(source: Path | None) -> dict[str, Path]:
    candidates: list[Path] = []
    if source is not None:
        candidates.append(source.expanduser().resolve())
    package_root = Path(__file__).resolve().parents[2]
    candidates.extend([package_root.parent, package_root])
    for candidate in candidates:
        if (
            (candidate / "presence-runtime" / "pyproject.toml").is_file()
            and (candidate / "live2d-avatar-runtime" / "pyproject.toml").is_file()
            and (candidate / "scripts" / "speak.py").is_file()
        ):
            repo = candidate
            presence = candidate / "presence-runtime"
            live2d = candidate / "live2d-avatar-runtime"
            skill = candidate
        elif (candidate / "presence-runtime" / "pyproject.toml").is_file():
            repo = candidate
            presence = candidate / "presence-runtime"
            live2d = candidate / "live2d-avatar-runtime"
            skill = candidate / "skills" / "codex-voice"
        elif candidate.name == "presence-runtime" and (candidate / "pyproject.toml").is_file():
            presence = candidate
            repo = candidate.parent
            live2d = repo / "live2d-avatar-runtime"
            skill = repo / "skills" / "codex-voice"
            if not skill.is_dir():
                skill = repo
                live2d = repo / "live2d-avatar-runtime"
        else:
            continue
        required = [
            presence / "renderer-host" / "package.json",
            live2d / "pyproject.toml",
            skill / "scripts" / "speak.py",
            skill / "scripts" / "rollout_adapter.py",
            skill / "requirements-cpu.txt",
        ]
        if all(path.is_file() for path in required):
            return {
                "repo": repo,
                "presence": presence,
                "live2d": live2d,
                "skill": skill,
            }
    raise ConflictError(
        "Could not locate canonical presence-runtime, live2d-avatar-runtime, and codex-voice sources"
    )


def _copy_tree(source: Path, destination: Path, *, ignore: Iterable[str] = ()) -> None:
    patterns = tuple(ignore) + ("__pycache__", "*.pyc", ".pytest_cache", "node_modules")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*patterns))


def _download(url: str, destination: Path) -> None:
    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        with urllib.request.urlopen(url) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if temporary.stat().st_size == 0:
            raise ConflictError(f"Downloaded asset is empty: {destination.name}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_rmtree(path: Path) -> None:
    home = presence_home().resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError as exc:
        raise ConflictError(f"Refusing to remove path outside Presence home: {path}") from exc
    if not relative.parts:
        raise ConflictError("Refusing to remove Presence home as a whole")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _install_provider(
    provider: str,
    base_python: list[str],
    skill: Path,
    models: Path,
) -> Path:
    environment = presence_home() / "providers" / provider / ".venv"
    python = _ensure_environment(environment, base_python)
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    requirements = skill / f"requirements-{provider}.txt"
    _run([str(python), "-m", "pip", "install", "--upgrade", "-r", str(requirements)])
    if provider == "directml":
        if os.name != "nt":
            raise ValidationError("DirectML is available only on Windows")
        _run([str(python), "-m", "pip", "uninstall", "-y", "onnxruntime"])
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "onnxruntime-directml==1.24.4",
            ]
        )
        _run(
            [str(python), "-m", "pip", "install", "--upgrade", "--no-deps", DIRECTML_KOKORO_URL]
        )
        patched = models / "kokoro-v1.0.int8.dml-conv2d.onnx"
        _run(
            [
                str(python),
                str(skill / "scripts" / "patch_convtranspose_1d.py"),
                str(models / "kokoro-v1.0.int8.onnx"),
                str(patched),
            ]
        )
    elif provider == "openvino":
        _run(
            [str(python), "-m", "pip", "install", "--upgrade", "--no-deps", OPENVINO_KOKORO_URL]
        )
    return python


def _windows_launcher_runtime(python: Path) -> tuple[Path, Path]:
    result = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json,sys,sysconfig; "
                "print(json.dumps([sys._base_executable, sysconfig.get_paths()['purelib']]))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ConflictError(
            "Could not inspect the managed Python launcher: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    try:
        base_text, site_text = json.loads(result.stdout.strip())
        base = Path(base_text).resolve()
        site_packages = Path(site_text).resolve()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConflictError("Managed Python returned invalid launcher paths") from exc
    if not base.is_file() or not site_packages.is_dir():
        raise ConflictError(
            f"Managed Python launcher paths are unavailable: base={base}, site={site_packages}"
        )
    return base, site_packages


def _write_launchers(python: Path) -> list[Path]:
    root = bin_path()
    root.mkdir(parents=True, exist_ok=True)
    launchers: list[Path] = []
    if os.name == "nt":
        # Run the public control CLI from the external base interpreter.  The
        # tiny bootstrap imports the managed package, but no process holds the
        # managed venv's python.exe open while `runtime uninstall` removes it.
        base_python, site_packages = _windows_launcher_runtime(python)
        bootstrap = root / "presence.py"
        bootstrap.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(site_packages)!r})\n"
            "from presence_runtime.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
            newline="\n",
        )
        command = root / "presence.cmd"
        command.write_text(
            f'@echo off\r\n"{base_python}" "{bootstrap}" %*\r\n',
            encoding="utf-8",
        )
        powershell = root / "presence.ps1"
        escaped_python = str(base_python).replace("'", "''")
        escaped_bootstrap = str(bootstrap).replace("'", "''")
        powershell.write_text(
            f"& '{escaped_python}' '{escaped_bootstrap}' @args\nexit $LASTEXITCODE\n",
            encoding="utf-8",
        )
        launchers.extend([command, powershell, bootstrap])
    else:
        launcher = root / "presence"
        escaped = str(python).replace("'", "'\\''")
        launcher.write_text(
            f"#!/bin/sh\nexec '{escaped}' -m presence_runtime.cli \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        launchers.append(launcher)
    return launchers


def _electron_executable(renderer_root: Path) -> Path:
    distribution = renderer_root / "node_modules" / "electron" / "dist"
    if os.name == "nt":
        return distribution / "electron.exe"
    if sys.platform == "darwin":
        return distribution / "Electron.app" / "Contents" / "MacOS" / "Electron"
    return distribution / "electron"


def install(
    *,
    source: Path | None = None,
    python: Path | None = None,
    provider: str | None = None,
    with_input: bool = False,
    start: bool = True,
) -> dict[str, Any]:
    previous: dict[str, Any] | None = None
    if installation_path().is_file():
        previous = read_installation(installation_path())
    previous_provider = previous.get("provider") if previous is not None else None
    selected_provider = provider or (
        previous_provider if isinstance(previous_provider, str) and previous_provider else "cpu"
    )
    if selected_provider not in SUPPORTED_PROVIDERS:
        raise ValidationError(f"Unsupported provider: {selected_provider}")
    with_input = with_input or bool(previous and previous.get("voice_input_installed"))
    installed_providers = set(previous.get("installed_providers", ())) if previous else set()
    installed_providers.add(selected_provider)
    layout = _source_layout(source)
    base_python = _select_python(python)
    home = presence_home()
    home.mkdir(parents=True, exist_ok=True)
    state_existed = state_database_path().is_file()
    previous_policy: dict[str, Any] | None = None
    if state_existed:
        from .store import PresenceStore

        with PresenceStore(state_database_path()) as existing_store:
            previous_policy = existing_store.runtime_settings()

    from . import lifecycle

    if previous is not None:
        lifecycle.stop()

    staging = home / f".install-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        _copy_tree(layout["presence"], staging / "runtime", ignore=("tests",))
        _copy_tree(layout["live2d"], staging / "live2d-runtime", ignore=("tests",))
        _copy_tree(layout["presence"] / "renderer-host", staging / "renderer")
        (staging / "adapters" / "codex").mkdir(parents=True)
        for name in (
            "activity.py",
            "cli_adapter.py",
            "clipboard.py",
            "configuration.py",
            "delivery.py",
            "inbox.py",
            "launch_codex.py",
            "profiles.py",
            "rollout_adapter.py",
            "runtime_adapter.py",
            "session_scope.py",
            "tui_bridge.py",
        ):
            shutil.copy2(
                layout["skill"] / "scripts" / name,
                staging / "adapters" / "codex" / name,
            )
        (staging / "worker").mkdir()
        for name in ("speak.py", "patch_convtranspose_1d.py"):
            shutil.copy2(layout["skill"] / "scripts" / name, staging / "worker" / name)
        if with_input:
            (staging / "stt").mkdir()
            for name in ("stt.py", "voice_input.py", "delivery.py", "clipboard.py", "inbox.py"):
                shutil.copy2(layout["skill"] / "scripts" / name, staging / "stt" / name)

        models = home / "models"
        _download(MODEL_URL, models / "kokoro-v1.0.int8.onnx")
        _download(VOICES_URL, models / "voices-v1.0.bin")

        runtime_environment = home / ".venv"
        managed_python = _ensure_environment(runtime_environment, base_python)
        _run([str(managed_python), "-m", "pip", "install", "--upgrade", "pip"])
        _run(
            [
                str(managed_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--no-deps",
                str(staging / "runtime"),
                str(staging / "live2d-runtime"),
            ]
        )
        _install_provider(selected_provider, base_python, layout["skill"], models)
        if with_input:
            stt_python = _ensure_environment(home / "stt" / ".venv", base_python)
            _run(
                [
                    str(stt_python),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "-r",
                    str(layout["skill"] / "requirements-input.txt"),
                ]
            )

        npm = shutil.which("npm")
        if npm is None:
            raise ConflictError("npm is required to install the centralized Electron renderer")
        _run([npm, "ci"], cwd=staging / "renderer")
        electron = _electron_executable(staging / "renderer")
        if not electron.is_file():
            node = shutil.which("node")
            install_script = staging / "renderer" / "node_modules" / "electron" / "install.js"
            if node is None or not install_script.is_file():
                raise ConflictError("Electron dependency installed without its runtime downloader")
            # Electron 43 publishes the downloader as an explicit package bin
            # rather than an npm postinstall hook.  Running it is therefore a
            # required, verified install step on both Windows and Fedora.
            _run([node, str(install_script)], cwd=staging / "renderer")
        if not electron.is_file():
            raise ConflictError(f"Electron runtime was not materialized: {electron}")

        replacements = {
            runtime_code_path(): staging / "runtime",
            live2d_code_path(): staging / "live2d-runtime",
            renderer_host_path(): staging / "renderer",
            adapter_code_path(): staging / "adapters",
            worker_path(): staging / "worker",
        }
        if with_input:
            replacements[home / "stt" / "scripts"] = staging / "stt"
        backups: dict[Path, Path] = {}
        placed: list[Path] = []
        launcher_paths = (
            [
                bin_path() / "presence.cmd",
                bin_path() / "presence.ps1",
                bin_path() / "presence.py",
            ]
            if os.name == "nt"
            else [bin_path() / "presence"]
        )
        launcher_backups: dict[Path, Path] = {}
        for launcher_path in launcher_paths:
            if launcher_path.is_file():
                backup = staging / f"launcher-backup-{len(launcher_backups)}"
                shutil.copy2(launcher_path, backup)
                launcher_backups[launcher_path] = backup
        installation_backup = staging / "installation-backup.json"
        if installation_path().is_file():
            shutil.copy2(installation_path(), installation_backup)
        try:
            for destination, prepared in replacements.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    backup = staging / f"backup-{len(backups)}"
                    os.replace(destination, backup)
                    backups[destination] = backup
                os.replace(prepared, destination)
                placed.append(destination)

            launchers = _write_launchers(managed_python)
            source_manifest = runtime_code_path() / "runtime-manifest.json"
            managed_sources = [
                path
                for root in (
                    runtime_code_path(),
                    live2d_code_path(),
                    renderer_host_path(),
                    adapter_code_path(),
                    worker_path(),
                )
                for path in root.rglob("*")
                if path.is_file() and "node_modules" not in path.parts
            ]
            installation = {
                "schema": INSTALLATION_SCHEMA,
                "revision": 1,
                "provider": selected_provider,
                "installed_providers": sorted(installed_providers),
                "voice_input_installed": with_input,
                "runtime_manifest": json.loads(source_manifest.read_text(encoding="utf-8")),
                "managed_files": file_inventory(home, managed_sources),
                "managed_external_files": {
                    str(path.resolve()): sha256_file(path) for path in launchers
                },
                "owned_directories": [
                    "runtime",
                    "live2d-runtime",
                    "renderer",
                    "adapters",
                    "worker",
                    ".venv",
                    *(f"providers/{item}" for item in sorted(installed_providers)),
                    "models",
                    *(["stt"] if with_input else []),
                ],
                "preserved_by_default": ["state.sqlite3", "catalog"],
            }
            atomic_json(installation_path(), installation)
            from .store import PresenceStore

            with PresenceStore(state_database_path()) as store:
                store.set_runtime_policy(
                    provider=selected_provider, microphone_permission=with_input
                )
            lifecycle.write_systemd_unit()
        except BaseException:
            for destination in reversed(tuple(replacements)):
                if destination not in placed and destination not in backups:
                    continue
                if destination.exists():
                    _safe_rmtree(destination)
                backup = backups.get(destination)
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
            for launcher_path in launcher_paths:
                launcher_path.unlink(missing_ok=True)
                backup = launcher_backups.get(launcher_path)
                if backup is not None and backup.is_file():
                    launcher_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, launcher_path)
            installation_path().unlink(missing_ok=True)
            if installation_backup.is_file():
                shutil.copy2(installation_backup, installation_path())
            if previous_policy is not None:
                from .store import PresenceStore

                with PresenceStore(state_database_path()) as restored_store:
                    restored_store.set_runtime_policy(
                        provider=previous_policy["provider"],
                        microphone_permission=previous_policy["microphone_permission"],
                    )
            elif not state_existed:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(state_database_path()) + suffix).unlink(missing_ok=True)
            if previous is not None:
                try:
                    lifecycle.start()
                except BaseException:
                    pass
            raise
    finally:
        if staging.exists():
            _safe_rmtree(staging)

    result = {
        "installed": True,
        "home": str(home),
        "provider": selected_provider,
        "voice_input_installed": with_input,
        "started": False,
    }
    if start:
        result["runtime"] = lifecycle.start()
        result["started"] = True
    return result


def _verify_external(path_text: str, digest: str) -> Path:
    path = Path(path_text)
    if path.is_file() and sha256_file(path) != digest:
        raise ConflictError(f"Refusing to remove modified managed launcher: {path}")
    return path


def _defer_windows_launcher_cleanup(paths: list[Path]) -> None:
    if not paths:
        return
    # A .cmd file cannot unlink itself while cmd.exe is still reading it.  A
    # detached base-Python helper waits for the launcher parent to return, then
    # removes all three tiny launchers.  It imports no managed runtime code.
    code = """
import ctypes, json, os, pathlib, sys, time
parent_pid = int(sys.argv[1])
paths = [pathlib.Path(item) for item in json.loads(sys.argv[2])]
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.WaitForSingleObject.restype = ctypes.c_uint32
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_int
handle = kernel32.OpenProcess(0x00100000, False, parent_pid)
if handle:
    try:
        kernel32.WaitForSingleObject(handle, 15000)
    finally:
        kernel32.CloseHandle(handle)
time.sleep(0.2)
for path in paths:
    for _ in range(30):
        try:
            path.unlink(missing_ok=True)
            break
        except OSError:
            time.sleep(0.1)
"""
    flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    subprocess.Popen(
        [
            str(Path(sys._base_executable).resolve()),
            "-c",
            code,
            str(os.getppid()),
            json.dumps([str(path) for path in paths]),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=False,
        creationflags=flags,
    )


def uninstall(
    *,
    all_projects: bool = False,
    purge_state: bool = False,
    purge_catalog: bool = False,
) -> dict[str, Any]:
    installation = read_installation(installation_path())
    from . import lifecycle
    from .adapters import ProjectAdapterManager
    from .store import PresenceStore

    store = PresenceStore(state_database_path()) if state_database_path().is_file() else None
    runtime_stopped = False
    try:
        active = store.active_sources() if store else []
        if active and not all_projects:
            raise ConflictError(
                "Presence Runtime has active sources; disconnect them or use --all-projects: "
                + ", ".join(item["source_id"] for item in active)
            )
        if all_projects and store:
            lifecycle.stop()
            runtime_stopped = True
            for project in store.list_projects():
                ProjectAdapterManager.cleanup_project_files(
                    project["project_root"],
                    project_id=project["project_instance_id"],
                )
                store.unregister_project(project["project_instance_id"], force=True)
        if purge_catalog and not purge_state and store:
            referenced = [
                binding
                for binding in store.list_bindings()
                if binding["state"] != "deleted" and store.effective_snapshot(binding["binding_id"])
            ]
            if referenced:
                raise ConflictError(
                    "Catalog is still referenced by preserved bindings; add --purge-state or leave the catalog intact"
                )
    finally:
        if store:
            store.close()

    if not runtime_stopped:
        lifecycle.stop()
    lifecycle.remove_systemd_unit()
    home = presence_home()
    for relative, digest in installation["managed_files"].items():
        verify_managed_file(home, relative, digest)
    external = [
        _verify_external(path_text, digest)
        for path_text, digest in installation.get("managed_external_files", {}).items()
    ]
    active_bootstrap = Path(sys.argv[0]).resolve()
    defer_launchers = os.name == "nt" and active_bootstrap in {
        path.resolve() for path in external
    }
    if not defer_launchers:
        for path in external:
            path.unlink(missing_ok=True)
    for relative in installation.get("owned_directories", []):
        target = (home / relative).resolve()
        if target.exists():
            _safe_rmtree(target)
    installation_path().unlink(missing_ok=True)
    for mutable in (home / "runtime.pid", home / "runtime.lock", home / "runtime.log"):
        mutable.unlink(missing_ok=True)
    if purge_state:
        for suffix in ("", "-wal", "-shm"):
            Path(str(state_database_path()) + suffix).unlink(missing_ok=True)
    if purge_catalog and catalog_path().exists():
        _safe_rmtree(catalog_path())
    if defer_launchers:
        _defer_windows_launcher_cleanup(external)
    return {
        "uninstalled": True,
        "state_preserved": not purge_state,
        "catalog_preserved": not purge_catalog,
    }
