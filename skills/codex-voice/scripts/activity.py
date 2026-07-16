"""Emit coarse, non-sensitive activity states to the project-local Strand Orb."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ORB_PORT = 17831
# The Orb endpoint is local-only, but it must still be isolated when more than
# one project runtime is active.  Keep the historical port as the fallback for
# callers that do not identify a project.
ORB_PORT_MIN = 20000
ORB_PORT_SPAN = 30000
ACTIVITY_STATES = ("idle", "thinking", "tool", "skill", "cli", "waiting", "error")
LOCAL_TOOL_NAMES = {
    "bash",
    "cmd",
    "exec",
    "powershell",
    "run_command",
    "shell_command",
    "terminal",
}
SKILL_TOOL_NAMES = {
    "skill",
    "skill_invoke",
    "skill_read",
    "skill_view",
    "skills.list",
    "skills.read",
}
ACTIVITY_TTL_SECONDS = {
    "idle": 0.0,
    "thinking": 12.0,
    "tool": 8.0,
    "skill": 12.0,
    "cli": 8.0,
    "waiting": 12.0,
    "error": 4.0,
}


def payload_of(record: dict) -> dict:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def classify_activity(record: dict) -> str | None:
    """Map rollout metadata to a visual category without reading its content."""

    if not isinstance(record, dict):
        return None
    outer_type = record.get("type")
    payload = payload_of(record)
    inner_type = payload.get("type")

    if outer_type == "event_msg":
        if inner_type == "agent_reasoning":
            return "thinking"
        if inner_type in {"task_started", "turn_started", "agent_turn_started"}:
            return "thinking"
        if inner_type in {
            "mcp_tool_call_start",
            "mcp_tool_call_end",
            "web_search_start",
            "web_search_end",
        }:
            return "tool"
        if inner_type in {"patch_apply_start", "patch_apply_end"}:
            return "cli"
        if inner_type in {"skill_start", "skill_invoked"}:
            return "skill"
        if inner_type == "skill_end":
            return "thinking"
        if inner_type in {"task_complete", "turn_complete", "session_end", "turn_aborted"}:
            return "idle"
        if inner_type in {"error", "agent_error", "stream_error", "turn_failed"}:
            return "error"
        if inner_type in {"approval_request", "user_input_required", "waiting"}:
            return "waiting"
        if inner_type == "agent_message":
            if payload.get("phase") == "final_answer":
                return "idle"
            if payload.get("phase") == "commentary":
                return "thinking"
        return None

    if outer_type == "response_item":
        if inner_type == "reasoning":
            return "thinking"
        if inner_type in {"custom_tool_call", "function_call", "web_search_call"}:
            name = payload.get("name")
            normalized_name = name.strip().lower() if isinstance(name, str) else ""
            if normalized_name in SKILL_TOOL_NAMES:
                return "skill"
            if normalized_name in {"request_user_input", "ask_user", "approval_request"}:
                return "waiting"
            return "cli" if normalized_name in LOCAL_TOOL_NAMES else "tool"
        if inner_type in {"custom_tool_call_output", "function_call_output"}:
            return "thinking"

    return None


def state_ttl_seconds(state: str) -> float:
    return ACTIVITY_TTL_SECONDS.get(state, ACTIVITY_TTL_SECONDS["thinking"])


def orb_port_for_root(voice_root: Path | None = None) -> int:
    """Return the localhost Orb port owned by one project voice runtime.

    ``CODEX_ORB_PORT`` remains an explicit override for development and
    troubleshooting.  Otherwise the canonical voice-root path provides a
    stable per-project endpoint, so independent project runtimes cannot steal
    each other's activity or audio packets.
    """
    configured = os.environ.get("CODEX_ORB_PORT")
    if configured:
        try:
            port = int(configured)
        except ValueError:
            port = 0
        if 1024 <= port <= 65535:
            return port

    if voice_root is None:
        return DEFAULT_ORB_PORT
    canonical_root = str(Path(voice_root).expanduser().resolve())
    digest = hashlib.sha256(canonical_root.encode("utf-8")).digest()
    return ORB_PORT_MIN + int.from_bytes(digest[:4], "big") % ORB_PORT_SPAN


class ActivityEmitter:
    """Send category-only activity packets over the existing localhost UDP bridge."""

    def __init__(self, port: int | None = None, voice_root: Path | None = None) -> None:
        configured_port = port if port is not None else orb_port_for_root(voice_root)
        self.address = ("127.0.0.1", configured_port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sequence = 0

    def send(
        self,
        state: str,
        *,
        source: str = "adapter",
        session_id: str | None = None,
        profile_id: str | None = None,
        avatar_id: str | None = None,
        ttl_ms: int | None = None,
    ) -> bool:
        if state not in ACTIVITY_STATES:
            raise ValueError(f"Unknown activity state: {state}")
        if ttl_ms is None:
            ttl_ms = round(state_ttl_seconds(state) * 1000)
        ttl_ms = 0 if state == "idle" else max(500, min(30000, int(ttl_ms)))
        packet: dict[str, object] = {
            "type": "activity",
            "state": state,
            "source": source,
            "sequence": self.sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "ttl_ms": ttl_ms,
        }
        if isinstance(session_id, str) and session_id:
            packet["session_id"] = session_id
        if isinstance(profile_id, str) and profile_id:
            packet["profile_id"] = profile_id
        if isinstance(avatar_id, str) and avatar_id:
            packet["avatar_id"] = avatar_id
        self.sequence += 1
        try:
            self.socket.sendto(json.dumps(packet, separators=(",", ":")).encode("utf-8"), self.address)
            return True
        except OSError:
            return False

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", choices=ACTIVITY_STATES)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--source", default="adapter")
    parser.add_argument("--session-id", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--ttl-ms", type=int)
    args = parser.parse_args()

    voice_root = args.project_root.resolve() / ".codex-voice"
    if not voice_root.is_dir():
        print(f"No project-local voice directory was found: {voice_root}")
        return 2

    emitter = ActivityEmitter(voice_root=voice_root)
    try:
        sent = emitter.send(
            args.state,
            source=args.source,
            session_id=args.session_id,
            ttl_ms=args.ttl_ms,
        )
    finally:
        emitter.close()
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
