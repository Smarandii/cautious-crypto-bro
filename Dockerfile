# Pin Python independently: uv's derived image can lag OS security updates.
# Dependabot tracks both upstream images and their immutable digests.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS uv

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS os
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/
ENV UV_TOOL_BIN_DIR=/usr/local/bin

FROM os AS runtime

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
COPY docs ./docs
COPY .env.example ./.env.example
COPY .github ./.github
RUN uv sync --frozen --group dev

CMD ["/app/.venv/bin/pytest", "-v"]
