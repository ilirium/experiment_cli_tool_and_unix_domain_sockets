#!/usr/bin/env bash

# Stage: Strict mode options
# Bash "strict mode", to help catch problems and bugs in the shell
# script. Every bash script you write should include this. See
# http://redsymbol.net/articles/unofficial-bash-strict-mode/ for
# details.
# More details about "E" and "x":
# https://vaneyckt.io/posts/safer_bash_scripts_with_set_euxo_pipefail/
set -eu -o pipefail

# Start Python as a coprocess
coproc PYTHON { uv run --no-sync --script server.py; }

# Save file descriptors
exec {PY_IN}>&"${PYTHON[1]}"
exec {PY_OUT}<&"${PYTHON[0]}"

# Define CLI function
pycmd() {
    printf '%s\n' "$*" >&"$PY_IN"

    local result
    IFS= read -r result <&"$PY_OUT"

    printf '%s\n' "$result"
}