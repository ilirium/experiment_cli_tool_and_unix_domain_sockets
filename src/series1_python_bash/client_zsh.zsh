#!/usr/bin/env zsh

# Start Python as a coprocess
coproc uv run --no-sync --script server.py

# Define CLI function
pycmd() {
    local result

    print -p -- "$*" || return 1

    read -r -p result || return 1

    print -r -- "$result"
}