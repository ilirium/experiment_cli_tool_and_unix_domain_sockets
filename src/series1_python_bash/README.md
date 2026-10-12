# Zsh Python Client

`client_zsh.zsh` starts `server.py` as a Zsh coprocess and defines `pycmd` to send commands to it. Python remains running between calls, so each command uses the same interpreter.

## Prerequisites

- Zsh (run the commands below in a Zsh shell).
- A Python virtual environment at `.venv`, with any dependencies required by the server installed.
- `server.py` and `client_zsh.zsh` in the current directory.
- A server that reads commands from stdin and returns exactly one newline-terminated response per command on stdout.

If needed, create the virtual environment:

```zsh
python3 -m venv .venv
```

## Usage

From the directory containing both scripts, load the client into your current shell:

```zsh
source ./client_zsh.zsh
```

Then send commands:

```zsh
pycmd sum 10 20 30
pycmd mean 10 20 30
pycmd pid
```

Repeated `pycmd pid` calls should return the same process ID while the server remains running.

## Key caveats

- Source the client once per shell session. Sourcing it again starts another Python process.
- The client launches `.venv/bin/python -u server.py`; paths are relative to the directory where you source it.
- Keep stdout reserved for responses. Send logs and diagnostics to stderr; extra stdout lines can misalign subsequent replies.
- Calls are synchronous and have no timeout: `pycmd` waits for a response. Use commands sequentially from the same shell.
- Arguments are joined into a single text line (`"$*"`); argument boundaries and embedded newlines are not preserved.
- Zsh's coprocess channel is shared within the shell. Starting another coprocess can interfere with this client.
- The client provides no shutdown command. Stop the server using its supported shutdown mechanism, or terminate the PID returned by `pycmd pid` when finished.
