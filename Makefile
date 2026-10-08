.PHONY: setup lint format format-check typecheck test test-live migrate migrate-check health connectivity serve clean

PY := .venv/bin/python

setup:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .

format-check:
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy factory tests

test:
	$(PY) -m pytest

test-live:
	$(PY) -m pytest -m live tests/integration -v

migrate:
	$(PY) -m alembic upgrade head

migrate-check:
	$(PY) -m alembic check

health:
	$(PY) -m factory.cli health

connectivity:
	$(PY) -m factory.cli cleanapis test-connection

serve:
	$(PY) -m factory.cli serve

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache storage
	rm -rf factory.egg-info build dist
