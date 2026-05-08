FROM python:3.11-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
COPY src/ src/

RUN uv pip install --system --no-cache -e .

COPY reports.txt .

EXPOSE 8000

CMD ["uvicorn", "bip_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
