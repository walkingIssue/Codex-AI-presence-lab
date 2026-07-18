"""Toggle the project-local Codex Kokoro voice hook."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from configuration import load_settings, marker_enabled
from session_scope import (
    current_thread_id,
    is_project_mode,
    load_state,
    register_session,
    registered_session_ids,
    set_project_mode,
    unregister_session,
)


WATCHER_SCRIPT = Path(__file__).with_name("watcher.py")


def environment_python(root: Path) -> Path:
    """Return the virtualenv interpreter path for the current platform."""
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def find_voice_root() -> Path | None:
    current = Path.cwd().resolve()
    candidates = (current, *current.parents)
    for candidate in candidates:
        voice_root = candidate / ".codex-voice"
        if voice_root.is_dir():
            return voice_root
    return None


def watcher_pid_path(voice_root: Path) -> Path:
    return voice_root / "watcher.pid"


def watcher_unit_name(voice_root: Path) -> str:
    digest = hashlib.sha256(str(voice_root.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"codex-voice-watcher-{digest}"


def orb_unit_name(voice_root: Path) -> str:
    digest = hashlib.sha256(str((voice_root / "orb").resolve()).encode("utf-8")).hexdigest()[:12]
    return f"codex-strand-orb-{digest}"


def watcher_pid(voice_root: Path) -> int | None:
    try:
        return int(watcher_pid_path(voice_root).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def watcher_is_running(voice_root: Path) -> bool:
    pid = watcher_pid(voice_root)
    if pid is not None and os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout
        except OSError:
            return False
    pid_running = False
    if pid is not None:
        try:
            os.kill(pid, 0)
        except PermissionError:
            pid_running = True
        except OSError:
            pid_running = False
        else:
            pid_running = True
        if pid_running and os.name != "nt":
            # A stale marker can point at an unrelated process (PID 1/3 are
            # common after a runtime restart).  Only accept a process whose
            # command line owns this exact project watcher.
            try:
                command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                command_line = ""
            expected_root = str(voice_root.resolve())
            if "watcher.py" not in command_line or expected_root not in command_line:
                pid_running = False
    if pid_running:
        return True
    systemctl = shutil.which("systemctl")
    if os.name != "nt" and systemctl and os.environ.get("XDG_RUNTIME_DIR"):
        result = subprocess.run(
            [systemctl, "--user", "is-active", "--quiet", f"{watcher_unit_name(voice_root)}.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    return False


def orb_pid(voice_root: Path) -> int | None:
    try:
        return int((voice_root / "orb" / "orb.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def orb_is_running(voice_root: Path) -> bool:
    pid = orb_pid(voice_root)
    if pid is not None and os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout
        except OSError:
            return False
    if pid is not None:
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            pass
        else:
            return True
    systemctl = shutil.which("systemctl")
    if os.name != "nt" and systemctl and os.environ.get("XDG_RUNTIME_DIR"):
        result = subprocess.run(
            [systemctl, "--user", "is-active", "--quiet", f"{orb_unit_name(voice_root)}.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    return False


def run_orb_script(voice_root: Path, name: str) -> None:
    if os.name != "nt" and name.endswith(".ps1"):
        name = f"{name[:-4]}.sh"
    script = voice_root / "orb" / name
    if not script.is_file():
        raise FileNotFoundError(script)
    if os.name != "nt":
        subprocess.run([str(script)], cwd=str(voice_root), check=True)
        return
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        cwd=str(voice_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def start_watcher(voice_root: Path) -> None:
    if watcher_is_running(voice_root):
        return
    project_root = voice_root.parent
    arguments = [
        str(WATCHER_SCRIPT),
        "--project-root",
        str(project_root),
        "--voice-root",
        str(voice_root),
        "--start-time",
        str(time.time()),
    ]

    if os.name != "nt":
        systemd_run = shutil.which("systemd-run")
        if systemd_run and os.environ.get("XDG_RUNTIME_DIR"):
            watcher_log = voice_root / "watcher.log"
            unit = watcher_unit_name(voice_root)
            command = [
                systemd_run,
                "--user",
                f"--unit={unit}",
                "--collect",
                "--quiet",
                f"--working-directory={project_root}",
                f"--property=StandardOutput=append:{watcher_log}",
                f"--property=StandardError=append:{watcher_log}",
                sys.executable,
                *arguments,
            ]
            try:
                result = subprocess.run(command, check=False)
                if result.returncode == 0:
                    return
            except OSError:
                pass
        try:
            process = subprocess.Popen(
                [sys.executable, *arguments],
                cwd=str(project_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return
        watcher_pid_path(voice_root).write_text(str(process.pid), encoding="utf-8")
        return

    def powershell_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    argument_list = " ".join(
        '"' + value.replace('"', '\\"') + '"' for value in arguments
    )
    command = (
        "$process = Start-Process "
        f"-FilePath {powershell_quote(sys.executable)} "
        f"-ArgumentList {powershell_quote(argument_list)} "
        f"-WorkingDirectory {powershell_quote(str(project_root))} "
        "-WindowStyle Hidden -PassThru; "
        "Write-Output $process.Id"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    pid = next((line.strip() for line in reversed(result.stdout.splitlines()) if line.strip().isdigit()), None)
    if pid is not None:
        watcher_pid_path(voice_root).write_text(pid, encoding="utf-8")


def stop_watcher(voice_root: Path) -> None:
    systemctl = shutil.which("systemctl")
    if os.name != "nt" and systemctl and os.environ.get("XDG_RUNTIME_DIR"):
        subprocess.run(
            [systemctl, "--user", "stop", f"{watcher_unit_name(voice_root)}.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    pid = watcher_pid(voice_root)
    if pid is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def restart_watcher(voice_root: Path) -> None:
    if watcher_is_running(voice_root):
        stop_watcher(voice_root)
        time.sleep(0.5)
    start_watcher(voice_root)


def require_thread_id() -> str | None:
    thread_id = current_thread_id()
    if thread_id is None:
        print(
            "No CODEX_THREAD_ID was found. Run this from a Codex session, "
            "or use project-on for project-wide voice.",
            file=sys.stderr,
        )
    return thread_id


def main() -> int:
    from presence_compat import delegate

    delegated = delegate("toggle", sys.argv[1:])
    if delegated is not None:
        return delegated
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "on",
            "off",
            "session-on",
            "session-off",
            "project-on",
            "project-off",
            "all-on",
            "all-off",
            "stream",
            "quality",
            "provider-cpu",
            "provider-directml",
            "provider-cuda",
            "provider-openvino",
            "provider-status",
            "progress-on",
            "progress-off",
            "orb-on",
            "orb-off",
            "orb-status",
            "runtime-restart",
            "status",
        ),
    )
    args = parser.parse_args()

    voice_root = find_voice_root()
    if voice_root is None:
        print("No project-local .codex-voice directory was found.", file=sys.stderr)
        return 2

    marker = voice_root / "enabled"
    project_root = voice_root.parent

    if args.operation in {"on", "session-on"}:
        thread_id = require_thread_id()
        if thread_id is None:
            return 2
        state = register_session(voice_root, project_root, thread_id)
        marker.write_text("on\n", encoding="utf-8")
        restart_watcher(voice_root)
        print(
            f"Codex voice: on for this session ({voice_root}; "
            f"registered sessions: {len(registered_session_ids(state))})"
        )
        return 0

    if args.operation in {"project-on", "all-on"}:
        set_project_mode(voice_root)
        marker.write_text("on\n", encoding="utf-8")
        restart_watcher(voice_root)
        print(f"Codex voice: on for all sessions in {project_root} ({voice_root})")
        return 0

    if args.operation in {"off", "project-off", "all-off"}:
        marker.unlink(missing_ok=True)
        stop_watcher(voice_root)
        print(f"Codex voice: off ({voice_root})")
        return 0

    if args.operation == "session-off":
        thread_id = require_thread_id()
        if thread_id is None:
            return 2
        current_state = load_state(voice_root)
        if is_project_mode(current_state):
            print(
                "Codex voice is currently project-scoped; use project-off "
                "before switching to individual sessions.",
                file=sys.stderr,
            )
            return 2
        state = unregister_session(voice_root, thread_id)
        ids = registered_session_ids(state)
        if ids:
            marker.write_text("on\n", encoding="utf-8")
            restart_watcher(voice_root)
        else:
            marker.unlink(missing_ok=True)
            stop_watcher(voice_root)
        print(
            f"Codex voice: session removed ({voice_root}; "
            f"registered sessions: {len(ids)})"
        )
        return 0

    if args.operation in {"stream", "quality"}:
        (voice_root / "mode").write_text(f"{args.operation}\n", encoding="utf-8")
        print(f"Codex voice mode: {args.operation} ({voice_root})")
        return 0

    if args.operation in {"provider-cpu", "provider-directml", "provider-cuda", "provider-openvino"}:
        provider = (
            "directml"
            if args.operation == "provider-directml"
            else "cuda"
            if args.operation == "provider-cuda"
            else "openvino"
            if args.operation == "provider-openvino"
            else "cpu"
        )
        if provider == "directml":
            dml_python = environment_python(voice_root / ".dml-venv")
            dml_model = voice_root / "gpu_patch" / "kokoro-v1.0.int8.dml-conv2d.onnx"
            if not dml_python.is_file() or not dml_model.is_file():
                print(
                    "DirectML is not ready: expected .dml-venv and the patched graph under gpu_patch.",
                    file=sys.stderr,
                )
                return 2
        if provider == "cuda":
            cuda_python = environment_python(voice_root / ".cuda-venv")
            model = voice_root / "kokoro-v1.0.int8.onnx"
            if not cuda_python.is_file() or not model.is_file():
                print(
                    "CUDA is not ready: expected .cuda-venv and the base Kokoro model.",
                    file=sys.stderr,
                )
                return 2
        if provider == "openvino":
            openvino_python = environment_python(voice_root / ".openvino-venv")
            model = voice_root / "kokoro-v1.0.int8.onnx"
            if not openvino_python.is_file() or not model.is_file():
                print(
                    "OpenVINO is not ready: expected .openvino-venv and the base Kokoro model.",
                    file=sys.stderr,
                )
                return 2
        (voice_root / "provider").write_text(f"{provider}\n", encoding="utf-8")
        if watcher_is_running(voice_root):
            stop_watcher(voice_root)
            time.sleep(0.5)
            start_watcher(voice_root)
        print(f"Codex voice provider: {provider} ({voice_root})")
        return 0

    if args.operation == "provider-status":
        try:
            provider = (voice_root / "provider").read_text(encoding="utf-8").strip().lower()
        except OSError:
            provider = "cpu"
        if provider in {"cuda", "cudaexecutionprovider", "nvidia", "nvidia-cuda"}:
            provider = "cuda"
        elif provider in {"directml", "dml", "gpu"}:
            provider = "directml"
        elif provider in {"openvino", "openvinoexecutionprovider", "intel", "arc", "arc-openvino"}:
            provider = "openvino"
        else:
            provider = "cpu"
        print(f"Codex voice provider: {provider} ({voice_root})")
        return 0

    if args.operation in {"progress-on", "progress-off"}:
        progress_path = voice_root / "progress"
        if args.operation == "progress-on":
            progress_path.write_text("on\n", encoding="utf-8")
        else:
            progress_path.unlink(missing_ok=True)
        state = "on" if args.operation == "progress-on" else "off"
        print(f"Codex visible progress voice: {state} ({voice_root})")
        return 0

    if args.operation in {"orb-on", "orb-off"}:
        marker = voice_root / "orb.enabled"
        if args.operation == "orb-on":
            marker.write_text("on\n", encoding="utf-8")
            run_orb_script(voice_root, "start_orb.ps1")
            print(f"Codex Strand Orb: on ({voice_root})")
        else:
            marker.unlink(missing_ok=True)
            run_orb_script(voice_root, "stop_orb.ps1")
            print(f"Codex Strand Orb: off ({voice_root})")
        return 0

    if args.operation == "orb-status":
        marker = voice_root / "orb.enabled"
        enabled = marker.is_file() and marker.read_text(encoding="utf-8").strip().lower() in {
            "1",
            "true",
            "on",
            "enabled",
        }
        print(
            f"Codex Strand Orb: {'on' if enabled else 'off'} "
            f"({voice_root}; window: {'running' if orb_is_running(voice_root) else 'stopped'})"
        )
        return 0

    if args.operation == "runtime-restart":
        restart_watcher(voice_root)
        orb_marker = voice_root / "orb.enabled"
        if marker_enabled(orb_marker):
            run_orb_script(voice_root, "stop_orb.ps1")
            run_orb_script(voice_root, "start_orb.ps1")
        print(
            f"Codex project presence runtime restarted ({voice_root}; "
            f"Orb: {'on' if marker_enabled(orb_marker) else 'off'})"
        )
        return 0

    enabled = marker_enabled(marker)
    watcher = "running" if watcher_is_running(voice_root) else "stopped"
    settings = load_settings(voice_root)
    mode = settings["mode"]
    speed = settings["speed"]
    progress = "on" if settings["progress"] else "off"
    provider = settings["provider"]
    voice = settings["voice"]
    volume = settings["volume"]
    commentary_volume = settings["commentary_volume"]
    orb = "on" if settings["orb"] else "off"
    scope_state = load_state(voice_root)
    scope = "project" if is_project_mode(scope_state) else "session"
    registered = registered_session_ids(scope_state)
    thread_id = current_thread_id()
    registration = "n/a" if thread_id is None else "yes" if thread_id in registered else "no"
    registered_count = "all matching" if is_project_mode(scope_state) else str(len(registered))
    print(
        f"Codex voice: {'on' if enabled else 'off'} "
        f"({voice_root}; voice: {voice}; speed: {speed}; mode: {mode}; "
        f"volume: {volume}%; commentary volume: {commentary_volume}%; "
        f"progress: {progress}; orb: {orb}; provider: {provider}; "
        f"scope: {scope}; registered sessions: {registered_count}; "
        f"current session registered: {registration}; desktop watcher: {watcher})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
