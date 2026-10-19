#!/usr/bin/env sh
set -eu
python -m ruff check .
python -m pytest --cov=agent --cov-fail-under=80
python agent.py --init
python agent.py --check
python agent.py --status
