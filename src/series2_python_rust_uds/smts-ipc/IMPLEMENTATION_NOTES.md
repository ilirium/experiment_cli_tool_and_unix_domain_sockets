# Implementation notes

Working notes for whoever picks this project up next, human or AI coding
assistant. README.md says how to use it and ARCHITECTURE.md says how it works;
this file records **where things stand, why decisions were made, what was
learned the hard way, and what to do next**.

Last updated: 2026-10-18.

---

## 1. Purpose and status

**Goal.** SMTS is a Python CLI tool. Starting Python and importing its
modules on every invocation is slow, so it is split into:

- a **warm Python server** that imports everything once and listens on a Unix
  domain socket;
- a **tiny Rust client**, `smts`, which the user actually runs. It forwards
  argv, cwd and environment, plus its stdin/stdout/stderr file descriptors,
  and exits with the server's status.

**Status: working prototype.** The server and client are generic. The command
processor is an *example* (`server/example_app.py`), not SMTS itself; the
real SMTS commands are not written yet.

| Area | State |
|---|---|
| Rust client | done; builds without warnings; clippy clean; rustdoc clean with `-D warnings` |
| Python server, thread mode | done, tested |
| Python server, fork mode | done, tested |
| Auto-start, single-instance lock, stop/status | done, tested |
| Docs: README, ARCHITECTURE, docstrings, rustdoc | done |
| `make test` (33 checks: 15 per mode + 3 auto-start) | passing on Linux |
| **Tested on macOS** | **NO.** Everything was developed and tested on a Linux VM. macOS-specific code paths (`getpeereid` via ctypes, kqueue, BSD `sendmsg` types, `$TMPDIR` socket path) are written but unverified. **First thing to do on a Mac: `make test`.** |
| Real SMTS commands | not started |

**Environment it was built and tested in:** Linux x86_64 (kernel 6.18),
Python 3.13.16, Rust/cargo 1.97.0, `libc` crate 0.2.191. The minimum Python
is 3.9 (needs `socket.recv_fds`, `os.waitstatus_to_exitcode`). The Rust code
uses C-string literals (`c"..."`), so Rust ≥ 1.77.

## 2. File map

```
client/Cargo.toml          libc is the only dependency; release profile: lto, panic=abort, strip
client/src/main.rs         the whole client (~470 lines, mostly docs)
server/smts_server.py      server: protocol, auth, event loop, thread/fork modes, CLI
server/example_app.py      example processor: argparse subcommands echo/pwd/ls/wc/getenv/sleep/counter/info
server/bench.py            latency benchmark (fallback for hyperfine)
test.sh                    end-to-end tests, uses its own socket ($TMPDIR/smts-test-<pid>.sock)
Makefile                   build/serve/start/stop/restart/status/logs/run/demo/test/bench/clean/dist
README.md                  usage
ARCHITECTURE.md            design, protocol, lifecycle, signals, fd ownership, security
IMPLEMENTATION_NOTES.md    this file
CLAUDE.md                  short orientation for AI coding assistants (points here)
```

## 3. Invariants — things that must stay in sync

Breaking one of these does not fail at compile time. It produces
"no server listening" or a protocol error at run time.

1. **Socket path rule**, implemented twice:
   - `main.rs::socket_path`
   - `smts_server.py::default_socket_path`

   The rule: `$SMTS_SOCKET`, else the first non-empty of `$XDG_RUNTIME_DIR`,
   `$TMPDIR`, `/tmp`, joined with `smts-<uid>.sock`. Empty variables count as
   unset; the Python version relies on `or`, which treats `""` as false.
2. **Protocol version:** `main.rs::PROTOCOL_VERSION` == `smts_server.py::VERSION`.
   Bump both whenever any frame layout changes.
3. **Frame layouts** are described in three places. Update all three
   together:
   - the code: `build_request` / `_parse_request`, `read_frame` / `_reply`;
   - the `main.rs` module doc comment;
   - ARCHITECTURE.md §3.
4. **Exactly three fds** per `R` request (0, 1, 2), attached to the *first*
   `sendmsg`. The server reads them with `recv_fds(conn, 65536, 3)` on its
   first read.
