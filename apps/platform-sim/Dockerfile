# platform-sim image. Build from the REPO ROOT, not this directory:
#
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -f apps/platform-sim/Dockerfile -t radar-platform-sim .
#
# The build context must be the repo root because this is a uv workspace: the
# simulator depends on the radar-common and radar-telemetry members, and uv resolves
# the whole workspace against the root uv.lock. A .dockerignore keeps the host
# .venv and caches out of the context.

# --- builder: resolve the workspace and install platform-sim into a venv ------
FROM python:3.14-slim AS builder

# Pinned uv, matching the version the repo's uv.lock was written with.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

WORKDIR /app

# copy = safe across the layer boundary; never fetch a different interpreter.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY . .

# --frozen: honor uv.lock exactly (reproducible). --no-editable: install the
# workspace packages as real wheels so the runtime stage needs no source tree.
RUN uv sync --frozen --no-dev --no-editable --package radar-platform-sim

# --- runtime: just the venv, no uv, no build tooling, non-root ----------------
FROM python:3.14-slim

WORKDIR /app

RUN useradd --system --uid 1001 radar

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

USER radar

EXPOSE 8080

CMD ["uvicorn", "radar_platform_sim.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
