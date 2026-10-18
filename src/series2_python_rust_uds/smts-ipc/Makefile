# smts-ipc — build, run and test the warm-server example.
# Compatible with GNU Make 3.81 (the version shipped with macOS).
#
#   make                      list targets
#   make start demo stop      quickest tour
#   make run ARGS="ls -a"     run one client command
#   make start MODE=fork      run the server in fork mode
#   make test                 full end-to-end test of both modes

PYTHON  ?= python3
#PYTHON  ?= uv run python3
CARGO   ?= cargo
MODE    ?= thread               # thread | fork
APP     ?= example_app:main
ARGS    ?= --help

ROOT    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
CLIENT  := $(ROOT)/client/target/release/smts
SERVER  := $(PYTHON) $(ROOT)/server/smts_server.py
SOURCES := $(ROOT)/client/Cargo.toml $(wildcard $(ROOT)/client/src/*.rs)

# Optional: make start SOCKET=/tmp/my.sock — otherwise client and server share
# their built-in default ($XDG_RUNTIME_DIR | $TMPDIR | /tmp)/smts-<uid>.sock.
ifdef SOCKET
export SMTS_SOCKET := $(SOCKET)
endif
SOCK_PATH := $(shell $(PYTHON) -c 'import sys; sys.path.insert(0, "$(ROOT)/server"); \
                import smts_server as s; print(s.default_socket_path())')
LOG := $(basename $(SOCK_PATH)).log

.DEFAULT_GOAL := help
.PHONY: help build debug serve start stop restart status logs run demo test bench clean dist

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | \
	  awk -F':.*## ' '{ printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2 }'
	@echo
	@echo "  Variables: MODE=$(strip $(MODE))  APP=$(APP)  PYTHON=$(PYTHON)  ARGS=\"$(ARGS)\""
	@echo "  Socket:    $(SOCK_PATH)"

# ---------------------------------------------------------------- build ----

build: $(CLIENT) ## Build the Rust client (release)

$(CLIENT): $(SOURCES)
	cd $(ROOT)/client && $(CARGO) build --release

debug: ## Build the Rust client (debug)
	cd $(ROOT)/client && $(CARGO) build

# --------------------------------------------------------------- server ----

serve: ## Run the server in the foreground (Ctrl-C to stop)
	$(SERVER) serve --app $(APP) --mode $(strip $(MODE))

start: ## Start the server in the background (no-op if running)
	@if $(SERVER) status >/dev/null 2>&1; then \
	  echo "server already running on $(SOCK_PATH)"; \
	else \
	  nohup $(SERVER) serve --app $(APP) --mode $(strip $(MODE)) >>"$(LOG)" 2>&1 & \
	  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do \
	    [ -S "$(SOCK_PATH)" ] && break; sleep 0.25; \
	  done; \
	  if [ -S "$(SOCK_PATH)" ]; then echo "server started (mode=$(strip $(MODE))), log: $(LOG)"; \
	  else echo "server did not start; see $(LOG)"; tail -n 20 "$(LOG)"; exit 1; fi; \
	fi

stop: ## Stop the background server
	@$(SERVER) stop && echo "server stopped" || true

restart: stop start ## Restart the server (e.g. after editing Python code)

status: ## Show server status
	@$(SERVER) status

logs: ## Follow the server log
	tail -f "$(LOG)"

# --------------------------------------------------------------- client ----

run: $(CLIENT) ## Run one command: make run ARGS="ls -a"
	@$(CLIENT) $(ARGS)

demo: $(CLIENT) start ## Run every example command against the server
	@set -e; S="$(CLIENT)"; \
	step() { printf '\n\033[1m$$ smts %s\033[0m\n' "$$*"; }; \
	step echo hello world;          $$S echo hello world; \
	step pwd;                       $$S pwd; \
	step ls -a;                     $$S ls -a; \
	step wc README.md Makefile;     $$S wc README.md Makefile; \
	printf '\n\033[1m$$ printf "a b\\\\nc\\\\n" | smts wc\033[0m\n'; \
	                                printf 'a b\nc\n' | $$S wc; \
	step getenv HOME;               $$S getenv HOME; \
	step counter; $$S counter; step counter; $$S counter; \
	step info;                      $$S info; \
	step sleep 0.3;                 $$S sleep 0.3; echo "(exit $$?)"; \
	step "no-such-command";         $$S no-such-command || echo "(exit $$?)"; \
	echo

# ---------------------------------------------------------------- tests ----

test: build ## End-to-end tests: both modes + auto-start (own socket)
	@bash $(ROOT)/test.sh

bench: $(CLIENT) start ## Compare warm client latency with a cold python3
	@if command -v hyperfine >/dev/null 2>&1; then \
	  hyperfine -N --warmup 10 "$(CLIENT) echo x" "$(PYTHON) -c 'import argparse'"; \
	else \
	  $(PYTHON) $(ROOT)/server/bench.py "$(CLIENT) echo x" "$(PYTHON) -c 'import argparse'"; \
	fi

# ---------------------------------------------------------------- misc -----

clean: ## Remove build output and caches
	cd $(ROOT)/client && $(CARGO) clean
	rm -rf $(ROOT)/server/__pycache__

dist: clean ## Create ../smts-ipc.zip
	cd $(ROOT)/.. && rm -f smts-ipc.zip && zip -qr smts-ipc.zip $(notdir $(ROOT)) && echo "wrote $$(pwd)/smts-ipc.zip"
