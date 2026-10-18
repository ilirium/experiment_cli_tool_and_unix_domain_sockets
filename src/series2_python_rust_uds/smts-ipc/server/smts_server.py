#!/usr/bin/env python3
"""smts_server — keep a Python process warm and run CLI commands for a thin client.

The Rust client (``smts``) connects over a Unix domain socket and sends one
request: argv, cwd and environment, plus its own stdin/stdout/stderr file
descriptors via SCM_RIGHTS. The server runs the command against those fds,
so output goes straight to the user's terminal or pipe, then replies with
the exit status.

Two execution modes (a design trade-off, see README):

``thread`` (default)
    Each request runs on a thread inside the long-lived server process.
    In-memory state (caches, loaded models) persists across commands.
    Commands must use ``req.cwd`` / ``req.env`` / ``req.path()`` rather than
    os.getcwd()/os.environ, because those are process-global.
    sys.stdin/stdout/stderr are routed per thread, so print() still works.

``fork``
    Each request runs in a fork() of the warm server. The child really
    chdir()s, gets the client's environment and fds 0/1/2, so commands may be
    written like an ordinary CLI. State changes die with the child, and
    fork-without-exec is unsafe after threads/CUDA/Objective-C frameworks are
    initialised.

The app is any callable ``app(req: Request) -> int | None``.

Usage::

    python3 smts_server.py serve --app example_app:main [--mode thread|fork]
    python3 smts_server.py status
    python3 smts_server.py stop

Requires Python >= 3.9 and a Unix (Linux, macOS, *BSD).
See ARCHITECTURE.md for the protocol and design rationale.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import fcntl
import importlib
import json
import os
import queue
import selectors
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional, TextIO

#: Every request payload starts with these bytes.
MAGIC = b"SMTS"
#: Wire-protocol version; must equal ``PROTOCOL_VERSION`` in the Rust client.
VERSION = 1
#: Request kinds: run a command, stop the server, report status.
KIND_RUN, KIND_STOP, KIND_STATUS = ord("R"), ord("S"), ord("P")
#: Upper bound for one request frame (argv + cwd + environment).
MAX_REQUEST = 16 * 1024 * 1024
#: Seconds a client may take to send its request after connecting.
REQUEST_TIMEOUT = 5.0

_U32 = struct.Struct("!I")
_I32 = struct.Struct("!i")


# --------------------------------------------------------------------------
# Public API for command processors
# --------------------------------------------------------------------------

@dataclass(eq=False)
class Request:
    """One client invocation, as seen by the command processor.

    Write commands against this object rather than against process-global
    state (``os.getcwd()``, ``os.environ``); then the same code runs in both
    thread and fork mode.

    Attributes:
        argv: The client's full argv; ``argv[0]`` is the program name.
        cwd: The client's working directory (physical path from getcwd()).
        env: The client's environment.
        stdin: Text stream over the client's stdin fd.
        stdout: Text stream over the client's stdout fd; line-buffered
            when it is a terminal, block-buffered otherwise.
        stderr: Text stream over the client's stderr fd; line-buffered.
        cancelled: Set when the client forwards a signal (Ctrl-C etc.). In
            thread mode this is the only way a command learns about it.
        signal: Number of the last signal forwarded by the client, if any.
    """

    argv: list[str]            # argv[0] is the client's program name
    cwd: str                   # client's working directory
    env: dict[str, str]        # client's environment
    stdin: TextIO              # the client's real fds, wrapped
    stdout: TextIO
    stderr: TextIO
    cancelled: threading.Event = field(default_factory=threading.Event)
    signal: Optional[int] = None   # last signal forwarded by the client

    def path(self, p: str | os.PathLike) -> str:
        """Resolve a path relative to the client's working directory.

        Args:
            p: A relative or absolute path, as typed by the user.

        Returns:
            ``p`` joined onto :attr:`cwd`; absolute paths are returned as-is.
            The result is not normalised, so symlinks and ``..`` keep their
            usual filesystem meaning.
        """
        return os.path.join(self.cwd, os.fspath(p))

    def check_cancelled(self) -> None:
        """Abort the command if the user interrupted it.

        Long-running commands should call this periodically (or wait on
        :attr:`cancelled`), because in thread mode a forwarded Ctrl-C cannot
        interrupt a running thread.

        Raises:
            KeyboardInterrupt: If :attr:`cancelled` is set. The server maps
                this to exit status 130.
        """
        if self.cancelled.is_set():
            raise KeyboardInterrupt


#: Signature of a command processor: return an exit status (``None`` means 0).
App = Callable[[Request], Optional[int]]


def default_socket_path() -> str:
    """Return the socket path shared by the client and the server.

    The rule is ``$SMTS_SOCKET`` if set, otherwise
    ``$XDG_RUNTIME_DIR``, ``$TMPDIR`` or ``/tmp`` (first non-empty) joined
    with ``smts-<uid>.sock``. It must stay identical to ``socket_path()`` in
    the Rust client, or the two will not find each other.

    Returns:
        Absolute path of the Unix domain socket.
    """
    explicit = os.environ.get("SMTS_SOCKET")
    if explicit:
        return explicit
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(base, f"smts-{os.getuid()}.sock")


# --------------------------------------------------------------------------
# Wire protocol
# --------------------------------------------------------------------------

class ProtocolError(Exception):
    """A request was malformed, truncated, oversized or of the wrong version.

    The message is sent back to the client in an ``E`` frame, so it should
    make sense to the end user.
    """


def _frame(payload: bytes) -> bytes:
    """Prefix ``payload`` with its big-endian u32 length.

    Args:
        payload: Frame body.

    Returns:
        The complete frame, ready to ``sendall``.
    """
    return _U32.pack(len(payload)) + payload


class _Cursor:
    """Bounds-checked sequential reader over a request payload.

    Every read that would run past the end raises :class:`ProtocolError`
    instead of returning short data, so the parser never sees a partial
    field.
    """

    def __init__(self, data: bytes) -> None:
        """Start reading ``data`` at offset 0.

        Args:
            data: The request payload (without the frame length prefix).
        """
        self.data, self.off = data, 0

    def take(self, n: int) -> bytes:
        """Consume exactly ``n`` bytes.

        Args:
            n: Number of bytes to read.

        Returns:
            The next ``n`` bytes.

        Raises:
            ProtocolError: If fewer than ``n`` bytes remain.
        """
        end = self.off + n
        if end > len(self.data):
            raise ProtocolError("truncated request")
        chunk, self.off = self.data[self.off:end], end
        return chunk

    def u32(self) -> int:
        """Consume a big-endian unsigned 32-bit integer.

        Raises:
            ProtocolError: If fewer than 4 bytes remain.
        """
        return _U32.unpack(self.take(4))[0]

    def blob(self) -> bytes:
        """Consume a length-prefixed byte string (``u32 len`` + bytes).

        Raises:
            ProtocolError: If the prefix or the body is truncated.
        """
        return self.take(self.u32())


def _parse_request(payload: bytes):
    """Decode a request payload.

    Layout (see ARCHITECTURE.md, section 3.1)::

        "SMTS" u8 version u8 kind
        kind 'R': u32 argc {blob}*  blob cwd  u32 envc {blob "K=V"}*

    argv and cwd are decoded with :func:`os.fsdecode` (surrogateescape),
    the same decoding CPython applies to its own ``sys.argv``, so undecodable
    bytes survive a round trip. The environment is kept as bytes because fork
    mode installs it via ``os.environb``.

    Args:
        payload: Request frame body.

    Returns:
        ``(kind, fields)`` where ``kind`` is one of ``KIND_*`` and
        ``fields`` is ``(argv, cwd, env_bytes)`` for ``KIND_RUN``, otherwise
        ``None``. Environment entries without ``=`` or with an empty key are
        dropped.

    Raises:
        ProtocolError: On bad magic, version mismatch or truncation.
    """
    cur = _Cursor(payload)
    if cur.take(4) != MAGIC:
        raise ProtocolError("not an smts request")
    version = cur.take(1)[0]
    if version != VERSION:
        raise ProtocolError(
            f"client speaks protocol v{version}, server v{VERSION}; rebuild or restart one of them")
    kind = cur.take(1)[0]
    if kind != KIND_RUN:
        return kind, None
    # os.fsdecode: same surrogateescape decoding Python applies to sys.argv.
    argv = [os.fsdecode(cur.blob()) for _ in range(cur.u32())]
    cwd = os.fsdecode(cur.blob())
    env: dict[bytes, bytes] = {}
    for _ in range(cur.u32()):
        key, sep, value = cur.blob().partition(b"=")
        if sep and key:
            env[key] = value
    return kind, (argv, cwd, env)


def _recv_exactly(sock: socket.socket, n: int, buf: bytearray) -> None:
    """Receive from ``sock`` until ``buf`` holds at least ``n`` bytes.

    Appends to ``buf`` in place, so bytes already received (for example the
    chunk returned by ``recv_fds``) count towards ``n``. May read past ``n``
    if the peer has sent more.

    Args:
        sock: Connected socket, possibly with a timeout set.
        n: Target length of ``buf``.
        buf: Buffer to fill.

    Raises:
        ProtocolError: If the peer closes the connection first.
        OSError: On socket errors, including ``TimeoutError``.
    """
    while len(buf) < n:
        chunk = sock.recv(max(65536, n - len(buf)))
        if not chunk:
            raise ProtocolError("client disconnected mid-request")
        buf += chunk


def _read_request(conn: socket.socket):
    """Read one request frame plus the file descriptors sent with it.

    The client attaches its fds to the first ``sendmsg``, so the first read
    uses ``socket.recv_fds`` to collect the ancillary data together with the
    first chunk of bytes. On any failure every received fd is closed, so
    nothing leaks.

    Args:
        conn: A freshly accepted, blocking connection with a timeout.

    Returns:
        ``(payload, fds, leftover)``. ``fds`` is the list of received
        descriptors (now owned by the caller). ``leftover`` holds bytes the
        client sent after the request, e.g. an early Ctrl-C ``K`` frame.

    Raises:
        ProtocolError: If fds were truncated (``MSG_CTRUNC``), the frame
            exceeds :data:`MAX_REQUEST`, or the client disconnects.
        OSError: On socket errors or timeout.
    """
    data, fds, flags, _ = socket.recv_fds(conn, 65536, 3)
    if flags & getattr(socket, "MSG_CTRUNC", 0):
        for fd in fds:
            os.close(fd)
        raise ProtocolError("ancillary data truncated")
    try:
        buf = bytearray(data)
        _recv_exactly(conn, 4, buf)
        (n,) = _U32.unpack_from(buf)
        if n > MAX_REQUEST:
            raise ProtocolError(f"request too large ({n} bytes)")
        _recv_exactly(conn, 4 + n, buf)
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    return bytes(buf[4:4 + n]), list(fds), buf[4 + n:]


# --------------------------------------------------------------------------
# Peer authentication: only the server's own uid may run commands
# --------------------------------------------------------------------------

_libc = None


def _peer_uid(sock: socket.socket) -> int:
    """Return the uid of the process at the other end of ``sock``.

    The kernel records the credentials at ``connect()`` time, so the peer
    cannot forge them. Linux uses ``SO_PEERCRED``; macOS and the BSDs use
    ``getpeereid(3)`` via ctypes, since CPython does not wrap it.

    Args:
        sock: A connected ``AF_UNIX`` socket.

    Returns:
        The peer's effective uid.

    Raises:
        OSError: If the credentials cannot be retrieved.
    """
    if hasattr(socket, "SO_PEERCRED"):                       # Linux
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    global _libc                                             # macOS, *BSD
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    if _libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return uid.value


# --------------------------------------------------------------------------
# Running a command
# --------------------------------------------------------------------------

def _wrap_fds(fds, *, closefd: bool) -> tuple[TextIO, TextIO, TextIO]:
    """Open text streams over the client's fds, mimicking CPython's stdio.

    UTF-8 throughout; ``surrogateescape`` on stdin/stdout so arbitrary bytes
    round-trip, ``backslashreplace`` on stderr so error output never fails.
    stdout is line-buffered only when it is a terminal (block-buffered when
    piped, which is much faster); stderr is always line-buffered.

    Args:
        fds: Three descriptors: stdin, stdout, stderr.
        closefd: Whether closing a stream closes its fd. ``True`` in thread
            mode (the server owns the received copies), ``False`` in fork mode
            (fds 0-2 belong to the child process itself).

    Returns:
        ``(stdin, stdout, stderr)`` text streams.

    Raises:
        OSError: If an fd is invalid.
    """
    stdin = open(fds[0], "r", encoding="utf-8", errors="surrogateescape", closefd=closefd)
    stdout = open(fds[1], "w", encoding="utf-8", errors="surrogateescape", closefd=closefd,
                  buffering=1 if os.isatty(fds[1]) else -1)
    stderr = open(fds[2], "w", encoding="utf-8", errors="backslashreplace", closefd=closefd,
                  buffering=1)
    return stdin, stdout, stderr


def _invoke(app: App, req: Request) -> int:
    """Run ``app(req)`` and translate its outcome into a CLI exit status.

    Mirrors what the interpreter does for a normal script:

    =============================== =======================================
    outcome                         status
    =============================== =======================================
    returns ``int`` / ``None``      that int / 0
    ``SystemExit(code)``            ``code`` (a str is printed, then 1)
    ``KeyboardInterrupt``           130
    ``BrokenPipeError``             141 (e.g. ``smts ... | head``)
    any other exception             traceback to the client's stderr, 1
    =============================== =======================================

    Never raises (except ``BaseException`` subclasses not listed above, such
    as ``GeneratorExit``); stdout is flushed afterwards, ignoring a broken
    pipe.

    Args:
        app: The command processor.
        req: The request to run it with.

    Returns:
        The exit status to report to the client.
    """
    try:
        rc = app(req)
        rc = 0 if rc is None else int(rc)
    except SystemExit as e:                 # sys.exit(), argparse errors, --help
        if e.code is None:
            rc = 0
        elif isinstance(e.code, int):
            rc = e.code
        else:
            print(e.code, file=req.stderr)
            rc = 1
    except KeyboardInterrupt:
        rc = 128 + signal.SIGINT
    except BrokenPipeError:                 # e.g. `smts ... | head`
        rc = 128 + signal.SIGPIPE
    except Exception:
        try:
            traceback.print_exc(file=req.stderr)
        except Exception:
            pass
        rc = 1
    try:
        req.stdout.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass
    return rc


#: Per-thread current request streams, read by :class:`_RoutedStream`.
_local = threading.local()


class _RoutedStream:
    """Stand-in for sys.std* in thread mode.

    On a worker thread it forwards to that request's stream, elsewhere to the
    server's original stream. Lets print(), input(), argparse, logging to
    sys.stderr etc. work unchanged inside commands.

    Implemented with ``__getattr__``, so *every* attribute (``write``,
    ``flush``, ``fileno``, ``isatty``, ``buffer``, ``encoding`` …) is looked
    up on the current target at call time. Code that checks
    ``isinstance(sys.stdout, io.TextIOBase)`` will see through the proxy;
    that is rare in practice.
    """

    def __init__(self, name: str, fallback: TextIO) -> None:
        """Create a proxy for one standard stream.

        Args:
            name: ``"stdin"``, ``"stdout"`` or ``"stderr"``; the attribute
                name looked up on :data:`_local`.
            fallback: The stream to use outside request threads (the
                server's original ``sys.<name>``). Also used by
                :meth:`Server._log` to bypass routing.
        """
        self._name = name
        self._fallback = fallback

    def _target(self) -> TextIO:
        """Return the stream for the calling thread."""
        return getattr(_local, self._name, None) or self._fallback

    def __getattr__(self, attr):
        """Delegate any attribute not defined on the proxy to the target."""
        return getattr(self._target(), attr)

    def __iter__(self):
        """Iterate the target (special methods bypass ``__getattr__``)."""
        return iter(self._target())


@dataclass(eq=False)
class _Job:
    """Server-side state of one running command.

    ``eq=False`` keeps identity hashing, so jobs can live in a ``set`` and
    serve as selector keys.

    Attributes:
        conn: Connection to the client; registered with the selector while
            the command runs (for ``K`` frames and EOF detection).
        rbuf: Unparsed bytes received from the client.
        pid: Child pid (fork mode only).
        request: The :class:`Request` given to the app (thread mode only).
        exit_code: Status to send in the final ``X`` frame.
        client_gone: The client disconnected; no reply will be sent.
    """

    conn: socket.socket
    rbuf: bytearray
    pid: Optional[int] = None           # fork mode
    request: Optional[Request] = None   # thread mode
    exit_code: int = 0
    client_gone: bool = False


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class AlreadyRunning(Exception):
    """Another server process holds the lock for this socket path.

    Not an error during auto-start: two clients may start servers at the
    same moment, and the loser simply exits while the winner serves.
    """


class Server:
    """Single-threaded event loop that accepts requests and runs commands.

    The loop (``selectors.DefaultSelector``: epoll on Linux, kqueue on
    macOS) watches three kinds of things:

    * the listening socket (new clients),
    * a wakeup socketpair, written by signal handlers
      (``signal.set_wakeup_fd``) and by finished worker threads,
    * one connection per running job (``K`` frames and EOF).

    Only worker threads (thread mode) run concurrently with it; they report
    back through a queue plus a wakeup byte. Keeping the loop itself
    single-threaded is what makes ``fork()`` safe in fork mode.
    """

    def __init__(self, app: App, *, path: str, mode: str = "thread") -> None:
        """Configure the server; nothing is opened until :meth:`serve_forever`.

        Args:
            app: The command processor called for every ``R`` request.
            path: Filesystem path of the Unix domain socket. ``<path>.lock``
                is used for the single-instance lock.
            mode: ``"thread"`` or ``"fork"``; see the module docstring.

        Raises:
            ValueError: For an unknown mode.
        """
        if mode not in ("thread", "fork"):
            raise ValueError(f"unknown mode {mode!r}")
        self._app, self._path, self._mode = app, path, mode
        self._jobs: set[_Job] = set()
        self._by_pid: dict[int, _Job] = {}
        self._done: queue.SimpleQueue[_Job] = queue.SimpleQueue()
        self._stopping = False
        self._served = 0
        self._started = time.time()

    # -- lifecycle ---------------------------------------------------------

    def serve_forever(self) -> None:
        """Acquire the lock, bind the socket and run the event loop.

        Returns after a graceful stop (``stop`` request or first
        SIGTERM/SIGINT/SIGHUP, then all running commands finished). Installs
        process-wide signal handlers and, in thread mode, replaces
        ``sys.stdin/stdout/stderr`` with :class:`_RoutedStream` proxies, so
        call it once per process.

        Raises:
            AlreadyRunning: If another server owns the socket path.
            SystemExit: On a second stop signal (immediate shutdown).
        """
        self._lock_fd = self._acquire_lock()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._listener = self._bind()
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._listener, selectors.EVENT_READ, "accept")
        self._sel.register(self._wake_r, selectors.EVENT_READ, "wake")

        signal.set_wakeup_fd(self._wake_w.fileno(), warn_on_full_buffer=False)
        signal.signal(signal.SIGCHLD, lambda *_: None)   # handler needed for the wakeup fd
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, self._on_stop_signal)

        if self._mode == "thread":
            sys.stdin = _RoutedStream("stdin", sys.stdin)
            sys.stdout = _RoutedStream("stdout", sys.stdout)
            sys.stderr = _RoutedStream("stderr", sys.stderr)

        self._log(f"listening on {self._path} (mode={self._mode}, pid={os.getpid()})")
        try:
            while not (self._stopping and not self._jobs):
                for key, _ in self._sel.select():
                    if key.data == "accept":
                        self._accept()
                    elif key.data == "wake":
                        self._on_wake()
                    else:
                        self._on_client_readable(key.data)
        finally:
            self._cleanup()
            self._log("stopped")

    def _acquire_lock(self) -> int:
        """Take an exclusive, non-blocking ``flock`` on ``<path>.lock``.

        The kernel drops the lock when the process dies, however it dies, so
        there is no stale-lock problem. Holding it is what makes it safe to
        unlink an existing socket file in :meth:`_bind`.

        Returns:
            The lock file descriptor; it must stay open for the server's
            lifetime.

        Raises:
            AlreadyRunning: If another process holds the lock.
        """
        fd = os.open(self._path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(f"another server already owns {self._path}") from None
        return fd

    def _bind(self) -> socket.socket:
        """Create the listening socket with mode 0600.

        The umask is narrowed *before* ``bind()``, so the socket file never
        exists with wider permissions, not even briefly.

        Returns:
            A non-blocking listening ``AF_UNIX`` stream socket.

        Raises:
            OSError: If the path is too long (``sun_path``: 104 bytes on
                macOS, 108 on Linux) or not writable.
        """
        # We hold the lock, so an existing socket file is a stale leftover.
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o177)           # socket is born 0600, no window
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self._path)
        finally:
            os.umask(old_umask)
        sock.listen(64)
        sock.setblocking(False)
        return sock

    def _on_stop_signal(self, signum, _frame) -> None:
        """Signal handler for SIGTERM/SIGINT/SIGHUP.

        The first signal starts a graceful stop; a second one aborts.

        Args:
            signum: The signal received.
            _frame: Unused stack frame.

        Raises:
            SystemExit: ``128 + signum`` on the second signal.
        """
        if self._stopping:
            raise SystemExit(128 + signum)    # second signal: stop now
        self._log(f"received signal {signum}")
        self._begin_stop()

    def _begin_stop(self) -> None:
        """Stop accepting new clients and let running commands finish.

        Closes and unlinks the listening socket at once, so new clients see
        "no server" (and may auto-start a fresh one) instead of hanging. The
        event loop exits once no jobs remain. Idempotent.
        """
        if self._stopping:
            return
        self._stopping = True
        self._sel.unregister(self._listener)
        self._listener.close()
        self._unlink_socket()
        if self._jobs:
            self._log(f"stopping after {len(self._jobs)} running command(s) finish")

    def _unlink_socket(self) -> None:
        """Remove the socket file, ignoring it already being gone."""
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    def _cleanup(self) -> None:
        """Release everything on the way out of :meth:`serve_forever`.

        Disables the signal wakeup fd, removes the socket if a forced exit
        skipped :meth:`_begin_stop`, sends SIGTERM to any remaining children,
        closes client connections and releases the lock.
        """
        signal.set_wakeup_fd(-1)
        if not self._stopping:
            self._listener.close()
            self._unlink_socket()
        for job in list(self._jobs):
            if job.pid is not None:
                try:
                    os.kill(job.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            job.conn.close()
        os.close(self._lock_fd)

    # -- accepting requests ----------------------------------------------

    def _accept(self) -> None:
        """Accept one client, authenticate it, read its request and dispatch.

        Steps: ``accept()``; reject peers whose uid differs from ours; read
        the request and its fds (blocking, with :data:`REQUEST_TIMEOUT`);
        then handle ``S`` (stop), ``P`` (status) or ``R`` (run, which needs
        exactly three fds). Errors become an ``E`` reply and every received
        fd is closed.

        Note:
            The request is read synchronously on the event loop. A same-user
            client that connects and then stalls delays other clients by up
            to :data:`REQUEST_TIMEOUT` (see ARCHITECTURE.md, section 10).
        """
        try:
            conn, _ = self._listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        fds: list[int] = []
        try:
            conn.setblocking(True)
            uid = _peer_uid(conn)
            if uid != os.getuid():
                self._log(f"rejected connection from uid {uid}")
                conn.close()
                return
            conn.settimeout(REQUEST_TIMEOUT)
            payload, fds, leftover = _read_request(conn)
            kind, fields = _parse_request(payload)
        except (OSError, ProtocolError) as e:
            for fd in fds:
                os.close(fd)
            self._reply_error(conn, str(e))
            return

        if kind == KIND_STOP:
            self._reply(conn, b"X" + _I32.pack(0))
            self._begin_stop()
        elif kind == KIND_STATUS:
            info = {"pid": os.getpid(), "mode": self._mode, "socket": self._path,
                    "running": len(self._jobs), "served": self._served,
                    "uptime_s": round(time.time() - self._started, 1)}
            self._reply(conn, b"I" + json.dumps(info).encode())
        elif kind == KIND_RUN and len(fds) == 3:
            self._served += 1
            conn.setblocking(False)
            job = _Job(conn=conn, rbuf=bytearray(leftover))
            self._jobs.add(job)
            self._sel.register(conn, selectors.EVENT_READ, job)
            if self._mode == "fork":
                self._start_fork(job, fields, fds)
            else:
                self._start_thread(job, fields, fds)
            self._process_client_frames(job)
        else:
            for fd in fds:
                os.close(fd)
            self._reply_error(conn, f"unexpected request kind {kind!r} with {len(fds)} fds")

    def _reply(self, conn: socket.socket, payload: bytes) -> None:
        """Send one frame and close the connection.

        Best effort: if the client has already gone, the error is ignored.
        Uses a short blocking timeout because the loop may have left the
        socket non-blocking.

        Args:
            conn: Client connection; always closed on return.
            payload: Frame body (``X``, ``E`` or ``I`` + data).
        """
        try:
            conn.setblocking(True)
            conn.settimeout(2)
            conn.sendall(_frame(payload))
        except OSError:
            pass
        finally:
            conn.close()

    def _reply_error(self, conn: socket.socket, message: str) -> None:
        """Log ``message`` and send it to the client as an ``E`` frame.

        Args:
            conn: Client connection; closed on return.
            message: Human-readable reason; the client prints it.
        """
        self._log(f"bad request: {message}")
        self._reply(conn, b"E" + message.encode())

    # -- thread mode -------------------------------------------------------

    def _start_thread(self, job: _Job, fields, fds: list[int]) -> None:
        """Run a request on a new daemon thread (thread mode).

        The received fds are wrapped with ``closefd=True``: from here on the
        streams own them, and :meth:`_thread_main` closes them when done.

        Args:
            job: The job being started; ``job.request`` is filled in.
            fields: ``(argv, cwd, env_bytes)`` from :func:`_parse_request`.
            fds: The client's stdin, stdout, stderr (server-owned copies).

        Raises:
            OSError: If the fds cannot be wrapped (they are closed first).
        """
        argv, cwd, envb = fields
        try:
            stdin, stdout, stderr = _wrap_fds(fds, closefd=True)
        except OSError:
            for fd in fds:
                os.close(fd)
            raise
        env = {os.fsdecode(k): os.fsdecode(v) for k, v in envb.items()}
        job.request = Request(argv, cwd, env, stdin, stdout, stderr)
        threading.Thread(target=self._thread_main, args=(job,),
                         name=f"smts-{self._served}", daemon=True).start()

    def _thread_main(self, job: _Job) -> None:
        """Body of a worker thread: run the app, then hand the job back.

        Points this thread's :data:`_local` at the request streams so
        :class:`_RoutedStream` routes ``print()`` etc. to the client; closes
        the streams (releasing the server's fd copies, which matters for
        pipe EOF); queues the job and wakes the event loop, which sends the
        exit status.

        Args:
            job: The job whose ``request`` to run.
        """
        req = job.request
        _local.stdin, _local.stdout, _local.stderr = req.stdin, req.stdout, req.stderr
        try:
            if not os.path.isdir(req.cwd):
                print(f"smts: working directory {req.cwd!r} does not exist", file=req.stderr)
                job.exit_code = 1
            else:
                job.exit_code = _invoke(self._app, req)
        finally:
            _local.__dict__.clear()
            for stream in (req.stdout, req.stderr, req.stdin):
                try:
                    stream.close()
                except Exception:
                    pass
            self._done.put(job)
            self._wake()

    def _wake(self) -> None:
        """Wake the event loop from another thread.

        Writes one byte to the wakeup socketpair. If its buffer is full a
        wakeup is already pending, so the error is ignored.
        """
        try:
            self._wake_w.send(b"\0")
        except OSError:
            pass                               # buffer full: a wakeup is pending anyway

    # -- fork mode ---------------------------------------------------------

    def _start_fork(self, job: _Job, fields, fds: list[int]) -> None:
        """Run a request in a forked child (fork mode).

        Flushes the server's own stdio first (otherwise buffered output would
        be duplicated into the child). After the fork the parent closes its
        copies of the client's fds immediately: a pipe reader only sees EOF
        once every writer has closed it.

        Args:
            job: The job being started; ``job.pid`` is filled in.
            fields: ``(argv, cwd, env_bytes)`` from :func:`_parse_request`.
            fds: The client's stdin, stdout, stderr.
        """
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            self._child_main(job, fields, fds)   # never returns
        for fd in fds:
            os.close(fd)
        job.pid = pid
        self._by_pid[pid] = job

    def _child_main(self, job: _Job, fields, fds: list[int]) -> None:
        """Turn the forked child into "the command" and run it. Never returns.

        1. Reset signal dispositions inherited from the server (SIGINT
           raises ``KeyboardInterrupt`` again; SIGTERM/SIGHUP/SIGCHLD back to
           default; no wakeup fd).
        2. Close server-only fds: listener, wakeup pair, every job
           connection (including this one), lock fd. The selector is left
           alone: kqueue fds are not inherited by ``fork()`` on macOS, so
           closing one could close an unrelated fd.
        3. ``dup2`` the client's fds onto 0/1/2, install its environment
           (``os.environb``), argv and stdio, and ``chdir`` to its cwd.
        4. Run the app via :func:`_invoke`, flush, and ``os._exit`` with the
           status. ``_exit`` skips ``atexit`` handlers and other cleanup that
           belongs to the parent server.

        Args:
            job: The job this child serves.
            fields: ``(argv, cwd, env_bytes)`` from :func:`_parse_request`.
            fds: The client's stdin, stdout, stderr.
        """
        rc = 70
        try:
            signal.set_wakeup_fd(-1)
            for s in (signal.SIGCHLD, signal.SIGTERM, signal.SIGHUP):
                signal.signal(s, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.default_int_handler)
            for sock in (self._listener, self._wake_r, self._wake_w,
                         *(j.conn for j in self._jobs)):
                sock.close()
            os.close(self._lock_fd)

            for target, fd in enumerate(fds):
                os.dup2(fd, target)
            for fd in fds:
                if fd > 2:
                    os.close(fd)

            argv, cwd, envb = fields
            os.environb.clear()
            os.environb.update(envb)
            sys.stdin, sys.stdout, sys.stderr = _wrap_fds((0, 1, 2), closefd=False)
            sys.argv = list(argv)
            req = Request(list(argv), cwd, dict(os.environ), sys.stdin, sys.stdout, sys.stderr)
            try:
                os.chdir(cwd)
            except OSError as e:
                print(f"smts: cannot enter {cwd!r}: {e}", file=sys.stderr)
                rc = 1
            else:
                rc = _invoke(self._app, req)
        except BaseException:
            try:
                traceback.print_exc()
            except BaseException:
                pass
        finally:
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except BaseException:
                    pass
            os._exit(rc & 0xFF)

    def _reap_children(self) -> None:
        """Collect every exited child and finish its job.

        Loops on ``waitpid(-1, WNOHANG)`` because several SIGCHLDs can
        coalesce into one wakeup. A child killed by signal N is reported as
        ``128 + N``, the shell convention.
        """
        while self._by_pid:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            job = self._by_pid.pop(pid, None)
            if job is not None:
                code = os.waitstatus_to_exitcode(status)
                job.exit_code = code if code >= 0 else 128 - code
                self._finish(job)

    # -- events from clients and workers ----------------------------------

    def _on_wake(self) -> None:
        """Handle the wakeup socketpair becoming readable.

        Drains it, reaps exited children (fork mode) and finishes every job
        that worker threads have queued (thread mode).
        """
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        if self._mode == "fork":
            self._reap_children()
        while True:
            try:
                job = self._done.get_nowait()
            except queue.Empty:
                break
            self._finish(job)

    def _on_client_readable(self, job: _Job) -> None:
        """Handle data or EOF from a client whose command is running.

        Data is buffered and parsed as ``K`` frames. EOF means the client
        died without forwarding a signal (e.g. it was SIGKILLed); the command
        gets SIGTERM (or ``cancelled`` in thread mode) and no reply is sent.

        Args:
            job: The job whose connection became readable.
        """
        try:
            data = job.conn.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            # Client died (e.g. SIGKILL) — nobody is waiting for this command.
            job.client_gone = True
            self._sel.unregister(job.conn)
            self._signal_job(job, signal.SIGTERM)
            return
        job.rbuf += data
        self._process_client_frames(job)

    def _process_client_frames(self, job: _Job) -> None:
        """Parse complete frames from ``job.rbuf``, leaving any partial one.

        Only ``K`` (forward a signal) is meaningful while a command runs;
        anything else is ignored.

        Args:
            job: The job whose buffer to parse.
        """
        while len(job.rbuf) >= 4:
            (n,) = _U32.unpack_from(job.rbuf)
            if len(job.rbuf) < 4 + n:
                return
            frame = bytes(job.rbuf[4:4 + n])
            del job.rbuf[:4 + n]
            if len(frame) == 5 and frame[:1] == b"K":
                (signum,) = _I32.unpack_from(frame, 1)
                self._signal_job(job, signum)

    def _signal_job(self, job: _Job, signum: int) -> None:
        """Deliver a client signal to a running command.

        Fork mode: a real ``kill()`` on the child. Thread mode: threads
        cannot be interrupted, so record the signal on the request and set
        ``req.cancelled``; the command must poll it.

        Args:
            job: The target job.
            signum: The signal number the client received.
        """
        if job.pid is not None:
            try:
                os.kill(job.pid, signum)
            except ProcessLookupError:
                pass
        elif job.request is not None:
            # Threads cannot be interrupted; the command polls req.cancelled.
            job.request.signal = signum
            job.request.cancelled.set()

    def _finish(self, job: _Job) -> None:
        """Retire a completed job and send its exit status (``X`` frame).

        Args:
            job: The completed job. Its connection is closed in all cases.
        """
        self._jobs.discard(job)
        if job.client_gone:
            job.conn.close()
            return
        try:
            self._sel.unregister(job.conn)
        except (KeyError, ValueError):
            pass
        self._reply(job.conn, b"X" + _I32.pack(job.exit_code))

    @staticmethod
    def _log(msg: str) -> None:
        """Write a timestamped line to the server's own stderr.

        Bypasses :class:`_RoutedStream` routing, so log lines never end up
        in a client's terminal even when called from a worker thread.

        Args:
            msg: The message.
        """
        stream = getattr(sys.stderr, "_fallback", sys.stderr)
        print(f"[smts {time.strftime('%H:%M:%S')}] {msg}", file=stream, flush=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_app(spec: str) -> App:
    """Import the command processor named by ``module:callable``.

    Args:
        spec: E.g. ``"example_app:main"``. The callable defaults to
            ``main`` when the ``:attr`` part is omitted.

    Returns:
        The callable.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the attribute does not exist.
        TypeError: If it exists but is not callable.
    """
    module_name, _, attr = spec.partition(":")
    app = getattr(importlib.import_module(module_name), attr or "main")
    if not callable(app):
        raise TypeError(f"{spec} is not callable")
    return app


def _control(path: str, kind: int) -> bytes:
    """Send a control request (stop/status) to a running server.

    Control requests carry no body and no file descriptors.

    Args:
        path: Socket path.
        kind: :data:`KIND_STOP` or :data:`KIND_STATUS`.

    Returns:
        The reply frame body (``X``, ``I`` or ``E`` + data).

    Raises:
        FileNotFoundError, ConnectionRefusedError: If no server is listening.
        ProtocolError: If the server closes without replying.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(path)
        s.sendall(_frame(MAGIC + bytes([VERSION, kind])))
        buf = bytearray()
        _recv_exactly(s, 4, buf)
        (n,) = _U32.unpack_from(buf)
        _recv_exactly(s, 4 + n, buf)
        return bytes(buf[4:])


