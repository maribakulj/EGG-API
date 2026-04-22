.PHONY: setup install init dev run check-config check-backend print-paths test

setup:
	./scripts/setup.sh

install: setup

init:
	egg-api init

# Developer loop: auto-reload on source edits. Do NOT use in production —
# uvicorn --reload forks a watcher process and is not hardened for real traffic.
dev:
	egg-api run --reload --port 8000

# Production-style local run: no --reload, bind loopback only.
run:
	egg-api run --port 8000

check-config:
	egg-api check-config

check-backend:
	egg-api check-backend

print-paths:
	egg-api print-paths

test:
	pytest
