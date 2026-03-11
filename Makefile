.PHONY: setup install init run check-config check-backend print-paths test

setup:
	./scripts/setup.sh

install: setup

init:
	pisco-api init

run:
	pisco-api run --reload --port 8000

check-config:
	pisco-api check-config

check-backend:
	pisco-api check-backend

print-paths:
	pisco-api print-paths

test:
	pytest
