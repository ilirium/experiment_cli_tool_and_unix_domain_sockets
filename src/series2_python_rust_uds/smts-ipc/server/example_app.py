"""Example command processor for smts_server (not SMTS itself).

Everything at module level runs once, when the server starts — this is where
a real app puts its expensive imports and initialisation. ``main(req)`` then
runs per invocation.

Commands use ``req.stdout``/``req.stderr``/``req.path()``/``req.env`` so they
behave the same in both server modes. (print() without file= also works:
in thread mode sys.stdout is routed per request.)

Each ``cmd_*`` function has the signature ``(req, args) -> int``: the
:class:`smts_server.Request` and the parsed ``argparse.Namespace``, returning
the exit status.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # only for annotations; the server runs as __main__
    from smts_server import Request

#: Server start time (module import time); reported by ``info``.
_STARTED = time.time()
#: In-memory state for ``counter``; persists in thread mode, resets in fork mode.
_counter = 0
_counter_lock = threading.Lock()


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all example commands.

    Called once at import time; ``parse_args`` does not mutate the parser,
    so the single instance is safe to share between request threads.

    Returns:
        A parser with one required subcommand per ``cmd_*`` function.
    """
    p = argparse.ArgumentParser(prog="smts", description="Example commands served by a warm Python process.")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("echo", help="print the arguments")
    s.add_argument("-n", action="store_true", help="omit the trailing newline")
    s.add_argument("words", nargs="*")

    sub.add_parser("pwd", help="print the client's working directory")

    s = sub.add_parser("ls", help="list a directory (relative to the client's cwd)")
    s.add_argument("-a", "--all", action="store_true", help="include dotfiles")
    s.add_argument("path", nargs="?", default=".")

    s = sub.add_parser("wc", help="count lines, words and bytes of files or stdin")
    s.add_argument("files", nargs="*")

    s = sub.add_parser("getenv", help="print a variable from the client's environment")
    s.add_argument("name")

    s = sub.add_parser("sleep", help="sleep, honouring Ctrl-C")
    s.add_argument("seconds", type=float)

    sub.add_parser("counter", help="increment an in-memory counter (shows persistent server state)")
    sub.add_parser("info", help="details of the process running the command")
    return p


#: The shared parser, built once when the server imports this module.
PARSER = _build_parser()


def cmd_echo(req: Request, args) -> int:
    """``echo [-n] WORDS...``: print the words separated by spaces.

    Returns:
        Always 0.
    """
    print(" ".join(args.words), end="" if args.n else "\n", file=req.stdout)
    return 0


def cmd_pwd(req: Request, args) -> int:
    """``pwd``: print the *client's* working directory.

    In thread mode this differs from the server process's own cwd (``/``);
    that difference is the point of the example.

    Returns:
        Always 0.
    """
    print(req.cwd, file=req.stdout)
    return 0


def cmd_ls(req: Request, args) -> int:
    """``ls [-a] [PATH]``: list a directory, sorted, directories marked ``/``.

    ``PATH`` is resolved with :meth:`Request.path`, i.e. relative to the
    client's cwd, which is the pattern to follow in thread mode.

    Returns:
        0 on success, 1 if the directory cannot be read.
    """
    target = req.path(args.path)
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name)
    except OSError as e:
        print(f"smts ls: {args.path}: {e.strerror}", file=req.stderr)
        return 1
    for entry in entries:
        if args.all or not entry.name.startswith("."):
            print(entry.name + ("/" if entry.is_dir() else ""), file=req.stdout)
    return 0


def _count(data: bytes) -> tuple[int, int, int]:
    """Count like ``wc``: newlines, whitespace-separated words, bytes.

    Args:
        data: The raw contents.

    Returns:
        ``(lines, words, bytes)``.
    """
    return data.count(b"\n"), len(data.split()), len(data)


