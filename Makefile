.PHONY: setup init check status run audit graph test lint

setup:
	python -m pip install -e '.[dev]'

init:
	python agent.py --init

check:
	python agent.py --check

status:
	python agent.py --status

audit:
	python agent.py --audit-verify

graph:
	python agent.py --graph --format json

run:
	python agent.py

test:
	python -m pytest tests

lint:
	ruff check .
