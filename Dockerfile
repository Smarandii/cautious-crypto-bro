FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install third-party dependencies first so this layer remains cached when
# application code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

CMD ["/app/.venv/bin/cautious-crypto-bro"]


FROM runtime AS test

COPY tests ./tests
RUN uv sync --frozen --group dev

CMD ["/app/.venv/bin/pytest", "-v"]
