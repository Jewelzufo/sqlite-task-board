.PHONY: setup init check status run test lint

setup:
	python -m pip install -e '.[dev]'

init:
	python agent.py --init

check:
	python agent.py --check

status:
	python agent.py --status

run:
	python agent.py

test:
	python -m pytest tests

lint:
	ruff check .
