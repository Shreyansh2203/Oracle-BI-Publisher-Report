.PHONY: install dev lint format typecheck test all docker-run

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

# Run the Docker image with env vars from .env (use docker-compose for full stack).
docker-run:
	docker run --rm -p 8000:8000 --env-file .env bip-api
