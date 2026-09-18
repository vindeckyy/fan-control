#!/usr/bin/env python3
"""JSON-RPC surface shared by the daemon, GUI, tray, and CLI.

The daemon is the only root EC owner. Privileged mutations and telemetry
reach unprivileged clients (fan-gui, the tray, fan-ctl) through a single
root-owned Unix socket carrying one JSON request per line and one JSON
reply per line: ``{"method": ..., "params": {...}}`` ->
``{"ok": true, "result": ...}`` or ``{"ok": false, "error": "..."}``.
Where the Python build does not provide ``AF_UNIX`` (official Windows
builds), the same line protocol runs over a loopback TCP socket whose
port is published in the runtime directory.
"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import socket
import sys
import threading
import weakref

try:
    import grp
except ImportError:
    grp = None

SOCKET_NAME = "control.sock"
MAX_MESSAGE = 1 << 20
SOCKET_GROUP = "fan-control"
ACCEPT_TIMEOUT = 0.5
SOCKET_TIMEOUT = 10.0
_USE_AF_UNIX = hasattr(socket, "AF_UNIX")

# Stable machine-readable error codes (RPC v2). The frontend branches on the
# code, never on the human-readable message.
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
REVISION_CONFLICT = "REVISION_CONFLICT"
UNSUPPORTED = "UNSUPPORTED"
BACKEND_ERROR = "BACKEND_ERROR"
PERMISSION_DENIED = "PERMISSION_DENIED"
CONFIG_ERROR = "CONFIG_ERROR"
HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"
INTERNAL = "INTERNAL"


class RpcError(RuntimeError):
    """Error carrying a stable machine-readable code plus a message."""

    def __init__(self, message, code=INVALID_ARGUMENT):
        super().__init__(message)
        self.code = code
        self.message = message


def control_socket_path(runtime_dir):
    return f"{runtime_dir.rstrip('/') if isinstance(runtime_dir, str) else str(runtime_dir)}/{SOCKET_NAME}"


def _give_group_access(path):
    """Best-effort: allow the fan-control group to use the socket/dir.

    Unpackaged installs may not have the group; root-only access then
    remains and clients get an actionable permission error.
    """
    if grp is None or not hasattr(os, "chown"):
        return
    p = pathlib.Path(path)
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
        os.chown(path, -1, gid)
    except (KeyError, OSError):
        pass
    try:
        if p.is_dir():
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o666)
    except OSError:
        pass


class _FrameTooLarge(Exception):
    """A single request exceeded MAX_MESSAGE."""


def _read_line(sock, limit=MAX_MESSAGE):
    """Read one newline-terminated frame.

    Returns ``None`` on EOF; raises ``_FrameTooLarge`` when the frame
    exceeds ``limit`` without a newline. Bytes after the newline are kept
    in a per-socket buffer so pipelined requests are not lost.
    """
    buf = _pending_buffer(sock)
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            line = bytes(buf[:nl])
            del buf[:nl + 1]
            if len(line) > limit:
                raise _FrameTooLarge()
            return line
        if len(buf) > limit:
            raise _FrameTooLarge()
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)
        if len(buf) > limit and buf.find(b"\n") == -1:
            raise _FrameTooLarge()


def _pending_buffer(sock):
    """Per-socket leftover buffer; falls back to ephemeral on odd sockets."""
    try:
        return _READ_BUFFERS.setdefault(sock, bytearray())
    except TypeError:
        return bytearray()


_READ_BUFFERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class RpcServer:
    """Serves a ``FanController.handle``-style dispatch over a Unix socket."""

    def __init__(self, controller, socket_path):
        self.controller = controller
        self.socket_path = str(socket_path)
        self._sock = None
        self._thread = None
        self._closed = threading.Event()
        self._owns_socket_file = False
        self._error = None

    def _setup(self):
        if _USE_AF_UNIX:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                if os.path.exists(self.socket_path):
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    connected = False
                    try:
                        probe.settimeout(ACCEPT_TIMEOUT)
                        try:
                            probe.connect(self.socket_path)
                            connected = True
                        except PermissionError as exc:
                            hint = (
                                f"cannot probe control socket {self.socket_path}: permission denied; "
                                f"check that the runtime directory is owned accessibly ({SOCKET_GROUP} group?)"
                            )
                            raise OSError(hint) from exc
                        except (ConnectionRefusedError, FileNotFoundError):
                            connected = False
                        except OSError:
                            connected = False
                    finally:
                        probe.close()

                    if connected:
                        raise OSError(
                            f"control socket {self.socket_path} is already served by another process"
                        )
                    # Stale file from a crashed daemon; the caller holds the
                    # EC lock, so no live server can be attached to it.
                    try:
                        os.unlink(self.socket_path)
                    except OSError:
                        pass
                sock.bind(self.socket_path)
                try:
                    os.chmod(self.socket_path, 0o666)
                except OSError:
                    pass
                _give_group_access(self.socket_path)
                _give_group_access(os.path.dirname(self.socket_path) or "/")
                sock.listen(4)
                sock.settimeout(ACCEPT_TIMEOUT)
                return sock
            except BaseException:
                sock.close()
                raise
        else:
            path = pathlib.Path(self.socket_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                port = None
                try:
                    port = int(path.read_text().strip())
                except (ValueError, OSError):
                    port = None
                if port is not None:
                    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    connected = False
                    try:
                        probe.settimeout(ACCEPT_TIMEOUT)
                        probe.connect(("127.0.0.1", port))
                        connected = True
                    except OSError:
                        connected = False
                    finally:
                        probe.close()
                    if connected:
                        raise OSError(
                            f"control socket {self.socket_path} is already served by another process"
                        )
                try:
                    path.unlink()
                except OSError:
                    pass
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                path.write_text(str(port))
                sock.listen(4)
                sock.settimeout(ACCEPT_TIMEOUT)
                return sock
            except BaseException:
                sock.close()
                raise

    def serve_forever(self, ready_event=None):
        """Blocking accept loop. Raises if the socket cannot be created."""
        self._sock = self._setup()
        self._owns_socket_file = True
        self._error = None
        if ready_event is not None:
            ready_event.set()
        while not self._closed.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK, errno.ECONNABORTED):
                    continue
                break
            threading.Thread(
                target=self._serve_connection, args=(conn,), daemon=True
            ).start()

    def start(self):
        """Serve on a background thread; re-raise setup failures here."""
        ready = threading.Event()

        def _run():
            try:
                self.serve_forever(ready_event=ready)
            except Exception as exc:  # noqa: BLE001 - re-raised in start()
                self._error = exc
                ready.set()

        self._thread = threading.Thread(target=_run, name="fan-rpc", daemon=True)
        self._thread.start()
        ready.wait(timeout=10)
        if self._error is not None:
            raise self._error

    def _serve_connection(self, conn):
        with conn:
            while True:
                try:
                    line = _read_line(conn)
                except _FrameTooLarge:
                    self._reply(conn, {"ok": False, "error": "request exceeds 1 MiB"})
                    return
                except OSError:
                    return
                if line is None:
                    return
                try:
                    payload = json.loads(line.decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(payload.get("method"), str):
                        raise ValueError("request must be an object with a string method")
                    params = payload.get("params") or {}
                    if not isinstance(params, dict):
                        raise ValueError("params must be an object")
                    result = self.controller.handle(payload["method"], params)
                    self._reply(conn, {"ok": True, "result": result})
                except RpcError as exc:
                    self._reply(conn, {
                        "ok": False,
                        "error": {"code": exc.code, "message": str(exc)},
                    })
                except (TypeError, ValueError) as exc:  # noqa: BLE001 - reported to the client
                    self._reply(conn, {
                        "ok": False,
                        "error": {"code": INVALID_ARGUMENT, "message": str(exc)},
                    })
                except Exception as exc:  # noqa: BLE001 - reported to the client
                    self._reply(conn, {
                        "ok": False,
                        "error": {"code": INTERNAL, "message": str(exc)},
                    })

    @staticmethod
    def _reply(conn, payload):
        try:
            conn.sendall(json.dumps(payload, default=str, separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass

    def close(self):
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        if self._owns_socket_file:
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            self._owns_socket_file = False


class RpcClient:
    """Persistent connection to an ``RpcServer``; safe for one caller per
    client, or for concurrent callers serialized by an internal lock."""

    def __init__(self, socket_path):
        self.socket_path = str(socket_path)
        self._sock = None
        self._io_lock = threading.Lock()

    def connect(self):
        with self._io_lock:
            self._connect()

    def _connect(self):
        if _USE_AF_UNIX:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            target = self.socket_path
        else:
            path = pathlib.Path(self.socket_path)
            if not path.is_file():
                start_hint = (
                    "net start fan-daemon (or run 'python fan-daemon.py')"
                    if sys.platform == "win32"
                    else "systemctl start fan-daemon"
                )
                raise ConnectionError(
                    f"fan-daemon is not running; start it with: {start_hint}"
                )
            try:
                port = int(path.read_text().strip())
            except (ValueError, OSError) as exc:
                raise ConnectionError(f"invalid control port file {self.socket_path}: {exc}") from exc
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target = ("127.0.0.1", port)

        try:
            sock.connect(target)
        except PermissionError as exc:
            sock.close()
            hint = (
                "cannot access the control socket; check permissions or run as Administrator"
                if sys.platform == "win32"
                else (
                    "cannot access the control socket; add your user to the "
                    "fan-control group (sudo usermod -aG fan-control $USER) and "
                    "re-login, or run the dashboard as root"
                )
            )
            raise PermissionError(hint) from exc
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            sock.close()
            start_hint = (
                "net start fan-daemon (or run 'python fan-daemon.py')"
                if sys.platform == "win32"
                else "systemctl start fan-daemon"
            )
            raise ConnectionError(
                f"fan-daemon is not running; start it with: {start_hint}"
            ) from exc
        sock.settimeout(SOCKET_TIMEOUT)
        self._sock = sock

    def call(self, method, params=None):
        with self._io_lock:
            if self._sock is None:
                self._connect()
            try:
                return self._exchange(method, params)
            except OSError:
                # Stale connection (daemon restarted mid-session); retry
                # once on a fresh connection before giving up.
                self._close_socket()
                self._connect()
                return self._exchange(method, params)

    def _exchange(self, method, params):
        request = json.dumps({"method": method, "params": params or {}}).encode() + b"\n"
        self._sock.sendall(request)
        try:
            reply = _read_line(self._sock)
        except _FrameTooLarge as exc:
            raise ConnectionError("oversized reply from fan-daemon") from exc
        if reply is None:
            raise ConnectionError("fan-daemon closed the connection")
        try:
            payload = json.loads(reply.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConnectionError("malformed reply from fan-daemon") from exc
        if not isinstance(payload, dict):
            raise ConnectionError("malformed reply from fan-daemon")
        if not payload.get("ok"):
            error = payload.get("error")
            if isinstance(error, dict):
                raise RpcError(
                    str(error.get("message", "unknown error")),
                    str(error.get("code", "UNKNOWN")),
                )
            raise RuntimeError(str(error or "unknown error"))
        return payload.get("result")

    def close(self):
        with self._io_lock:
            self._close_socket()

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False
