# feedback-service image. Build from the REPO ROOT, not this directory:
#
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -f apps/feedback-service/Dockerfile -t radar-feedback-service .
#
# The build context must be the repo root because this is a uv workspace: the
# service depends on the radar-common, radar-contracts, radar-database,
# radar-telemetry, and radar-plugin-notifications-slack members, and uv resolves
# the whole workspace against the root uv.lock. The root .dockerignore trims the
# context.

# --- builder: resolve the workspace and install feedback-service into a venv --
FROM python:3.14-slim AS builder

# Pinned uv, matching the version the repo's uv.lock was written with.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

WORKDIR /app

# asyncpg (a radar-database dependency) ships no CPython 3.14 wheel yet, so uv
# builds it from source — which needs a C compiler. Kept in this builder stage
# only; the runtime stage below never gets the toolchain.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY . .

# --frozen: honor uv.lock exactly. --no-editable: install workspace packages as
# real wheels so the runtime stage needs no source tree.
RUN uv sync --frozen --no-dev --no-editable --package radar-feedback-service

# --- runtime: just the venv, no uv, no build tooling, non-root -----------------
FROM python:3.14-slim

WORKDIR /app

RUN useradd --system --uid 1001 radar

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

USER radar

EXPOSE 8080

CMD ["uvicorn", "radar_feedback_service.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
