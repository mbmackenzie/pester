# Multi-stage build: uv resolves the locked dependencies into a venv, and the runtime image copies only that.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN groupadd --system --gid 1000 pester \
    && useradd --system --uid 1000 --gid pester --home-dir /app --no-create-home pester \
    && mkdir /data && chown pester:pester /data
COPY --from=build --chown=pester:pester /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PESTER_HOST=0.0.0.0 \
    PESTER_PORT=8000 \
    PESTER_DATABASE_PATH=/data/pester.sqlite
WORKDIR /data
USER pester
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
CMD ["pester", "serve"]
