#!/usr/bin/env bash
# End-to-end smoke test: builds the client, then exercises both server modes.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
SMTS="$ROOT/client/target/release/smts"
SERVER="$ROOT/server/smts_server.py"
export SMTS_SOCKET="${TMPDIR:-/tmp}/smts-test-$$.sock"
fails=0

check() {  # check <name> <expected exit> <expected stdout substring> -- cmd...
  local name="$1" want_rc="$2" want_out="$3"; shift 4
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc == "$want_rc" && $out == *"$want_out"* ]]; then
    printf '  ok    %s\n' "$name"
  else
    printf '  FAIL  %s (rc=%s, want %s)\n%s\n' "$name" "$rc" "$want_rc" "$out" | sed 's/^/        /'
    fails=$((fails + 1))
  fi
}

(cd "$ROOT/client" && cargo build --release -q) || exit 1

for mode in thread fork; do
  echo "== mode: $mode"
  python3 "$SERVER" serve --app example_app:main --mode "$mode" 2>"$SMTS_SOCKET.$mode.log" &
  server_pid=$!
  for _ in $(seq 50); do [[ -S $SMTS_SOCKET ]] && break; sleep 0.1; done

  work="$(mktemp -d)"; printf 'a b\nc\n' >"$work/f.txt"; mkdir "$work/sub"
  check "echo"            0 "hello  wörld"  -- "$SMTS" echo hello "" wörld
  check "pwd is client's" 0 "$work"         -- bash -c "cd '$work' && '$SMTS' pwd"
  check "ls relative"     0 "sub/"          -- bash -c "cd '$work' && '$SMTS' ls"
  check "wc file"         0 "2        3        6 f.txt" -- bash -c "cd '$work' && '$SMTS' wc f.txt"
  check "wc stdin pipe"   0 "3        3"    -- bash -c "printf 'x\ny\nz\n' | '$SMTS' wc"
  check "env forwarded"   0 "42"            -- env SMTS_DEMO=42 "$SMTS" getenv SMTS_DEMO
  check "exit status"     1 ""              -- "$SMTS" getenv NO_SUCH_VAR_HOPEFULLY
  check "argparse error"  2 "invalid choice" -- "$SMTS" frobnicate
  check "help"            0 "usage: smts"   -- "$SMTS" --help
  check "stdout to file"  0 "isatty False"  -- bash -c "'$SMTS' info > '$work/o' && cat '$work/o'"
  check "broken pipe"     0 "1"             -- bash -c "'$SMTS' ls / | head -1 | wc -l"
  "$SMTS" counter >/dev/null
  if [[ $mode == thread ]]; then
    check "state persists" 0 "counter = 2" -- "$SMTS" counter
  else
    check "state resets"   0 "counter = 1" -- "$SMTS" counter
  fi
  check "ctrl-c -> 130"   130 ""            -- bash -c "'$SMTS' sleep 30 & c=\$!; sleep 0.5; kill -INT \$c; wait \$c"
  check "status"          0 "\"mode\": \"$mode\"" -- python3 "$SERVER" status

  python3 "$ROOT/server/bench.py" -n 100 "$SMTS echo x" | sed 's/^/  ..    /'

  check "stop"            0 ""              -- python3 "$SERVER" stop
  wait "$server_pid"
  rm -rf "$work"
done

echo "== auto-start"
export SMTS_SERVER_CMD="python3 '$SERVER' serve --app example_app:main"
check "auto-start"      0 "started"       -- "$SMTS" echo started
check "second call"     0 "again"         -- "$SMTS" echo again
python3 "$SERVER" stop >/dev/null
unset SMTS_SERVER_CMD
check "no server -> 69" 69 "no server listening" -- "$SMTS" echo x

python3 "$ROOT/server/bench.py" -n 20 "python3 -c 'import argparse'" | sed 's/^/  ..    /'

rm -f "$SMTS_SOCKET".* "${SMTS_SOCKET%.sock}.log"
echo; [[ $fails == 0 ]] && echo "all passed" || { echo "$fails failure(s)"; exit 1; }
