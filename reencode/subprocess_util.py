from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from typing import Any


def _apply_windows_hidden_process_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "win32":
        return kwargs

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        existing_flags = kwargs.get("creationflags", 0)
        kwargs["creationflags"] = existing_flags | create_no_window
        return kwargs

    if kwargs.get("startupinfo") is None and hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        if hasattr(subprocess, "STARTF_USESHOWWINDOW"):
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs


def run_hidden(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(command, **_apply_windows_hidden_process_kwargs(kwargs))


def popen_hidden(command: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    return subprocess.Popen(command, **_apply_windows_hidden_process_kwargs(kwargs))
