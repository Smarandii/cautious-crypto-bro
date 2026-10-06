# Tag and digest together: the tag keeps the line readable, the digest makes the
# build reproducible. Dependabot tracks the Dockerfile and bumps both.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS runtime

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
