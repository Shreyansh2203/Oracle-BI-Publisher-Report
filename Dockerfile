FROM python:3.11-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
COPY src/ src/

RUN uv pip install --system --no-cache .

COPY reports.txt .

EXPOSE 8000

# Single worker by default; scale horizontally via container replicas, not workers.
CMD ["sh", "-c", "uvicorn bip_api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