5. **Exit-status conventions:**
   - 69: client cannot reach or start a server.
   - 70: protocol or I/O error, or an `E` reply.
   - 130: SIGINT.
   - 141: broken pipe.
   - 128+N: killed by signal N.
   - 2: argparse usage error.

   `test.sh` asserts several of these.
6. **The server event loop must stay single-threaded** (workers report back
   through `_done` plus `_wake`). This is what makes `fork()` safe in fork
   mode.
7. **The fork-mode parent closes its copies of the client fds right after
   `fork()`.** If it didn't, `smts x | head` would never see EOF.

## 4. Decision log

Each entry gives the decision, why it was made, and what was rejected. Most are
design trade-offs rather than facts; the other side is recorded so the decision
can be revisited.

### D1. Pass fds with SCM_RIGHTS instead of streaming stdout/stderr frames
- **Why:** with real fds, `isatty()`, colours, progress bars, `| head` (real
  `EPIPE`), `< file` and `> file` all just work, with no extra copy and no
  output-ordering problems between stdout and stderr.
- **Rejected:** framed stdout/stderr/stdin over the socket, as nailgun does.
  It is portable to Windows, but needs a stdin pump, loses tty semantics, and
  interleaves badly.
- **Cost:** Unix only. Windows `AF_UNIX` has no ancillary data (see §7).

### D2. AF_UNIX `SOCK_STREAM`, not `SOCK_DGRAM` and not TCP
- `SOCK_DGRAM`: the request (with the environment) often exceeds macOS's
  default 2 KB datagram size (`net.local.dgram.maxdgram`); there is no
  connection whose closure tells us the client died; replies need a bound
  client address.
- TCP loopback: no fd passing, no kernel-verified peer uid, and a port to
  manage.
- `SOCK_SEQPACKET` would be ideal on Linux but does not exist for AF_UNIX on
  macOS.

### D3. Custom length-prefixed binary protocol with raw bytes, not JSON
- argv, paths and environment values on Unix are arbitrary bytes, and JSON
  needs Unicode. The server decodes with `os.fsdecode` (surrogateescape),
  which is exactly how CPython builds `sys.argv`.
- No serde dependency keeps the client small and quick to build.

### D4. Rust client depends only on `libc`
- `std::os::unix::net` lacks stable `sendmsg` with ancillary data
  (`SocketAncillary` is unstable), so `libc` is needed for `sendmsg`, the
  `CMSG_*` macros, `sigwait`, `setsid` and `getuid`. `nix` was not used: it
  is heavier, and the unsafe surface here is small.

### D5. Two execution modes; thread is the default
- **thread:** warm state persists; safe after threads, CUDA or ObjC
  frameworks are loaded. But commands must use `req.cwd`, `req.env` and
  `req.path()`, and Ctrl-C is cooperative (`req.cancelled`).
- **fork:** ordinary CLI code works (real chdir, environment, fds 0-2, real
  SIGINT), and crashes are isolated. But state changes are lost, and fork
  without exec is unsafe after threads, CUDA, or macOS frameworks (PyObjC,
  Accelerate-backed numpy).
- Thread is the default because the target user runs on macOS with an ML/CV
  stack and PyObjC in the picture, and because warm caches are the reason
  for having a server at all.

### D6. In thread mode, sys.std* are per-thread proxies (`_RoutedStream`)
- They let `print()`, argparse help/errors and `logging` to `sys.stderr`
  reach the right client without changing app code. They are implemented
  with `__getattr__` and a thread-local.
- Known gap: code that checks `isinstance(sys.stdout, io.TextIOBase)` sees
  the proxy.

### D7. Server `chdir("/")` at startup
- In thread mode, a command that wrongly uses a relative path then fails
  loudly instead of silently reading whatever directory the server was
  started from.

### D8. Signals: the client blocks them and uses a `sigwait` thread
- There is no code in a signal handler, so async-signal-safety is not an
  issue. Signals become `K` frames.
- The third signal makes the client exit on its own, so a stuck server can
  never trap the user.
