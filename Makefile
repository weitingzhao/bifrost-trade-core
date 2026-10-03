.PHONY: install install-dev test test-all test-db lint clean db-init db-init-brokerage seed-call-spread-templates

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

test:
	pytest -m 'not ib and not db'

test-all:
	pytest

# db-marked tests against a throwaway postgres:16-alpine container (needs docker; removed on exit).
# PYTEST_ARGS='-k ...' narrows the run; TEST_DB_IMAGE=postgres:17 picks another image.
# CI (no docker) runs `bash scripts/test_db.sh --sidecar` against a postgres sidecar on 127.0.0.1.
test-db:
	bash scripts/test_db.sh

test-ib:
	pytest -m ib

lint:
	ruff check .

lint-fix:
	ruff check --fix .

db-init:
	python scripts/db/db_refresh_schema.py
	python scripts/db/db_init_brokerage.py

db-init-brokerage:
	python scripts/db/db_init_brokerage.py

db-init-brokerage-fdw:
	python scripts/db/db_init_brokerage.py --with-fdw

seed-call-spread-templates:
	python scripts/db/seed_call_spread_templates.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc" -delete
	rm -rf dist/ build/ *.egg-info/