def main(argv: Optional[list[str]] = None) -> int:
    """Command-line entry point: ``serve``, ``stop`` or ``status``.

    ``serve`` imports the app *before* binding the socket, so clients can
    only connect once the server is fully warm, then ``chdir("/")`` so that
    a command which wrongly uses relative paths fails loudly in thread mode.

    Args:
        argv: Arguments without the program name; defaults to
            ``sys.argv[1:]``.

    Returns:
        Process exit status. ``serve`` also returns 0 when another instance
        already owns the socket (an auto-start race, not an error).
    """
    p = argparse.ArgumentParser(description="Warm Python server for the smts client.")
    p.add_argument("--socket", default=default_socket_path(), help="socket path (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the server in the foreground")
    serve.add_argument("--app", required=True, help="command processor as module:callable")
    serve.add_argument("--mode", choices=("thread", "fork"), default="thread")
    serve.add_argument("--path", action="append", default=[],
                       help="extra directory to put on sys.path (repeatable)")
    sub.add_parser("stop", help="ask a running server to stop")
    sub.add_parser("status", help="show a running server's status")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        sys.path[:0] = args.path
        app = _load_app(args.app)        # pay the import cost once, up front
        os.chdir("/")                    # relative paths in commands should fail loudly
        try:
            Server(app, path=args.socket, mode=args.mode).serve_forever()
        except AlreadyRunning as e:
            print(f"smts_server: {e}", file=sys.stderr)
            return 0                     # not an error for auto-start races
        return 0

    try:
        reply = _control(args.socket, KIND_STOP if args.cmd == "stop" else KIND_STATUS)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"smts_server: no server on {args.socket}", file=sys.stderr)
        return 1
    if reply[:1] == b"I":
        print(json.dumps(json.loads(reply[1:]), indent=2))
    elif reply[:1] == b"E":
        print(f"smts_server: {reply[1:].decode()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
