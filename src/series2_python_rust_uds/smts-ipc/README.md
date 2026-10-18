# smts-ipc — warm Python server + thin Rust client

`smts` is a ~400 KB Rust binary. It sends argv, cwd and environment, plus
**its own stdin/stdout/stderr file descriptors**, to a long-running Python
process over a Unix domain socket. It then exits with the status the
server reports. Python startup and imports are paid once, when the server starts.

```
shell ──exec──▶ smts (Rust)  ──UDS: request + SCM_RIGHTS[0,1,2]──▶ smts_server.py
                  │  ◀────────────── 'X' exit status ──────────────── │
                  └─ Ctrl-C → 'K' signal frame ─────────────────────▶ │
                                    command writes directly to the user's tty/pipe
```

Passing the fds instead of copying output through the socket means that
`isatty()`, colours, progress bars, `| head`, `< file` and `> file` all behave
as if Python ran in the user's shell.

## Layout

```
client/            Rust client (only dependency: libc)
server/
  smts_server.py   server: socket, protocol, thread/fork execution
  example_app.py   example command processor (argparse subcommands)
  bench.py         small latency benchmark (fallback when hyperfine is missing)
Makefile           build / start / stop / demo / test / bench
test.sh            end-to-end test of both modes + auto-start
ARCHITECTURE.md    how it works: protocol, lifecycle, modes, signals, fd ownership
```

## Quick start

```sh
make                       # list targets
make start demo stop       # build, start server in background, run all examples, stop
make run ARGS="ls -a"      # one command through the client
make restart MODE=fork     # switch execution mode
make test                  # end-to-end tests (own socket, doesn't touch a running server)
make bench                 # warm client vs cold python3 (uses hyperfine if installed)
```

Or by hand:

```sh
cd client && cargo build --release && cd ..
python3 server/smts_server.py serve --app example_app:main          # terminal 1

./client/target/release/smts --help                                 # terminal 2
./client/target/release/smts ls -a
printf 'a b\nc\n' | ./client/target/release/smts wc
./client/target/release/smts counter; ./client/target/release/smts counter
python3 server/smts_server.py status
python3 server/smts_server.py stop
```

**Auto-start:** if nobody is listening and `SMTS_SERVER_CMD` is set, the client
starts the server detached (`setsid`, output to `<socket>.log`) and waits for it:

```sh
export SMTS_SERVER_CMD="python3 /path/to/server/smts_server.py serve --app example_app:main"
```

**Socket path:** `$SMTS_SOCKET`, otherwise `$XDG_RUNTIME_DIR` or `$TMPDIR` or `/tmp`,
plus `/smts-<uid>.sock`. On macOS `$TMPDIR` is a private per-user directory
and stays well under the 104-byte `sun_path` limit.

## Writing the real app

The processor is any callable `app(req) -> int | None`, given as `--app module:callable`:

```python
# heavy imports / initialisation at module level: runs once, at server start
import numpy as np

def main(req):               # req: smts_server.Request
    ...                      # req.argv, req.cwd, req.env, req.stdin/stdout/stderr,
    return 0                 # req.path("rel/file"), req.cancelled, req.check_cancelled()
```

`sys.exit()`, argparse errors and `--help`, `KeyboardInterrupt` (130),
`BrokenPipeError` (141) and uncaught exceptions (traceback goes to the
client's stderr, status 1) all map to the usual CLI exit statuses.

## Execution modes: a trade-off

| | `--mode thread` (default) | `--mode fork` |
|---|---|---|
| In-memory state (caches, models) | **persists** between commands | each command gets a copy; changes are lost |
| `os.getcwd()`, `os.environ`, relative `open()` | **server's** (cwd is `/`), so use `req.cwd`/`req.path()`/`req.env` | **client's**: ordinary CLI code just works |
| `sys.stdout` / `print()` | routed per thread (works) | real fds 0/1/2 |
| Ctrl-C | sets `req.cancelled`; the command must poll it | real `SIGINT`, so `KeyboardInterrupt` anywhere |
| Concurrent commands | share the GIL | separate processes |
| Crash isolation | a segfault or `os._exit` kills the server | only the child dies |
| Safe with threads / CUDA / ObjC frameworks loaded | **yes** | **no**: fork-without-exec after threads, CUDA, or macOS frameworks (Accelerate, PyObjC) can deadlock or crash |
| Per-call overhead (Linux VM, `echo`) | ~2.7 ms | ~7.7 ms |

For reference, a cold `python3 -c 'import argparse'` costs ~28 ms on the same
machine. Real apps with heavy imports gain much more.

Thread mode is the default because it is the one that stays safe once the
app loads numpy/torch/PyObjC and benefits from warm caches. Fork mode is
better when commands are ordinary CLI code that relies on process-global state.

## Protocol (big-endian)

```
frame   = u32 len, payload
request = "SMTS" u8 version=1 u8 kind
          kind 'R': u32 argc {u32 len, bytes}*  u32 len, cwd  u32 envc {u32 len, "K=V"}*
                    + SCM_RIGHTS [stdin, stdout, stderr] on the first sendmsg()
          kind 'S' stop, 'P' status
client → server while running:  'K' i32 signum
server → client:                'X' i32 status | 'E' message | 'I' json (status)
```

Arguments, cwd and environment are raw bytes, decoded with `os.fsdecode`
(surrogateescape), exactly like Python's own `sys.argv`.

## Security

- The socket is created with mode `0600` (umask set before `bind`, so there
  is no window where it has wider permissions).
- Every connection's peer uid is checked: `SO_PEERCRED` on Linux,
  `getpeereid()` on macOS/BSD. Connections from other uids are rejected.
- A `flock`ed `<socket>.lock` prevents two servers and makes stale-socket
  cleanup race-free.

## Limitations

- **Unix only.** fd passing (`SCM_RIGHTS`) does not exist on Windows
  `AF_UNIX`. A Windows port would have to stream stdin/stdout/stderr as frames
  over named pipes or TCP instead.
- The command has no controlling terminal: `/dev/tty`, `getpass` and job
  control (Ctrl-Z) do not reach it. Ctrl-Z suspends only the client.
- Code changes need a server restart (`smts_server.py stop`; auto-start brings it back).
- The client sends the physical cwd (`getcwd`). The logical one is in `req.env["PWD"]`.
