mcp_url := "https://hex-mcp.wongfam.io/mcp"

# List available commands
default:
    @just --list

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
