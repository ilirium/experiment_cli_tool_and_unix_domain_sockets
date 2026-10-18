# CLAUDE.md

Warm Python server plus thin Rust client (`smts`) over a Unix domain socket,
passing stdio fds with SCM_RIGHTS. This is the foundation for the SMTS CLI.

Read before changing anything:
- **IMPLEMENTATION_NOTES.md**: status, decision log, invariants, gotchas, backlog.
- **ARCHITECTURE.md**: protocol, request lifecycle, modes, signals, fd ownership.

Commands:
- `make test`: end-to-end tests (both server modes + auto-start). Must pass.
- `cd client && cargo clippy --release`: expect 0 warnings.
- `cd client && RUSTDOCFLAGS="-D warnings" cargo doc --document-private-items --no-deps`
- `make start demo stop`: manual smoke run.

Rules:
- Keep the socket-path rule and protocol version identical in
  `client/src/main.rs` and `server/smts_server.py` (notes §3).
- Keep the server event loop single-threaded (fork-mode safety).
- Stay compatible with GNU Make 3.81 and BSD userland (macOS is the target;
  so far it has only been tested on Linux).
- Update docstrings/rustdoc and ARCHITECTURE.md together with behaviour changes.
