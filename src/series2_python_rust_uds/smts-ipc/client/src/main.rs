//! `smts` — thin client for a warm Python command server.
//!
//! It does almost nothing on purpose:
//!   1. connect to the server's Unix domain socket (auto-starting it if
//!      `SMTS_SERVER_CMD` is set and nobody is listening);
//!   2. send one request frame — argv, cwd, environment — together with its
//!      own stdin/stdout/stderr file descriptors (SCM_RIGHTS);
//!   3. forward SIGINT/SIGTERM/SIGHUP/SIGQUIT to the server as `K` frames;
//!   4. wait for the `X` (exit status) frame and exit with that status.
//!
//! Because the server receives the real fds, the command writes straight to
//! the user's terminal/pipe/file: isatty(), colours, progress bars, `| head`
//! and stdin redirection all behave as if the command ran in-process here.
//!
//! Wire format (all integers big-endian):
//!
//! ```text
//! frame    = u32 length, payload[length]
//! request  = "SMTS" u8 version u8 kind ('R')
//!            u32 argc { u32 len, bytes }*          -- argv incl. argv[0]
//!            u32 len, bytes                        -- cwd
//!            u32 envc { u32 len, "KEY=VALUE" }*    -- environment
//! client → server, later:  'K' i32 signal
//! server → client:         'X' i32 exit status  |  'E' utf-8 message
//! ```
//!
//! Environment variables read by the client:
//! - `SMTS_SOCKET` — socket path override (see [`socket_path`]);
//! - `SMTS_SERVER_CMD` — shell command that starts the server when none is
//!   listening (see [`connect_or_start`]).
//!
//! See ARCHITECTURE.md for the full design.

