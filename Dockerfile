FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev || uv sync --no-dev

CMD ["uv", "run", "prr", "worker"]
