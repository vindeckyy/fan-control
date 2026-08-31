#!/usr/bin/env python3
"""Runtime files for exclusive EC ownership and process discovery."""

from __future__ import annotations

import fcntl
import os
import pathlib
import signal

DEFAULT_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_RUNTIME", "/run/fan-control"))
DEMO_RUNTIME = pathlib.Path(os.environ.get("FAN_CONTROL_DEMO_RUNTIME", "/tmp/fan-control"))


def runtime_dir(demo=False, override=None):
    if override is not None:
        path = pathlib.Path(override)
    else:
        path = DEMO_RUNTIME if demo else DEFAULT_RUNTIME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def write_pid(directory, name, pid=None):
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(str(pid if pid is not None else os.getpid()) + "\n")


def read_pid(directory, name):
    path = pathlib.Path(directory) / name
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def clear_pid(directory, name):
    try:
        (pathlib.Path(directory) / name).unlink()
    except OSError:
        pass


def write_gui_pid(directory, pid=None):
    write_pid(directory, "gui.pid", pid)


def write_daemon_pid(directory, pid=None):
    write_pid(directory, "daemon.pid", pid)


def gui_lock_held(directory):
    pid = read_pid(directory, "gui.pid")
    if pid is None:
        return False
    if _pid_alive(pid):
        return True
    clear_pid(directory, "gui.pid")
    return False


def daemon_pid(directory):
    pid = read_pid(directory, "daemon.pid")
    if pid is None or not _pid_alive(pid):
        return None
    return pid


def signal_daemon(directory, sig=signal.SIGHUP):
    pid = daemon_pid(directory)
    if pid is None:
        return False
    os.kill(pid, sig)
    return True


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