use std::env;
use std::ffi::OsString;
use std::fs::OpenOptions;
use std::io::{self, ErrorKind, Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// Every request payload starts with these bytes.
const MAGIC: &[u8; 4] = b"SMTS";
/// Wire-protocol version; must equal `VERSION` in `smts_server.py`.
const PROTOCOL_VERSION: u8 = 1;
/// Request kind "run a command" (the server also knows `S` stop, `P` status).
const KIND_RUN: u8 = b'R';
/// Largest response frame accepted; real ones are a few bytes.
const MAX_RESPONSE: usize = 1 << 20;
/// How long [`connect_or_start`] waits for an auto-started server.
const START_TIMEOUT: Duration = Duration::from_secs(30);

/// `sysexits.h`: the server is not running and could not be started.
const EX_UNAVAILABLE: i32 = 69;
/// `sysexits.h`: protocol or I/O failure, or an `E` reply from the server.
const EX_SOFTWARE: i32 = 70;

/// Entry point: run the request and exit with the command's status.
///
/// Errors are printed as `smts: <message>` and mapped to [`EX_UNAVAILABLE`]
/// or [`EX_SOFTWARE`]. `process::exit` is used (rather than returning)
/// because the signal-forwarding thread may still be blocked in `sigwait`.
fn main() {
    ensure_std_fds();
    let code = match run() {
        Ok(code) => code,
        Err(Failure::Unavailable(msg)) => {
            eprintln!("smts: {msg}");
            EX_UNAVAILABLE
        }
        Err(Failure::Io(e)) => {
            eprintln!("smts: {e}");
            EX_SOFTWARE
        }
    };
    std::process::exit(code);
}

/// Why the client could not report a command's exit status.
enum Failure {
    /// No server could be reached or started. Exit [`EX_UNAVAILABLE`].
    Unavailable(String),
    /// Anything else: I/O or protocol errors. Exit [`EX_SOFTWARE`].
    Io(io::Error),
}

/// Lets `?` turn plain I/O errors into [`Failure::Io`].
impl From<io::Error> for Failure {
    fn from(e: io::Error) -> Self {
        Failure::Io(e)
    }
}

/// Perform one invocation end to end.
///
/// Builds the request, connects (or auto-starts the server), sends the
/// request with fds 0/1/2 attached, starts signal forwarding, then waits
/// for the server's reply.
///
/// Returns the command's exit status from the `X` frame, or [`EX_SOFTWARE`]
/// after printing the server's message for an `E` frame.
///
/// # Errors
///
/// [`Failure::Unavailable`] if no server can be reached; [`Failure::Io`] if
/// the cwd is unreadable, sending fails, the server disconnects without an
/// `X` frame, or it sends a malformed frame.
fn run() -> Result<i32, Failure> {
    let path = socket_path();
    let frame = build_request()?;
    let mut sock = connect_or_start(&path)?;

    // The fds ride along with the first chunk; the rest is plain bytes.
    let sent = send_with_fds(&sock, &frame, &[0, 1, 2])?;
    (&sock).write_all(&frame[sent..])?;

    // Only now block signals: the auto-started server (if any) must not
    // inherit a blocked signal mask.
    spawn_signal_forwarder(sock.try_clone()?);

    loop {
        let Some(frame) = read_frame(&mut sock)? else {
            return Err(Failure::Io(io::Error::other(
                "server closed the connection without reporting an exit status",
            )));
        };
        match frame.split_first() {
            Some((b'X', rest)) if rest.len() == 4 => {
                return Ok(i32::from_be_bytes(rest.try_into().unwrap()));
            }
            Some((b'E', msg)) => {
                eprintln!("smts: server: {}", String::from_utf8_lossy(msg));
                return Ok(EX_SOFTWARE);
            }
            _ => return Err(Failure::Io(io::Error::other("malformed frame from server"))),
        }
    }
}

/// Socket location shared with the server.
///
/// `$SMTS_SOCKET` if set, otherwise the first non-empty of
/// `$XDG_RUNTIME_DIR`, `$TMPDIR`, `/tmp`, joined with `smts-<uid>.sock`.
/// Empty variables count as unset. Must match `default_socket_path()` in
/// `smts_server.py`.
fn socket_path() -> PathBuf {
    let non_empty = |k: &str| env::var_os(k).filter(|v| !v.is_empty());
    if let Some(p) = non_empty("SMTS_SOCKET") {
        return PathBuf::from(p);
    }
    let base = non_empty("XDG_RUNTIME_DIR")
        .or_else(|| non_empty("TMPDIR"))
        .unwrap_or_else(|| OsString::from("/tmp"));
    let uid = unsafe { libc::getuid() };
    PathBuf::from(base).join(format!("smts-{uid}.sock"))
}

/// Append `v` as a big-endian `u32`.
///
/// Values above `u32::MAX` would be truncated; the server rejects requests
/// over 16 MiB long before that matters.
fn put_u32(buf: &mut Vec<u8>, v: usize) {
    buf.extend_from_slice(&(v as u32).to_be_bytes());
}

/// Append a length-prefixed byte string (`u32 len` + bytes).
fn put_blob(buf: &mut Vec<u8>, b: &[u8]) {
    put_u32(buf, b.len());
    buf.extend_from_slice(b);
}

/// Encode the complete `R` request frame, length prefix included.
///
/// argv, cwd and environment are taken as raw bytes (`OsStr`), never
/// `String`: on Unix they need not be valid UTF-8, and the server decodes
/// them with `os.fsdecode` exactly as CPython decodes its own `sys.argv`.
/// The cwd is the physical path from `getcwd()`; the shell's logical path
/// is still available to the server as `$PWD`.
///
/// # Errors
///
/// Fails if the current directory cannot be determined (for example it was
/// deleted while the shell was in it). This is checked before connecting,
/// so the server is never involved.
fn build_request() -> io::Result<Vec<u8>> {
    let mut p = Vec::with_capacity(8 * 1024);
    p.extend_from_slice(MAGIC);
    p.push(PROTOCOL_VERSION);
    p.push(KIND_RUN);

    // Raw bytes, not String: argv and paths on Unix need not be UTF-8.
    let args: Vec<OsString> = env::args_os().collect();
    put_u32(&mut p, args.len());
    for a in &args {
        put_blob(&mut p, a.as_bytes());
    }

    // getcwd(): the physical path. The logical one is still in $PWD.
    let cwd = env::current_dir()
        .map_err(|e| io::Error::new(e.kind(), format!("cannot determine current directory: {e}")))?;
    put_blob(&mut p, cwd.as_os_str().as_bytes());

    let vars: Vec<(OsString, OsString)> = env::vars_os().collect();
    put_u32(&mut p, vars.len());
    let mut kv = Vec::new();
    for (k, v) in &vars {
        kv.clear();
        kv.extend_from_slice(k.as_bytes());
        kv.push(b'=');
        kv.extend_from_slice(v.as_bytes());
        put_blob(&mut p, &kv);
    }

    let mut frame = Vec::with_capacity(p.len() + 4);
    put_u32(&mut frame, p.len());
    frame.extend_from_slice(&p);
    Ok(frame)
}

/// True if a `connect()` error means "no server here".
///
/// `ENOENT`: the socket file does not exist. `ECONNREFUSED`: a stale socket
/// file with nobody listening. Both are worth an auto-start; anything else
/// (e.g. `EACCES`) is a real error.
fn is_absent(e: &io::Error) -> bool {
    matches!(e.kind(), ErrorKind::NotFound | ErrorKind::ConnectionRefused)
}

/// Connect to the server, starting it first if allowed and necessary.
///
/// If nobody is listening and `SMTS_SERVER_CMD` is set, runs it as
/// `/bin/sh -c "$SMTS_SERVER_CMD"`, detached: new session (`setsid`), cwd
/// `/`, stdin from `/dev/null`, stdout/stderr appended to `<socket>.log`,
/// and `SMTS_SOCKET` exported so it binds where we look. Then polls
/// `connect()` with exponential back-off (10 ms doubling to 200 ms) for up
/// to [`START_TIMEOUT`].
///
/// If the spawned shell exits with a failure status we give up early. If it
/// exits with success, it most likely lost the startup race to another
/// instance (the server exits 0 on `AlreadyRunning`), so polling continues
/// for the winner.
///
/// # Errors
///
/// [`Failure::Unavailable`] if there is no server and no
/// `SMTS_SERVER_CMD`, the server fails or times out on startup, or
/// `connect()` fails for another reason; [`Failure::Io`] if the log file
/// cannot be opened or the shell cannot be spawned.
fn connect_or_start(path: &Path) -> Result<UnixStream, Failure> {
    match UnixStream::connect(path) {
        Ok(s) => return Ok(s),
        Err(e) if is_absent(&e) => {}
        Err(e) => return Err(Failure::Unavailable(format!("cannot connect to {}: {e}", path.display()))),
    }

    let Some(cmd) = env::var_os("SMTS_SERVER_CMD").filter(|c| !c.is_empty()) else {
        return Err(Failure::Unavailable(format!(
            "no server listening on {} (start one, or set SMTS_SERVER_CMD to auto-start it)",
            path.display()
        )));
    };

    // Detached server: new session, no stdin, output to <socket>.log.
    let log_path = path.with_extension("log");
    let log = OpenOptions::new().create(true).append(true).open(&log_path)?;
    let mut command = Command::new("/bin/sh");
    command
        .arg("-c")
        .arg(&cmd)
        .env("SMTS_SOCKET", path)
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let mut child = command.spawn()?;

    let deadline = Instant::now() + START_TIMEOUT;
    let mut delay = Duration::from_millis(10);
    loop {
        thread::sleep(delay);
        match UnixStream::connect(path) {
            Ok(s) => return Ok(s),
            Err(e) if is_absent(&e) => {}
            Err(e) => return Err(Failure::Unavailable(format!("cannot connect to {}: {e}", path.display()))),
        }
        // A non-zero exit means it crashed; a zero exit usually means another
        // instance won the startup race, so keep polling for that one.
        if let Ok(Some(status)) = child.try_wait() {
            if !status.success() {
                return Err(Failure::Unavailable(format!(
                    "server failed to start ({status}); see {}",
                    log_path.display()
                )));
            }
        }
        if Instant::now() >= deadline {
            return Err(Failure::Unavailable(format!(
                "server did not come up within {}s; see {}",
                START_TIMEOUT.as_secs(),
                log_path.display()
            )));
        }
        delay = (delay * 2).min(Duration::from_millis(200));
    }
}

/// `sendmsg(2)` with an `SCM_RIGHTS` control message carrying `fds`.
///
/// The fds are attached to this call only, so the server must collect them
/// with its first read (`socket.recv_fds`); the caller sends any unsent tail
/// of `data` with a plain `write_all`. The kernel installs *duplicates* of
/// the fds in the server, pointing to the same open file descriptions (same
/// terminal, pipe or file offset).
///
/// Implementation notes: the control buffer is backed by `u64` so that the
/// `cmsghdr` is properly aligned; `msg_iovlen`, `msg_controllen` and
/// `cmsg_len` have different integer types on Linux and macOS, hence the
/// `as _` casts. Retries on `EINTR`.
///
/// Returns the number of bytes of `data` sent (possibly fewer than
/// `data.len()`).
///
/// # Errors
///
/// Any `sendmsg` error other than `EINTR`, e.g. `EBADF` for an invalid fd
/// (prevented by [`ensure_std_fds`]) or `EPIPE` if the server went away.
fn send_with_fds(sock: &UnixStream, data: &[u8], fds: &[libc::c_int]) -> io::Result<usize> {
    unsafe {
        let fd_bytes = std::mem::size_of_val(fds) as libc::c_uint;
        let space = libc::CMSG_SPACE(fd_bytes) as usize;
        // u64 backing store keeps the cmsghdr suitably aligned.
        let mut cbuf = vec![0u64; space.div_ceil(8)];

        let mut iov = libc::iovec {
            iov_base: data.as_ptr() as *mut libc::c_void,
            iov_len: data.len(),
        };
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = cbuf.as_mut_ptr() as *mut libc::c_void;
        msg.msg_controllen = space as _;

        let cmsg = libc::CMSG_FIRSTHDR(&msg);
        (*cmsg).cmsg_level = libc::SOL_SOCKET;
        (*cmsg).cmsg_type = libc::SCM_RIGHTS;
        (*cmsg).cmsg_len = libc::CMSG_LEN(fd_bytes) as _;
        std::ptr::copy_nonoverlapping(
            fds.as_ptr() as *const u8,
            libc::CMSG_DATA(cmsg),
            fd_bytes as usize,
        );

        loop {
            let n = libc::sendmsg(sock.as_raw_fd(), &msg, 0);
            if n >= 0 {
                return Ok(n as usize);
            }
            let err = io::Error::last_os_error();
            if err.kind() != ErrorKind::Interrupted {
                return Err(err);
            }
        }
    }
}

/// Read one length-prefixed frame from the server.
///
/// Returns `Ok(None)` on a clean EOF *before* a frame starts (the server
/// closed the connection), and `Ok(Some(payload))` otherwise.
///
/// # Errors
///
/// I/O errors, EOF in the middle of a frame, or a frame larger than
/// [`MAX_RESPONSE`].
fn read_frame(sock: &mut UnixStream) -> io::Result<Option<Vec<u8>>> {
    let mut len = [0u8; 4];
    match sock.read_exact(&mut len) {
        Ok(()) => {}
        Err(e) if e.kind() == ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }
    let n = u32::from_be_bytes(len) as usize;
    if n > MAX_RESPONSE {
        return Err(io::Error::other(format!("response frame too large ({n} bytes)")));
    }
    let mut buf = vec![0u8; n];
    sock.read_exact(&mut buf)?;
    Ok(Some(buf))
}

/// Forward terminating signals to the server instead of dying from them.
///
/// Blocks SIGINT, SIGTERM, SIGHUP and SIGQUIT with `pthread_sigmask` in the
/// calling (main) thread, so the mask is inherited by the new thread and
/// every later one, then receives them *synchronously* with `sigwait()` on a
/// dedicated thread. No code runs in a signal handler, so there are no
/// async-signal-safety concerns. Each signal becomes a `'K' i32 signum`
/// frame on `sock` (a clone of the request connection).
///
/// Escape hatch: the third signal makes the client exit with `128 + signum`,
/// so a server that ignores signals (e.g. a thread-mode command that never
/// checks `req.cancelled`) cannot trap the user. The client also exits if
/// the frame cannot be written (server gone).
///
/// Must be called *after* any auto-start: a child spawned while signals are
/// blocked would inherit the blocked mask.
fn spawn_signal_forwarder(sock: UnixStream) {
    let mut set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut set);
        for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP, libc::SIGQUIT] {
            libc::sigaddset(&mut set, sig);
        }
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
    }
    thread::spawn(move || {
        let mut received = 0;
        loop {
            let mut sig: libc::c_int = 0;
            if unsafe { libc::sigwait(&set, &mut sig) } != 0 {
                continue;
            }
            received += 1;
            if received >= 3 {
                eprintln!("smts: server is not reacting to signals; giving up");
                std::process::exit(128 + sig);
            }
            let mut f = Vec::with_capacity(9);
            put_u32(&mut f, 5);
            f.push(b'K');
            f.extend_from_slice(&sig.to_be_bytes());
            if (&sock).write_all(&f).is_err() {
                std::process::exit(128 + sig);
            }
        }
    });
}

/// Make sure fds 0, 1 and 2 are all open.
///
/// If the client was started with one of them closed (e.g. `smts <&-`),
/// open `/dev/null` in its place. `open()` always returns the lowest free
/// descriptor, so iterating 0..=2 fills the gaps in order. Without this,
/// `sendmsg` would fail with `EBADF`, or worse, an unrelated fd that later
/// took number 1 would be sent as "stdout".
fn ensure_std_fds() {
    for fd in 0..=2 {
        unsafe {
            if libc::fcntl(fd, libc::F_GETFD) == -1 {
                libc::open(c"/dev/null".as_ptr(), libc::O_RDWR); // takes the lowest free fd
            }
        }
    }
}
