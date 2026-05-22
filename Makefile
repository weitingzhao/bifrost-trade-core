.PHONY: install install-dev test test-all lint clean db-init

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

test:
	pytest -m 'not ib and not db'

test-all:
	pytest

test-ib:
	pytest -m ib

lint:
	ruff check src/ tests/

lint-fix:
	ruff check --fix src/ tests/

db-init:
	python scripts/db/db_refresh_schema.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc" -delete
	rm -rf dist/ build/ *.egg-info/
