from __future__ import annotations

import os

from presence_runtime.lifecycle import _pid_running, _render_systemd_unit


def test_pid_running_recognizes_the_current_process() -> None:
    assert _pid_running(os.getpid()) is True
    assert _pid_running(None) is False


def test_systemd_working_directory_is_an_unquoted_absolute_path() -> None:
    unit = _render_systemd_unit()
    working_directory = next(
        line for line in unit.splitlines() if line.startswith("WorkingDirectory=")
    )
    value = working_directory.removeprefix("WorkingDirectory=")

    assert value.startswith("/")
    assert not value.startswith(('"', "'"))
    assert not value.endswith(('"', "'"))
