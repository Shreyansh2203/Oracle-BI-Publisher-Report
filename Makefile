.PHONY: install dev lint format typecheck test all

install:
	uv pip install -e ".[dev]"

dev:
	uvicorn bip_api.main:app --reload --host 0.0.0.0 --port 8000

lint:
	ruff check src tests

format:
	ruff format src tests

typecheck:
	mypy src

test:
	pytest -v

all: lint typecheck test
