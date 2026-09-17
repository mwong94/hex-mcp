mcp_url := "https://hex-mcp.wongfam.io/mcp"

# Local dev server settings (override: just serve port=9000)
host := "127.0.0.1"
port := "8000"
env_file := ".env"

# Port the Docker container's 8000 is published on (the Caddy upstream)
docker_port := "8386"
image := "hex-mcp:local"

# List available commands
default:
    @just --list

# ---------------------------------------------------------------------------
# Local server
# ---------------------------------------------------------------------------

# Create .env from the example if it does not exist yet
init-env:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -e "{{env_file}}" ]; then
        echo "{{env_file}} already exists — leaving it alone."
    else
        cp .env.example "{{env_file}}"
        chmod 600 "{{env_file}}"
        echo "Created {{env_file}}. Fill in HEX_API_KEY and MCP_API_KEY."
        echo "Suggested MCP_API_KEY: $(openssl rand -hex 32)"
    fi

# Verify .env exists and the required keys are filled in
check-env:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -e "{{env_file}}" ]; then
        echo "Missing {{env_file}} — run 'just init-env' first." >&2
        exit 1
    fi
    missing=0
    for key in HEX_API_KEY MCP_API_KEY; do
        value=$(grep -E "^${key}=" "{{env_file}}" | tail -n1 | cut -d= -f2-)
        if [ -z "$value" ] || [[ "$value" == *replace_me* ]]; then
            echo "$key is unset or still a placeholder in {{env_file}}" >&2
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] && echo "{{env_file}} looks good." >&2
    exit "$missing"

# Run the server on stdio (how a local client spawns it)
run: check-env
    MCP_TRANSPORT=stdio uv run --env-file {{env_file}} app.py

# Run the server as a local HTTP service (default 127.0.0.1:8000)
serve: check-env
    MCP_TRANSPORT=streamable-http MCP_HOST={{host}} MCP_PORT={{port}} \
        uv run --env-file {{env_file}} app.py

# Run the HTTP service with no client auth (localhost-only convenience)
serve-noauth: check-env
    MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT={{port}} MCP_API_KEY= \
        uv run --env-file {{env_file}} app.py

# Open the FastMCP inspector against the local server
inspect: check-env
    MCP_TRANSPORT=stdio uv run --env-file {{env_file}} --with fastmcp fastmcp dev app.py

# Send an initialize request to a local server to confirm it is up
health target_port=port:
    #!/usr/bin/env bash
    set -euo pipefail
    key=$(grep -E '^MCP_API_KEY=' "{{env_file}}" | tail -n1 | cut -d= -f2-)
    curl -sS -i -X POST "http://{{host}}:{{target_port}}/mcp" \
        ${key:+-H "Authorization: Bearer $key"} \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"just","version":"0"}}}'

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

# Build the container image
build:
    docker compose build

# Build and start the container in the background (published on host port 8386)
up: check-env
    docker compose up -d --build
    @echo "Running on http://{{host}}:{{docker_port}}/mcp — 'just health-docker' to verify."

# Stop and remove the container
down:
    docker compose down

# Restart the container
restart: check-env
    docker compose restart

# Follow container logs
logs:
    docker compose logs -f

# Show container status
ps:
    docker compose ps

# Initialize against the containerised server (host port 8386)
health-docker: (health docker_port)

# Open a shell inside the running container
shell:
    docker compose exec hex-mcp /bin/bash

# Run the image in the foreground without compose (ephemeral)
docker-run: check-env
    docker run --rm -it --env-file {{env_file}} \
        -e MCP_TRANSPORT=streamable-http -e MCP_HOST=0.0.0.0 -e MCP_PORT=8000 \
        -p {{host}}:{{docker_port}}:8000 {{image}}

# ---------------------------------------------------------------------------
# Claude Code registration — local
# ---------------------------------------------------------------------------

# Register the LOCAL server with Claude Code as a stdio subprocess (name: hex-local)
connect-local: check-env
    #!/usr/bin/env bash
    set -euo pipefail
    claude mcp add --scope user hex-local -- \
        env MCP_TRANSPORT=stdio uv run --env-file {{justfile_directory()}}/{{env_file}} \
        {{justfile_directory()}}/app.py
    echo "Connected to the local server. Run 'just status-local' to verify."

# Remove the local hex MCP server from Claude Code
disconnect-local:
    claude mcp remove hex-local
    echo "Removed hex-local MCP server."

# Show local registration status
status-local:
    claude mcp get hex-local

# ---------------------------------------------------------------------------
# Claude Code registration — remote
# ---------------------------------------------------------------------------

# Register the remote hex MCP server with Claude Code (user scope)
connect:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${MCP_API_KEY:-}" ]; then
        read -rsp "MCP_API_KEY: " key
        echo
    else
        key="$MCP_API_KEY"
    fi
    claude mcp add --scope user hex -- uvx mcp-proxy --transport streamablehttp \
        -H Authorization "Bearer $key" {{mcp_url}}
    echo "Connected. Run 'just status' to verify."

# Remove the hex MCP server from Claude Code
disconnect:
    claude mcp remove hex
    echo "Removed hex MCP server."

# Show current registration status
status:
    claude mcp get hex

# List all registered MCP servers
list:
    claude mcp list
