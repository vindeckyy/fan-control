#!/usr/bin/env python3
"""Runtime files for exclusive EC ownership and directory resolution."""

from __future__ import annotations

import fcntl
import os
import pathlib

DEFAULT_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_RUNTIME", "/run/fan-control"))
DEMO_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_DEMO_RUNTIME", "/tmp/fan-control"))


def runtime_dir(demo=False, override=None):
    if override is not None:
        path = pathlib.Path(override)
    else:
        path = DEMO_RUNTIME if demo else DEFAULT_RUNTIME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise OSError(f"runtime path {path} exists and is not a directory") from exc
    if not path.is_dir():
        raise OSError(f"runtime path {path} exists and is not a directory")
    return path


class ExclusiveLock:
    """Process-lifetime flock. Released automatically if the process dies."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self._fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode())
            return True
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False

    def release(self):
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
