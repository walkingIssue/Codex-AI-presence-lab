"""Install the project-local Codex AI Presence voice integration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from session_scope import ensure_state_file


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
RUNTIME_MANIFEST_NAME = "RUNTIME-MANIFEST.md"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
DIRECTML_KOKORO_URL = "git+https://github.com/walkingIssue/kokoro-onnx-intel-arc.git@intel-arc-directml"
MODEL_NAME = "kokoro-v1.0.int8.onnx"
VOICES_NAME = "voices-v1.0.bin"
VOICE_GITIGNORE = """.venv/
.cuda-venv/
.dml-venv/
*.wav
*.log
*.pid
orb/node_modules/
orb-position.json
orb.enabled
volume
commentary-volume
kokoro-v1.0*.onnx
voices-v1.0.bin
gpu_patch/*.onnx
sessions.json
"""
ORB_FILES = (
    "index.html",
    "main.cjs",
    "preload.cjs",
    "renderer.js",
    "styles.css",
    "package.json",
    "package-lock.json",
    "start_orb.ps1",
    "stop_orb.ps1",
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
    for name in ORB_FILES:
        shutil.copy2(source / name, destination / name)
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


def install_start_script(voice_root: Path) -> None:
    shutil.copy2(SCRIPT_ROOT / "start_voice.ps1", voice_root / "start_voice.ps1")


def install_activity_script(voice_root: Path) -> None:
    """Expose the host-adapter activity bridge from the project runtime."""
    shutil.copy2(SCRIPT_ROOT / "activity.py", voice_root / "activity.py")


def install_app_server_bridge(voice_root: Path) -> None:
    """Expose the transparent Codex app-server bridge from the project runtime."""
    shutil.copy2(SCRIPT_ROOT / "app_server_bridge.py", voice_root / "app_server_bridge.py")


def install_runtime_manifest(voice_root: Path) -> None:
    """Copy the tracked ownership inventory into the project runtime."""
    shutil.copy2(SKILL_ROOT / RUNTIME_MANIFEST_NAME, voice_root / RUNTIME_MANIFEST_NAME)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--enable", action="store_true", help="enable voice after setup")
    parser.add_argument("--force", action="store_true", help="back up and replace a different existing speak.py")
    parser.add_argument("--no-orb", action="store_true", help="skip copying/installing the Strand Orb")
    parser.add_argument(
        "--python",
        type=Path,
        help="base Python 3.11 or 3.12 executable for isolated environments",
    )
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument("--cuda", action="store_true", help="install the untested NVIDIA CUDA 12.x path")
    provider_group.add_argument("--directml", action="store_true", help="install the experimental Intel/DirectML path")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    base_python = select_base_python(args.python)
    voice_root = project_root / ".codex-voice"
    voice_root.mkdir(parents=True, exist_ok=True)
    write_default(voice_root / ".gitignore", VOICE_GITIGNORE)
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

    (voice_root / "gpu_patch").mkdir(exist_ok=True)
    write_default(voice_root / "voice", "bf_isabella\n")
    write_default(voice_root / "mode", "stream\n")
    write_default(voice_root / "speed", "1.08\n")
    write_default(voice_root / "volume", "20\n")
    write_default(voice_root / "commentary-volume", "50\n")
    (voice_root / "provider").write_text(provider + "\n", encoding="utf-8")
    if args.enable:
        (voice_root / "enabled").write_text("on\n", encoding="utf-8")
    install_hook(project_root, voice_root, args.force)
    install_orb(voice_root, args.no_orb)
    install_start_script(voice_root)
    install_activity_script(voice_root)
    install_app_server_bridge(voice_root)
    install_runtime_manifest(voice_root)

    print(f"Codex AI Presence setup complete in {project_root}")
    print(f"Base Python: {' '.join(base_python)}")
    print(f"Provider: {provider}")
    print(f"Enable/control: python {Path.home() / '.codex' / 'skills' / 'codex-voice' / 'scripts' / 'toggle.py'} status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
