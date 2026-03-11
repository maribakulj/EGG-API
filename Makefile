.PHONY: setup install init test run check-config check-backend paths

setup:
	./scripts/setup.sh

install: setup

init:
	pisco-api init

test:
	pytest

run:
	pisco-api run --host 127.0.0.1 --port 8000

check-config:
	pisco-api check-config

check-backend:
	pisco-api check-backend

paths:
	pisco-api print-paths
