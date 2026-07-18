"""Compatibility installer for the user-level Presence Runtime v0.2."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from codex_override import install_override, remove_override
from session_scope import ensure_state_file


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
RUNTIME_MANIFEST_NAME = "RUNTIME-MANIFEST.md"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
DIRECTML_KOKORO_URL = "git+https://github.com/walkingIssue/kokoro-onnx-intel-arc.git@intel-arc-directml"
OPENVINO_KOKORO_URL = "git+https://github.com/walkingIssue/kokoro-onnx-intel-arc.git@main"
MODEL_NAME = "kokoro-v1.0.int8.onnx"
VOICES_NAME = "voices-v1.0.bin"
VOICE_GITIGNORE = """.venv/
.cuda-venv/
.dml-venv/
.openvino-venv/
.stt-venv/
*.wav
*.log
*.pid
tts-progress.json
tts-progress.tmp
orb/node_modules/
orb-position.json
orb.enabled
volume
commentary-volume
kokoro-v1.0*.onnx
voices-v1.0.bin
gpu_patch/*.onnx
sessions.json
avatar-selection.json
avatar-state.json
avatar-states.json
avatar-state-status.json
avatar-state-statuses.json
.avatar-state.json.*.tmp
.avatar-states.json.*.tmp
avatar-state-statuses.json.tmp
presence-profiles.json
input.json
inbox.sqlite3*
inbox/
stt-models/
"""
ORB_FILES = (
    "frame_policy.cjs",
    "index.html",
    "main.cjs",
    "preload.cjs",
    "presence_windows.cjs",
    "voice_control.cjs",
    "renderer.js",
    "styles.css",
    "package.json",
    "package-lock.json",
    "start_orb.sh",
    "start_orb.ps1",
    "stop_orb.sh",
    "stop_orb.ps1",
)
LEGACY_RUNTIME_FILES = (
    "main.cjs",
    "preload.cjs",
    "styles.css",
    "watcher.py",
)
MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 13)


def run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    return subprocess.run(command, cwd=str(cwd) if cwd else None, check=check, text=True)


def environment_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def directml_supported() -> bool:
    """Return whether the configured DirectML provider can run on this host.

    The Intel Arc fork currently documents only the Windows DirectML
    execution-provider path.  Failing early on Fedora is safer than creating
    a misleading .dml-venv that silently falls back to CPU.
    """
    return os.name == "nt"


def request_codex_override(mode: str) -> bool:
    """Resolve the install-time choice for the optional Windows command shim."""

    if mode == "disable":
        return False
    if os.name != "nt":
        if mode == "enable":
            raise RuntimeError("--codex-override enable is supported only on Windows.")
        return False
    if mode == "enable":
        return True
    if not sys.stdin.isatty():
        print("Skipping the user-wide Codex override because setup is non-interactive; use --codex-override enable to opt in.")
        return False
    answer = input("Enable the user-wide Codex adapter override for app-server commands? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def python_version(command: list[str]) -> tuple[int, int] | None:
    result = subprocess.run(
        [*command, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except (ValueError, TypeError):
        return None


def supported_python(version: tuple[int, int] | None) -> bool:
    return version is not None and MIN_PYTHON <= version < MAX_PYTHON


def select_base_python(requested: Path | None) -> list[str]:
    candidates: list[list[str]] = []
    if requested is not None:
        candidates.append([str(requested.expanduser().resolve())])
    else:
        candidates.append([sys.executable])
        launcher = shutil.which("py") if os.name == "nt" else None
        if launcher:
            candidates.extend([[launcher, "-3.12"], [launcher, "-3.11"]])

    rejected: list[str] = []
    for candidate in candidates:
        version = python_version(candidate)
        if supported_python(version):
            print(f"Using Python {version[0]}.{version[1]} for isolated environments: {' '.join(candidate)}")
            return candidate
        version_text = "unavailable" if version is None else f"{version[0]}.{version[1]}"
        rejected.append(f"{' '.join(candidate)} ({version_text})")

    supported = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}-{MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1}"
    raise RuntimeError(
        f"Kokoro requires a supported Python runtime ({supported}); rejected: {', '.join(rejected)}. "
        "Install Python 3.11 or 3.12, or pass --python PATH."
    )


def ensure_environment(root: Path, base_python: list[str]) -> Path:
    python = environment_python(root)
    if python.is_file() and supported_python(python_version([str(python)])):
        return python
    if root.exists():
        print(f"Replacing incompatible isolated environment: {root}")
        if root.is_symlink():
            root.unlink()
        else:
            shutil.rmtree(root)
    if not python.is_file():
        print(f"Creating isolated environment: {root}")
        run([*base_python, "-m", "venv", str(root)])
    if not python.is_file():
        raise RuntimeError(f"Could not create isolated environment: {root}")
    return python


def install_requirements(python: Path, requirements: Path) -> None:
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(python), "-m", "pip", "install", "--upgrade", "-r", str(requirements)])


def download(url: str, destination: Path) -> None:
    if destination.is_file() and destination.stat().st_size > 0:
        print(f"Using existing model asset: {destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
        delete=False,
    ) as handle:
        partial = Path(handle.name)
    try:
        print(f"Downloading {destination.name}...")
        with urllib.request.urlopen(url) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def write_default(path: Path, value: str) -> None:
    if not path.exists():
        path.write_text(value, encoding="utf-8")


def ensure_gitignore(path: Path) -> None:
    """Add newly managed runtime patterns without replacing local entries."""
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    existing_lines = set(existing.splitlines())
    missing = [line for line in VOICE_GITIGNORE.splitlines() if line and line not in existing_lines]
    if not missing:
        return
    updated = existing.rstrip("\r\n")
    if updated:
        updated += "\n"
    updated += "\n".join(missing) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, path)


def electron_binary(orb_root: Path) -> Path:
    if os.name == "nt":
        return orb_root / "node_modules" / "electron" / "dist" / "electron.exe"
    if sys.platform == "darwin":
        return orb_root / "node_modules" / "electron" / "dist" / "Electron.app" / "Contents" / "MacOS" / "Electron"
    return orb_root / "node_modules" / "electron" / "dist" / "electron"


def repair_electron_runtime(orb_root: Path) -> bool:
    """Run Electron's postinstall explicitly when npm skipped the binary download."""
    binary = electron_binary(orb_root)
    if binary.is_file():
        return True

    install_script = orb_root / "node_modules" / "electron" / "install.js"
    node = shutil.which("node")
    if node is None or not install_script.is_file():
        return False

    print("Electron runtime is missing; running Electron's installer...")
    run([node, str(install_script)], cwd=orb_root, check=False)
    if binary.is_file():
        print(f"Electron runtime ready: {binary}")
        return True
    return False


def install_hook(project_root: Path, voice_root: Path, force: bool) -> None:
    hooks_dir = project_root / ".codex" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    source = SCRIPT_ROOT / "speak.py"
    destination = hooks_dir / "speak.py"
    if destination.exists() and destination.read_bytes() != source.read_bytes():
        if not force:
            raise RuntimeError(
                f"Existing hook differs: {destination}. Re-run with --force to back it up and replace it."
            )
        backup = destination.with_name("speak.py.codex-voice-backup.py")
        if not backup.exists():
            shutil.copy2(destination, backup)
        print(f"Backed up existing hook to {backup}")
    shutil.copy2(source, destination)

    hooks_path = project_root / ".codex" / "hooks.json"
    if hooks_path.exists():
        try:
            document = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Cannot update invalid JSON hooks file: {hooks_path}") from exc
    else:
        document = {}
    if not isinstance(document, dict):
        raise RuntimeError(f"Expected a JSON object in {hooks_path}")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"Expected 'hooks' to be an object in {hooks_path}")
    stop_hooks = hooks.setdefault("Stop", [])
    if not isinstance(stop_hooks, list):
        raise RuntimeError(f"Expected 'Stop' to be a list in {hooks_path}")

    python = environment_python(voice_root / ".venv")
    command = f'"{python}" "{destination}"'
    already_registered = any(
        isinstance(wrapper, dict)
        and any(
            isinstance(entry, dict)
            and str(entry.get("commandWindows", entry.get("command", ""))) == command
            for entry in wrapper.get("hooks", [])
        )
        for wrapper in stop_hooks
    )
    if not already_registered:
        stop_hooks.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "commandWindows": command,
                        "timeout": 300,
                        "statusMessage": "Speaking Codex response",
                    }
                ]
            }
        )
        temporary = hooks_path.with_suffix(".codex-voice.tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, hooks_path)
        print(f"Registered Stop hook: {hooks_path}")


def install_orb(voice_root: Path, skip: bool) -> None:
    if skip:
        print("Skipping Strand Orb (--no-orb).")
        return
    source = SCRIPT_ROOT / "orb"
    destination = voice_root / "orb"
    destination.mkdir(parents=True, exist_ok=True)
    dependency_files = ("package.json", "package-lock.json")
    dependencies_changed = any(
        not (destination / name).is_file()
        or (destination / name).read_bytes() != (source / name).read_bytes()
        for name in dependency_files
    )
    for name in ORB_FILES:
        shutil.copy2(source / name, destination / name)
    if not dependencies_changed and electron_binary(destination).is_file():
        print("Reused existing Orb dependencies; package manifests are unchanged.")
        return
    npm = shutil.which("npm")
    if npm is None:
        print("npm was not found; voice works, but run npm ci in .codex-voice/orb to enable the orb.")
        return
    result = run([npm, "ci"], cwd=destination, check=False)
    if result.returncode != 0:
        print("Orb dependency installation failed; voice setup is still usable.")
    if not repair_electron_runtime(destination):
        print(
            "Electron's platform binary is unavailable; voice setup is still usable, "
            "but the orb needs a successful npm install and Electron download."
        )


def install_posix_script(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_start_script(voice_root: Path) -> None:
    if os.name == "nt":
        source = SCRIPT_ROOT / "start_voice.ps1"
        destination = voice_root / "start_voice.ps1"
    else:
        source = SCRIPT_ROOT / "start_voice.sh"
        destination = voice_root / "start_voice.sh"
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_activity_script(voice_root: Path) -> None:
    """Expose the host-adapter activity bridge from the project runtime."""
    shutil.copy2(SCRIPT_ROOT / "activity.py", voice_root / "activity.py")


def install_tui_bridge(voice_root: Path) -> None:
    """Expose the TUI bridge and its shared Windows CLI process boundary."""
    shutil.copy2(SCRIPT_ROOT / "cli_adapter.py", voice_root / "cli_adapter.py")
    shutil.copy2(SCRIPT_ROOT / "tui_bridge.py", voice_root / "tui_bridge.py")


def install_tui_runtime(voice_root: Path) -> None:
    """Expose the real Kokoro worker and stock-TUI launcher."""
    shutil.copy2(SCRIPT_ROOT / "tui_kokoro_worker.py", voice_root / "tui_kokoro_worker.py")
    shutil.copy2(SCRIPT_ROOT / "launch_codex.py", voice_root / "launch_codex.py")
    launcher_wrapper = voice_root / "launch_codex.sh"
    if os.name == "nt":
        shutil.copy2(SCRIPT_ROOT / "launch_codex.sh", launcher_wrapper)
    else:
        install_posix_script(SCRIPT_ROOT / "launch_codex.sh", launcher_wrapper)


def install_avatar_script(voice_root: Path) -> None:
    """Expose avatar bundle management from the project runtime."""
    shutil.copy2(SCRIPT_ROOT / "avatar.py", voice_root / "avatar.py")


def install_avatar_state_script(voice_root: Path) -> None:
    """Expose the generic model-agnostic avatar state writer."""
    shutil.copy2(SCRIPT_ROOT / "avatar_state.py", voice_root / "avatar_state.py")


def install_profile_script(voice_root: Path) -> None:
    """Expose session/profile identity routing from the project runtime."""
    for name in ("configuration.py", "profiles.py", "session_scope.py", "presence_compat.py"):
        shutil.copy2(SCRIPT_ROOT / name, voice_root / name)


def install_voice_input_scripts(voice_root: Path) -> None:
    """Expose the local inbox, input control, STT, and delivery adapters."""
    for name in (
        "inbox.py",
        "voice_input.py",
        "stt.py",
        "delivery.py",
        "clipboard.py",
        "presence_service.py",
        "runtime_adapter.py",
    ):
        shutil.copy2(SCRIPT_ROOT / name, voice_root / name)


def setup_input(voice_root: Path, base_python: list[str]) -> None:
    environment = voice_root / ".stt-venv"
    python = ensure_environment(environment, base_python)
    install_requirements(python, SKILL_ROOT / "requirements-input.txt")
    print(f"Local STT runtime is configured in {environment}")


def install_runtime_manifest(voice_root: Path) -> None:
    """Copy the tracked ownership inventory into the project runtime."""
    shutil.copy2(SKILL_ROOT / RUNTIME_MANIFEST_NAME, voice_root / RUNTIME_MANIFEST_NAME)


def remove_legacy_runtime_files(voice_root: Path) -> None:
    """Remove only obsolete files from prior managed runtime layouts."""
    for name in LEGACY_RUNTIME_FILES:
        path = voice_root / name
        if path.is_file() or path.is_symlink():
            path.unlink()
            print(f"Removed obsolete managed runtime file: {path}")


def install_managed_runtime_files(
    project_root: Path,
    voice_root: Path,
    *,
    force: bool,
    skip_orb: bool,
) -> None:
    install_hook(project_root, voice_root, force)
    install_orb(voice_root, skip_orb)
    install_start_script(voice_root)
    install_activity_script(voice_root)
    install_tui_bridge(voice_root)
    install_tui_runtime(voice_root)
    install_avatar_script(voice_root)
    install_avatar_state_script(voice_root)
    install_profile_script(voice_root)
    install_voice_input_scripts(voice_root)
    install_runtime_manifest(voice_root)
    remove_legacy_runtime_files(voice_root)


def provider_check(python: Path, label: str) -> None:
    result = subprocess.run(
        [str(python), "-c", "import onnxruntime as ort; print(','.join(ort.get_available_providers()))"],
        capture_output=True,
        text=True,
        check=False,
    )
    providers = result.stdout.strip() or result.stderr.strip() or "unavailable"
    print(f"{label} providers: {providers}")


def setup_directml(voice_root: Path, model: Path, base_python: list[str]) -> None:
    environment = voice_root / ".dml-venv"
    python = ensure_environment(environment, base_python)
    install_requirements(python, SKILL_ROOT / "requirements-directml.txt")
    # The fork inherits the CPU ORT dependency from upstream. Keep the
    # DirectML wheel as the only ONNX Runtime implementation in this env.
    run([str(python), "-m", "pip", "uninstall", "-y", "onnxruntime"], check=False)
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "onnxruntime-directml==1.24.4",
        ]
    )
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--no-deps",
            DIRECTML_KOKORO_URL,
        ]
    )
    patched = voice_root / "gpu_patch" / "kokoro-v1.0.int8.dml-conv2d.onnx"
    run(
        [
            str(python),
            str(SCRIPT_ROOT / "patch_convtranspose_1d.py"),
            str(model),
            str(patched),
        ]
    )
    provider_check(python, "DirectML")


def setup_cuda(voice_root: Path, base_python: list[str]) -> None:
    environment = voice_root / ".cuda-venv"
    python = ensure_environment(environment, base_python)
    install_requirements(python, SKILL_ROOT / "requirements-cuda.txt")
    provider_check(python, "CUDA")
    print("CUDA provider is configured but untested on this machine; use provider-cpu if it fails to initialize.")


def setup_openvino(voice_root: Path, base_python: list[str]) -> None:
    environment = voice_root / ".openvino-venv"
    python = ensure_environment(environment, base_python)
    install_requirements(python, SKILL_ROOT / "requirements-openvino.txt")
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--no-deps",
            OPENVINO_KOKORO_URL,
        ]
    )
    provider_check(python, "OpenVINO")


def v2_setup(args: argparse.Namespace) -> int:
    """Translate the legacy setup surface without creating project runtimes."""

    print(
        "warning: setup.py is a v0.1 compatibility wrapper; use `presence runtime install` "
        "and `presence project register`",
        file=sys.stderr,
    )
    command = [sys.executable, str(SCRIPT_ROOT / "presence.py"), "runtime", "install"]
    if args.python is not None:
        command.extend(["--python", str(args.python.expanduser().resolve())])
    provider = None
    if args.cuda:
        provider = "cuda"
    elif args.directml:
        provider = "directml"
    elif args.openvino:
        provider = "openvino"
    if provider is not None:
        command.extend(["--provider", provider])
    if args.with_input:
        command.append("--with-input")
    result = subprocess.run(command, text=True, check=False)
    if result.returncode:
        return result.returncode
    project_root = args.project_root.expanduser().resolve()
    if args.enable:
        from presence_compat import _presence

        return _presence(["project", "register", str(project_root)]).returncode
    print(
        f"Presence Runtime installed. Register this project when ready: "
        f"presence project register {project_root}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--enable", action="store_true", help="enable voice after setup")
    parser.add_argument("--force", action="store_true", help="back up and replace a different existing speak.py")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="replace managed code in an existing runtime without changing models, environments, provider, or state",
    )
    parser.add_argument("--no-orb", action="store_true", help="skip copying/installing the Strand Orb")
    parser.add_argument(
        "--with-input",
        action="store_true",
        help="install the optional local speech-to-text runtime in .stt-venv",
    )
    parser.add_argument(
        "--python",
        type=Path,
        help="base Python 3.11 or 3.12 executable for isolated environments",
    )
    parser.add_argument(
        "--codex-override",
        choices=("ask", "enable", "disable"),
        default="ask",
        help="ask before installing the user-wide Windows Codex adapter shim",
    )
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument("--cuda", action="store_true", help="install the untested NVIDIA CUDA 12.x path")
    provider_group.add_argument("--directml", action="store_true", help="install the experimental Intel/DirectML path")
    provider_group.add_argument("--openvino", action="store_true", help="install the Intel GPU OpenVINO path")
    args = parser.parse_args()

    if args.directml and not directml_supported():
        parser.error(
            "--directml is currently supported only on Windows: the Intel Arc "
            "Kokoro fork requires ONNX Runtime's DmlExecutionProvider and has "
            "no Linux-compatible provider path"
        )

    return v2_setup(args)

    project_root = args.project_root.expanduser().resolve()
    voice_root = project_root / ".codex-voice"
    if args.refresh:
        if args.python is not None or args.with_input or args.cuda or args.directml or args.openvino:
            parser.error("--refresh cannot change Python, input, or provider environments")
        if not voice_root.is_dir():
            parser.error(f"--refresh requires an existing runtime: {voice_root}")
        ensure_gitignore(voice_root / ".gitignore")
        ensure_state_file(voice_root)
        if args.enable:
            (voice_root / "enabled").write_text("on\n", encoding="utf-8")
        install_managed_runtime_files(
            project_root,
            voice_root,
            force=args.force,
            skip_orb=args.no_orb,
        )
        try:
            provider = (voice_root / "provider").read_text(encoding="utf-8").strip() or "cpu"
        except OSError:
            provider = "cpu"
        print(f"Codex AI Presence runtime refreshed in {project_root}")
        print(f"Provider preserved: {provider}")
        return 0

    base_python = select_base_python(args.python)
    voice_root.mkdir(parents=True, exist_ok=True)
    ensure_gitignore(voice_root / ".gitignore")
    ensure_state_file(voice_root)

    model = voice_root / MODEL_NAME
    voices = voice_root / VOICES_NAME
    download(MODEL_URL, model)
    download(VOICES_URL, voices)

    cpu_environment = voice_root / ".venv"
    cpu_python = ensure_environment(cpu_environment, base_python)
    install_requirements(cpu_python, SKILL_ROOT / "requirements-cpu.txt")
    provider_check(cpu_python, "CPU")

    provider = "cpu"
    if args.cuda:
        setup_cuda(voice_root, base_python)
        provider = "cuda"
    elif args.directml:
        setup_directml(voice_root, model, base_python)
        provider = "directml"
    elif args.openvino:
        setup_openvino(voice_root, base_python)
        provider = "openvino"

    if args.with_input:
        setup_input(voice_root, base_python)

    (voice_root / "gpu_patch").mkdir(exist_ok=True)
    write_default(voice_root / "voice", "bf_isabella\n")
    write_default(voice_root / "mode", "stream\n")
    write_default(voice_root / "speed", "1.08\n")
    write_default(voice_root / "volume", "20\n")
    write_default(voice_root / "commentary-volume", "50\n")
    input_settings = voice_root / "input.json"
    if not input_settings.exists():
        input_settings.write_text(
            json.dumps(
                {
                    "input_enabled": False,
                    "input_gesture": "hold-ctrl-alt-right",
                    "delivery_mode": "clipboard",
                    "session_lock": "through-response",
                    "session_labels": "session-change",
                    "session_label_template": "{session_name} says",
                    "max_record_seconds": 60,
                    "lock_timeout_seconds": 120,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    (voice_root / "provider").write_text(provider + "\n", encoding="utf-8")
    if args.enable:
        (voice_root / "enabled").write_text("on\n", encoding="utf-8")
    install_managed_runtime_files(
        project_root,
        voice_root,
        force=args.force,
        skip_orb=args.no_orb,
    )
    if args.codex_override == "disable":
        remove_override(project_root)
    elif request_codex_override(args.codex_override):
        install_override(
            project_root,
            voice_root,
            environment_python(cpu_environment),
            SCRIPT_ROOT / "codex_override.py",
        )
        print("User-wide Codex adapter override enabled for app-server commands.")

    print(f"Codex AI Presence setup complete in {project_root}")
    print(f"Base Python: {' '.join(base_python)}")
    print(f"Provider: {provider}")
    print(f"Enable/control: python {Path.home() / '.codex' / 'skills' / 'codex-voice' / 'scripts' / 'toggle.py'} status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
