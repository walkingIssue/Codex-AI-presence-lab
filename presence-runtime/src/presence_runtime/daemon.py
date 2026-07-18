"""User-level Presence Runtime supervisor entry point."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from types import FrameType
from typing import IO, Any, Mapping

from .catalog import Catalog
from .adapters import ProjectAdapterManager
from .control import ControlAPI
from .controller import RuntimeController
from .errors import ConflictError
from .managed import read_installation
from .migration import LegacyMigrator
from .paths import (
    installation_path,
    lock_path,
    pid_path,
    presence_home,
    provider_python,
    renderer_host_path,
    renderer_udp_port,
    stt_python,
    worker_path,
)
from .renderer import ElectronRendererSupervisor
from .server import PresenceServer
from .store import PresenceStore
from .stt import STTWorkerSupervisor
from .worker import KokoroWorkerSupervisor


class RuntimeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: IO[bytes] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ConflictError("Another Presence Runtime supervisor owns the lock") from exc
        self.handle = handle

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "RuntimeLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def _write_pid() -> None:
    temporary = pid_path().with_suffix(".tmp")
    temporary.write_text(f"{os.getpid()}\n", encoding="utf-8")
    os.replace(temporary, pid_path())


def _playback_loop(controller: RuntimeController, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            played = controller.play_next()
        except BaseException as exc:
            print(f"presence playback loop error: {exc}", flush=True)
            stop.wait(0.5)
            continue
        if played is None:
            stop.wait(0.2)


def run() -> int:
    installation = read_installation(installation_path())
    available = set(installation.get("installed_providers", ()))
    if not available:
        raise ConflictError("Installation has no managed inference provider")
    home = presence_home()
    home.mkdir(parents=True, exist_ok=True)
    with RuntimeLock(lock_path()):
        _write_pid()
        store = PresenceStore()
        catalog = Catalog()
        catalog.initialize()
        # Model packs and their assets are immutable, but their generated
        # renderer shell belongs to this runtime release.  Refresh it before
        # Electron can load a stale catalog copy after an upgrade.
        catalog.refresh_avatar_renderers()
        policy = store.runtime_settings()
        provider = str(policy["provider"])
        if provider not in available:
            raise ConflictError(
                f"Selected provider {provider!r} is unavailable; installed: {sorted(available)}"
            )
        models = home / "models"
        input_root = home / "input" / "recordings"
        voice_udp_port = renderer_udp_port()
        input_root.mkdir(parents=True, exist_ok=True)
        stt = STTWorkerSupervisor(
            python=stt_python(),
            script=home / "stt" / "scripts" / "stt.py",
            runtime_root=home,
        )
        input_installed = bool(stt.status()["installed"])
        capture_inputs: dict[str, str] = {}
        capture_lock = threading.Lock()
        transcription_threads: set[threading.Thread] = set()
        transcription_lock = threading.Lock()
        controller_holder: dict[str, RuntimeController] = {}

        def input_status() -> dict[str, Any]:
            return {
                "permission": store.runtime_settings()["microphone_permission"],
                **stt.status(),
            }

        def renderer_input(message: Mapping[str, Any]) -> None:
            binding_id = message.get("binding_id")
            capture_id = message.get("capture_id")
            state = message.get("state")
            if not isinstance(binding_id, str) or not isinstance(capture_id, str):
                raise ConflictError("renderer input event omitted binding or capture identity")
            if not store.runtime_settings()["microphone_permission"]:
                raise ConflictError("machine microphone permission is disabled")
            controller = controller_holder["controller"]
            if state == "capture-start":
                input_id = store.begin_input(binding_id, capture_id)
                with capture_lock:
                    capture_inputs[capture_id] = input_id
                controller.pause_playback(binding_id)
                return
            with capture_lock:
                input_id = capture_inputs.pop(capture_id, None)
            if input_id is None:
                raise ConflictError(f"voice input capture {capture_id!r} was not started")
            controller.resume_playback(binding_id)
            if state == "capture-cancel":
                store.finish_input(input_id, diagnostic="capture cancelled")
                return
            recording_value = message.get("recording")
            recording = Path(recording_value).resolve() if isinstance(recording_value, str) else None
            if state != "capture-finish" or recording is None:
                store.finish_input(input_id, diagnostic="invalid renderer input transition")
                return
            try:
                recording.relative_to(input_root.resolve())
            except ValueError:
                store.finish_input(input_id, diagnostic="recording escaped managed input root")
                return

            def transcribe() -> None:
                try:
                    transcript = stt.transcribe(recording)
                    store.finish_input(input_id, transcript=transcript)
                except BaseException as exc:
                    store.finish_input(input_id, diagnostic=str(exc))
                finally:
                    recording.unlink(missing_ok=True)
                    with transcription_lock:
                        transcription_threads.discard(threading.current_thread())

            thread = threading.Thread(
                target=transcribe,
                name=f"presence-stt-{capture_id[:8]}",
                daemon=True,
            )
            with transcription_lock:
                transcription_threads.add(thread)
            thread.start()

        renderer = ElectronRendererSupervisor(
            host_root=renderer_host_path(),
            catalog=catalog,
            store=store,
            udp_port=voice_udp_port,
            input_enabled=bool(policy["microphone_permission"] and input_installed),
            input_root=input_root,
            input_handler=renderer_input,
        )
        voice = KokoroWorkerSupervisor(
            runtime_root=home,
            python=provider_python(provider),
            worker_script=worker_path() / "speak.py",
            renderer_udp_port=voice_udp_port,
            provider=provider,
            model_path=models / "kokoro-v1.0.int8.onnx",
            voices_path=models / "voices-v1.0.bin",
            dml_model_path=models / "kokoro-v1.0.int8.dml-conv2d.onnx",
        )
        controller = RuntimeController(
            store=store,
            catalog=catalog,
            voice=voice,
            renderer=renderer,
            input_status=input_status,
        )
        controller_holder["controller"] = controller
        migrator = LegacyMigrator(controller)
        adapters = ProjectAdapterManager(store)
        shutdown = threading.Event()
        holder: dict[str, PresenceServer] = {}

        def request_shutdown() -> None:
            shutdown.set()
            server = holder.get("server")
            if server is not None:
                threading.Timer(0.1, server.stop).start()

        def policy_changed(updated: dict[str, Any]) -> None:
            enabled = bool(updated["microphone_permission"] and input_installed)
            renderer.set_input_enabled(enabled)
            if enabled:
                threading.Thread(target=stt.start, name="presence-stt-warm", daemon=True).start()

        control = ControlAPI(
            controller,
            migrator=migrator,
            on_shutdown=request_shutdown,
            available_providers=available,
            running_provider=provider,
            input_available=input_installed,
            on_policy_changed=policy_changed,
            adapter_manager=adapters,
        )
        server = PresenceServer(
            controller,
            control_api=control,
            migrator=migrator,
        )
        holder["server"] = server

        def signal_handler(_signal: int, _frame: FrameType | None) -> None:
            request_shutdown()

        for selected in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(selected, signal_handler)
            except (ValueError, OSError):
                pass

        playback = threading.Thread(
            target=_playback_loop,
            args=(controller, shutdown),
            name="presence-playback",
            daemon=True,
        )
        try:
            if not voice.start():
                raise ConflictError("Kokoro worker did not become ready")
            if not renderer.start():
                raise ConflictError(
                    "Electron renderer did not become ready: "
                    + str(renderer.status().get("last_error") or "unknown error")
                )
            if policy["microphone_permission"] and input_installed:
                threading.Thread(target=stt.start, name="presence-stt-warm", daemon=True).start()
            controller.rehydrate()
            playback.start()
            server.listener.open()
            adapters.start_monitor()
            print(
                f"Presence Runtime ready pid={os.getpid()} provider={provider}",
                flush=True,
            )
            server.serve_forever()
        finally:
            shutdown.set()
            adapters.close()
            if playback.is_alive():
                playback.join(timeout=3)
            with transcription_lock:
                pending_transcriptions = tuple(transcription_threads)
            for thread in pending_transcriptions:
                thread.join(timeout=5)
            controller.close()
            voice.stop()
            stt.stop()
            renderer.close()
            store.close()
            pid_path().unlink(missing_ok=True)
    return 0


def main() -> int:
    try:
        return run()
    except BaseException as exc:
        pid_path().unlink(missing_ok=True)
        print(f"Presence Runtime failed: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
