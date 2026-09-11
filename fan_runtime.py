#!/usr/bin/env python3
"""Runtime files for exclusive EC ownership and directory resolution."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

if sys.platform == "win32":
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    fcntl = None
else:
    try:
        import fcntl
    except ImportError:
        fcntl = None
    msvcrt = None

if sys.platform == "win32":
    _prog_data = os.environ.get("PROGRAMDATA", "C:\\ProgramData")
    DEFAULT_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_RUNTIME", str(pathlib.Path(_prog_data) / "fan-control" / "run")))
    DEMO_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_DEMO_RUNTIME", str(pathlib.Path(tempfile.gettempdir()) / "fan-control")))
else:
    DEFAULT_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_RUNTIME", "/run/fan-control"))
    DEMO_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_DEMO_RUNTIME", "/tmp/fan-control"))


def runtime_dir(demo=False, override=None):
    if override is not None:
        path = pathlib.Path(override)
    else:
        path = DEMO_RUNTIME if demo else DEFAULT_RUNTIME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return path
    except FileExistsError as exc:
        raise OSError(f"runtime path {path} exists and is not a directory") from exc
    if path.exists() and not path.is_dir():
        raise OSError(f"runtime path {path} exists and is not a directory")
    return path


class ExclusiveLock:
    """Process-lifetime flock. Released automatically if the process dies."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self._fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            self._fd = os.open(self.path, flags, 0o644)
        except OSError:
            return False

        try:
            if sys.platform == "win32" and msvcrt is not None:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode())
            return True
        except OSError:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            return False

    def release(self):
        if self._fd is None:
            return
        try:
            if sys.platform == "win32" and msvcrt is not None:
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