- **Ordering matters:** block signals only after auto-start; otherwise the
  spawned server inherits a blocked mask and cannot be stopped with
  SIGTERM.

### D9. Single instance via `flock` on `<socket>.lock`
- The kernel drops the lock however the process dies, so there is no stale
  lock to clean up. Holding it makes unlinking a stale socket file race-free.
- A loser of the auto-start race exits **0** (`AlreadyRunning`), and the
  client keeps polling for the winner.

### D10. Peer-uid check on every connection
- `SO_PEERCRED` on Linux; `getpeereid()` via ctypes on macOS/BSD, since
  CPython does not wrap it. Together with the 0600 socket (umask set before
  bind), only the same user can run commands, which matters because commands
  run with the server's privileges on the caller's fds.

### D11. Considered and not adopted: ZeroMQ
- It hides fd passing and peer credentials, adds a native dependency, and on
  Windows falls back to TCP anyway. libzmq's last release is 4.3.5 (Oct
  2023), and pyzmq 24+ Windows wheels ship without AF_UNIX support.
- It is a good fit for pub/sub or worker topologies, not for one-shot
  CLI-to-server RPC. If a messaging library is ever needed, look at **nng /
  pynng** first: as far as we know its Windows `ipc://` uses named pipes.

## 5. Gotchas found during implementation

Each of these cost time once. Check this list before "fixing" something that
looks odd.

**Rust client**
- The `cmsghdr` buffer must be aligned, so it is backed by `Vec<u64>`, not
  `Vec<u8>`.
- `msghdr.msg_iovlen`, `msg_controllen` and `cmsghdr.cmsg_len` have
  **different integer types on Linux and macOS**, hence the `as _` casts. Do
  not "fix" them into concrete types.
- `ensure_std_fds`: a client started with fd 1 closed would otherwise send an
  unrelated fd as "stdout", or fail with `EBADF`.
- The rustdoc in the module header must keep the wire-format sketch inside a
  ```` ```text ```` fence. Otherwise `payload[length]` and `argv[0]` are
  parsed as broken intra-doc links and `cargo doc -D warnings` fails.
- `std::process::exit` is used deliberately: the forwarder thread may still
  be blocked in `sigwait`.

**Python server**
- `socket.recv_fds` must be the **first** read on a new connection: the
  ancillary data arrives with the first `sendmsg` chunk. Check `MSG_CTRUNC`
  and close fds on every error path.
- `signal.set_wakeup_fd` only fires if a **Python-level handler** is
  installed. `SIGCHLD`'s default disposition is "ignore", so a no-op lambda
  handler is installed on purpose.
- Several SIGCHLDs can coalesce into one wakeup, so `_reap_children` loops on
  `waitpid(-1, WNOHANG)`.
- Fork-mode child: do **not** close the selector. A kqueue fd is not
  inherited across `fork()` on macOS, so its number may belong to something
  else in the child.
- Fork-mode child: flush the server's stdio *before* `fork()` (otherwise
  buffered output is duplicated), and leave the child with `os._exit()` (so
  inherited `atexit` handlers do not run).
- `example_app.py` imports `Request` only under `TYPE_CHECKING`. The server
  runs as `__main__`, so a real `import smts_server` would execute the module
  a second time and create a second `_local`.
- argparse resolves `sys.stdout` and `sys.stderr` at call time, which is why
  the thread-mode proxies work for `--help` and usage errors.
- `sun_path` limit: 104 bytes on macOS, 108 on Linux. `$TMPDIR` on macOS is
  about 50 chars, which is fine for `smts-<uid>.sock`, but deep test temp
  directories can exceed it.

**Tooling and macOS portability**
- macOS ships **GNU Make 3.81**: no `.ONESHELL`, no `!=` assignment, no
  `$(file …)`. The Makefile keeps to 3.81 features. Also, `MODE ?= thread  #
  comment` keeps the trailing spaces in the value, hence `$(strip $(MODE))`.
- macOS BSD `date` has no `%N`. Timing in `test.sh` therefore goes through
  `bench.py`.
- `bench.py` measures `subprocess.run` round trips, so its numbers include
  fork/exec of the client itself, and they vary with machine load (2.5 ms
  idle vs. 6.7 ms under the test's load).

## 6. How to verify a change

```sh
make test                                   # 33 end-to-end checks, both modes + auto-start
cd client && cargo clippy --release         # expect 0 warnings
RUSTDOCFLAGS="-D warnings" cargo doc --document-private-items --no-deps
make start demo stop                        # eyeball the output
```

When editing **only documentation**, the following check proved useful:
- Python: compare `ast.dump()` of each file with docstrings stripped (and a
  body left empty, or only `pass`, normalised to `[Pass]`) against the
  original.
- Rust: compare the source with blank lines and `//`-comment lines removed.

