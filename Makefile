.PHONY: setup install init run check-config check-backend print-paths test

setup:
	./scripts/setup.sh

install: setup

init:
	egg-api init

run:
	egg-api run --reload --port 8000

check-config:
	egg-api check-config

check-backend:
	egg-api check-backend

print-paths:
	egg-api print-paths

test:
	pytest
