# Pin Python independently: uv's derived image can lag OS security updates.
# Dependabot tracks both upstream images and their immutable digests.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS uv

FROM python:3.15-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853 AS os
# The official image can lag Debian security updates (CVE-2026-84782).
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade libssl3 openssl \
    && rm -rf /var/lib/apt/lists/*
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