To test on a **real terminal** (for `isatty` and colours), run
`script -qc "./client/target/release/smts info" /dev/null` on Linux; the macOS
`script` syntax differs (`script -q /dev/null cmd`). Fork mode should report
`stdout isatty True` and process cwd equal to the client's cwd.

**Measured performance** (Linux VM, `smts echo x`, from `make bench`):

| Mode | Per call |
|---|---|
| thread | ~2.5 ms |
| fork | ~7.5 ms |
| cold `python3 -c 'import argparse'` | ~22 ms |

Expect a much larger gap once SMTS's real imports are in place.

## 7. Background research (as of 2026-10-18)

- **AF_UNIX on Windows (Python):**
  - The Windows kernel has had AF_UNIX since Windows 10 1803, but only
    `SOCK_STREAM`: no DGRAM or SEQPACKET, **no ancillary data** (no
    SCM_RIGHTS), no abstract namespace, and peer identity only as a pid via
    `SIO_AF_UNIX_GETPEERPID`.
  - CPython does not expose `socket.AF_UNIX` on Windows: issue gh-77589 is
    open, and PR gh-137420 (socket support only, no asyncio) was still
    unmerged as far as we could tell.
  - **Consequence:** this design (D1) cannot be ported to Windows. A Windows
    version would use named pipes (`multiprocessing.connection` with
    `AF_PIPE`, or pywin32) and stream stdio as frames.
- **ZeroMQ:** see D11.
- **macOS AF_UNIX specifics:**
  - The default datagram size limit is small: `net.local.dgram.maxdgram` is
    2048, and stream buffers are about 8 KB.
  - `LOCAL_PEERPID` exists, but Python has no constant for it.
  - Socket file permissions are honoured.

## 8. Backlog / next steps (in rough priority order)

1. **Run `make test` on macOS** and fix whatever the Linux-only testing
   missed.
2. **Start SMTS proper:** create `smts_app.py` with heavy imports at module
   level and `main(req)`, run it with `--app smts_app:main`. Follow the
   `example_app.py` patterns: `req.path()`, `req.env`, `print(...,
   file=req.stdout)`, and poll `req.cancelled` in long loops.
3. **Fix head-of-line blocking in `Server._accept`.** The request is read
   synchronously, with up to `REQUEST_TIMEOUT` (5 s) blocking. Register new
   connections with the selector and assemble requests incrementally, like
   `_on_client_readable`.
4. **Stale-server detection:** have the `P` (status) reply include a code
   version or mtime, and let the client restart the server when the Python
   sources change (today you run `make restart`).
5. **Job control:** forward `SIGTSTP`/`SIGCONT` (and maybe `SIGWINCH`) as `K`
   frames. Today Ctrl-Z suspends only the client.
6. **Optional:** move `server/bench.py` to `tools/` (it is dev tooling, not
   server code). The Makefile and `test.sh` reference the current path.
7. **Packaging:** a `pyproject.toml` for the server, and `cargo install` /
   Homebrew for the client.

## 9. Conventions used in this codebase

- Python: Google-style docstrings on everything, `from __future__ import
  annotations`, stdlib only on the server side.
- Rust: `///` on every item with `# Errors` sections, `//!` module docs,
  `unsafe` kept to FFI blocks.
- Docs cite code as `file::symbol` so it can be grepped. Keep ARCHITECTURE.md
  in sync when behaviour changes.
- Design trade-offs are labelled as trade-offs, with both sides, in the code
  docs and markdown alike.