def cmd_wc(req: Request, args) -> int:
    """``wc [FILES...]``: count lines, words and bytes.

    With no files it reads the client's stdin, so ``... | smts wc`` works:
    the stdin fd was passed from the client, not proxied.

    Returns:
        0 on success, 1 if any file could not be read (the others are still
        counted, like ``wc``).
    """
    rc = 0
    if not args.files:
        lines, words, size = _count(req.stdin.buffer.read())
        print(f"{lines:8} {words:8} {size:8}", file=req.stdout)
        return 0
    for name in args.files:
        try:
            with open(req.path(name), "rb") as f:
                lines, words, size = _count(f.read())
        except OSError as e:
            print(f"smts wc: {name}: {e.strerror}", file=req.stderr)
            rc = 1
            continue
        print(f"{lines:8} {words:8} {size:8} {name}", file=req.stdout)
    return rc


def cmd_getenv(req: Request, args) -> int:
    """``getenv NAME``: print a variable from the client's environment.

    Reads :attr:`Request.env`, not ``os.environ`` (which in thread mode is
    the server's environment).

    Returns:
        0 if the variable is set, 1 otherwise (prints nothing), like
        ``printenv``.
    """
    value = req.env.get(args.name)
    if value is None:
        return 1
    print(value, file=req.stdout)
    return 0


def cmd_sleep(req: Request, args) -> int:
    """``sleep SECONDS``: wait, aborting promptly on Ctrl-C in either mode.

    Shows the cancellation pattern for long-running commands: wait on
    ``req.cancelled`` in short slices instead of ``time.sleep``.

    Returns:
        0 when the time is up.

    Raises:
        KeyboardInterrupt: When interrupted; the server turns this into
            exit status 130.
    """
    deadline = time.monotonic() + args.seconds
    while (left := deadline - time.monotonic()) > 0:
        # Fork mode: Ctrl-C arrives as a real SIGINT -> KeyboardInterrupt.
        # Thread mode: it sets req.cancelled, which wakes this wait.
        if req.cancelled.wait(min(left, 0.5)):
            raise KeyboardInterrupt
    return 0


def cmd_counter(req: Request, args) -> int:
    """``counter``: increment and print a module-level counter.

    Demonstrates the main difference between the modes: in thread mode the
    value keeps growing across invocations; in fork mode each command
    increments its own copy-on-write copy, so it always prints 1.

    Returns:
        Always 0.
    """
    global _counter
    with _counter_lock:
        _counter += 1
        value = _counter
    print(f"counter = {value} (pid {os.getpid()})", file=req.stdout)
    return 0


def cmd_info(req: Request, args) -> int:
    """``info``: show which process and thread ran the command, and where.

    Useful for seeing the modes side by side: thread mode reports the
    server's pid with process cwd ``/``; fork mode reports a fresh child pid
    whose cwd equals the client's.

    Returns:
        Always 0.
    """
    print(f"pid           {os.getpid()}", file=req.stdout)
    print(f"thread        {threading.current_thread().name}", file=req.stdout)
    print(f"process cwd   {os.getcwd()}", file=req.stdout)
    print(f"client cwd    {req.cwd}", file=req.stdout)
    print(f"stdout isatty {req.stdout.isatty()}", file=req.stdout)
    print(f"warm for      {time.time() - _STARTED:.1f}s", file=req.stdout)
    return 0


#: Subcommand name → handler.
COMMANDS = {
    "echo": cmd_echo, "pwd": cmd_pwd, "ls": cmd_ls, "wc": cmd_wc,
    "getenv": cmd_getenv, "sleep": cmd_sleep, "counter": cmd_counter, "info": cmd_info,
}


def main(req: Request) -> int:
    """Entry point called by the server for every invocation.

    Parses ``req.argv[1:]`` and dispatches to the matching ``cmd_*``.
    argparse's usual behaviour carries over unchanged: ``--help`` prints to
    the client and raises ``SystemExit(0)``, a usage error raises
    ``SystemExit(2)``; the server maps both to exit statuses.

    Args:
        req: The client's request.

    Returns:
        The command's exit status.
    """
    args = PARSER.parse_args(req.argv[1:])   # exits with 2 / prints help like any CLI
    return COMMANDS[args.command](req, args)
