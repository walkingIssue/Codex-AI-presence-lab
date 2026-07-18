from __future__ import annotations

import os

from presence_runtime.lifecycle import _pid_running


def test_pid_running_recognizes_the_current_process() -> None:
    assert _pid_running(os.getpid()) is True
    assert _pid_running(None) is False
