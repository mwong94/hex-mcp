# Thin uv-based image for the Hex MCP server.
#
# app.py declares its dependencies inline (PEP 723), so there is no
# pyproject/lockfile — `uv sync --script` resolves them at build time into a
# cached script environment, and `uv run` reuses it at start-up.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Create the runtime user up front and build the environment as that user, so
# the ~200MB of dependencies live in exactly one layer (a later chown -R would
# duplicate them).
RUN useradd --system --create-home --uid 10001 hexmcp \
    && mkdir -p /opt/uv-cache /app \
    && chown hexmcp:hexmcp /opt/uv-cache /app
USER hexmcp
WORKDIR /app

COPY --chown=hexmcp:hexmcp app.py ./
RUN uv sync --script app.py && uv cache prune --ci

# Container defaults: an HTTP service on 8000, bound to all interfaces so the
# published port reaches it. Everything else (HEX_API_KEY, MCP_API_KEY) comes
# from the environment / .env at run time.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

CMD ["uv", "run", "app.py"]
