.PHONY: setup install test run

setup:
	./scripts/setup.sh

install: setup

test:
	pytest

run:
	uvicorn app.main:app --reload --port 8000
