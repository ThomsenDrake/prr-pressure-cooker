FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PRR_DB_PATH=/data/prr.db \
    PRR_CASEFILES_DIR=/data/casefiles \
    DEPLOYMENT_NAME=prr-pressure-cooker-prod

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev || uv sync --no-dev
RUN mkdir -p /data/casefiles

CMD ["uv", "run", "--no-sync", "prr", "worker"]
