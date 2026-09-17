# hex-mcp

An MCP server for interacting with [Hex](https://hex.tech) notebooks and projects via the Hex API.

## What it does

Exposes the Hex API as MCP tools so an AI agent can discover, read, edit, run, and monitor Hex projects and notebooks without opening the Hex UI.

**Covered operations:**
- Browse workspace projects, users, and collections
- Read and edit notebook cells (Python, SQL, Markdown)
- Trigger project runs and poll their status
- Retrieve rendered chart images from completed runs
- Inspect data connections and queried tables
- Manage project status labels

## Setup

The server runs remotely at `https://hex-mcp.wongfam.io`. The `HEX_API_KEY` is configured server-side — no local credentials needed.

### Connect to Claude Code

Set `MCP_API_KEY` to your server's key, then:

```bash
just connect
```

Or manually:

```bash
claude mcp add --scope user hex -- uvx mcp-proxy --transport streamablehttp \
    -H Authorization "Bearer <MCP_API_KEY>" https://hex-mcp.wongfam.io/mcp
```

### Disconnect

```bash
just disconnect
```

### Running the server

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
openssl rand -hex 32   # use this as MCP_API_KEY
```

Then run it. `app.py` does not read `.env` on its own, so pass it to `uv`:

```bash
uv run --env-file .env app.py    # or: just run   (stdio)
just serve                       # HTTP on 127.0.0.1:8000
just health                      # confirm it answers an initialize
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `HEX_API_KEY` | yes | — | Hex API key the server authenticates to Hex with |
| `MCP_API_KEY` | no* | — | Bearer token clients must present; no auth if unset |
| `MCP_TRANSPORT` | no | `stdio` | Set to `streamable-http` to run as an HTTP service |
| `MCP_HOST` | no | `0.0.0.0` | Bind address (use `127.0.0.1` behind a local Caddy) |
| `MCP_PORT` | no | `8000` | Listen port (Docker publishes it on host `8386`) |
| `HEX_BASE_URL` | no | `https://app.hex.tech/api/v1` | Override for self-hosted Hex |

\* Not required by the code, but mandatory in practice for any deployment
reachable from outside localhost — without it anyone who can reach the port
can drive your Hex workspace.

## Running in Docker

A thin `uv`-based image (`ghcr.io/astral-sh/uv:python3.14-bookworm-slim`)
resolves `app.py`'s inline PEP 723 dependencies at build time and runs the
server as an unprivileged user. The container listens on **8000**; compose
publishes that on **127.0.0.1:8386**, which is the Caddy upstream.

```bash
just up             # build + start in the background
just health-docker  # initialize against http://127.0.0.1:8386/mcp
just logs           # follow logs
just down           # stop and remove
```

`compose.yaml` reads `.env` for `HEX_API_KEY` / `MCP_API_KEY` and overrides
`MCP_TRANSPORT`, `MCP_HOST` and `MCP_PORT` so the container always matches the
port mapping. To change the published port, edit the `ports:` entry.

| Recipe | Purpose |
|---|---|
| `just build` | Build the image |
| `just up` | Build and start in the background |
| `just down` | Stop and remove the container |
| `just restart` | Restart the container |
| `just logs` / `just ps` | Follow logs / show status |
| `just shell` | Shell inside the running container |
| `just docker-run` | Run the image in the foreground, without compose |

## Deploying behind Caddy

This is the setup that backs `https://hex-mcp.wongfam.io`: something listens on
`127.0.0.1:8386` — either the Docker container above or the systemd unit below —
and Caddy terminates TLS and reverse proxies to it. With Docker, skip step 1 and
run `just up`; steps 2–4 are unchanged.

### 1. Run the server as a systemd service

```bash
sudo tee /etc/systemd/system/hex-mcp.service <<'EOF'
[Unit]
Description=Hex MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hexmcp
WorkingDirectory=/opt/hex-mcp
EnvironmentFile=/opt/hex-mcp/.env
ExecStart=/usr/bin/env uv run app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now hex-mcp
sudo systemctl status hex-mcp
```

`EnvironmentFile` reads `.env` directly, so `--env-file` is not needed here.
Keep the file readable only by the service user:

```bash
sudo chmod 600 /opt/hex-mcp/.env && sudo chown hexmcp /opt/hex-mcp/.env
```

### 2. Point DNS at the host

Create an `A`/`AAAA` record for `hex-mcp.wongfam.io` pointing at your home
server's public IP, and forward ports 80 and 443 to it. Caddy needs port 80
reachable for the HTTP-01 ACME challenge.

If you would rather not open ports, use the DNS-01 challenge instead — that
needs a Caddy build with your DNS provider's plugin and a `dns` directive in
the `tls` block.

### 3. Caddyfile block

Add to `/etc/caddy/Caddyfile`:

```caddy
hex-mcp.wongfam.io {
	encode zstd gzip

	reverse_proxy 127.0.0.1:8386 {
		# MCP streams responses over SSE — don't buffer them.
		flush_interval -1

		# Long-lived streams; keep the upstream connection open.
		transport http {
			read_timeout 0
			write_timeout 0
		}
	}

	log {
		output file /var/log/caddy/hex-mcp.log
	}
}
```

Reload and check:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

`flush_interval -1` is the important line — without it Caddy buffers the
streamable-HTTP/SSE responses and clients hang waiting for tool results.

### 4. Verify

An unauthenticated request should be rejected:

```bash
curl -i https://hex-mcp.wongfam.io/mcp
```

An authenticated initialize should return a session:

```bash
curl -i -X POST https://hex-mcp.wongfam.io/mcp \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Then connect a client with `just connect`.

## Key concepts

**Draft vs. Published**
Every Hex project has two versions. All cell edits (create/update/delete) target the **draft** only. Running a project executes the **published** version — drafts changes won't appear in a run until you publish from the Hex UI.

**Cell IDs**
Each cell has two identifiers:
- `id` — version-scoped; changes when the project is published
- `staticId` — stable across all versions; required when fetching chart images from a run

**Cell types**
The API supports `CODE` (Python/R), `SQL`, and `MARKDOWN` cells.

**Rate limits**
Run project: 20 requests/min, 60 requests/hr.

## Tools reference

### Workspace & users

| Tool | Description |
|---|---|
| `get_current_user` | Identity of the authenticated API key owner |
| `list_users` | Workspace users; filter by group |
| `list_collections` | Browse workspace collections |

### Projects

| Tool | Description |
|---|---|
| `list_projects` | List projects with sort/filter options |
| `get_project` | Full project metadata including analytics and schedules |
| `create_project` | Create a new blank project |
| `update_project_status` | Set or clear a project's status/endorsement label |
| `get_queried_tables` | Warehouse tables referenced by a project's SQL cells |

### Cells

| Tool | Description |
|---|---|
| `list_cells` | All cells in a project's draft notebook |
| `get_cell` | Full source code of a single cell |
| `create_cell` | Add a CODE, SQL, or MARKDOWN cell to the draft |
| `update_cell` | Update a cell's source code or data connection |
| `delete_cell` | Remove a cell from the draft |
| `search_cells` | Regex search across all cell sources |
| `duplicate_cell` | Copy a cell and place it after the original |
| `notebook_outline` | High-level structure: position, type, label, first line |
| `find_and_replace` | Bulk regex find-and-replace across cells (dry-run by default) |

### Execution

| Tool | Description |
|---|---|
| `run_project` | Trigger a run of the published project |
| `get_run_status` | Poll run status (PENDING/RUNNING/COMPLETED/ERRORED/KILLED) |
| `cancel_run` | Cancel a pending or running execution |
| `get_project_runs` | Recent runs with optional status filter |

### Charts & data connections

| Tool | Description |
|---|---|
| `get_chart_image` | PNG of a chart cell from a completed run (uses `staticId`) |
| `get_cell_image_live` | PNG of a chart cell from the current session (uses `id`) |
| `list_data_connections` | Available warehouse connections and their IDs |
| `get_data_connection` | Full details for a single connection |

## Common workflows

**Inspect and edit a notebook**
```
get_current_user          # confirm identity
list_projects             # find the project_id
list_cells <project_id>   # see all cells
get_cell <cell_id>        # read full source
update_cell <cell_id> ... # edit source
run_project <project_id>  # run the published version
get_run_status ...        # poll until COMPLETED
```

**Add a new SQL cell after an existing one**
```
list_cells <project_id>           # get cell ids and order
list_data_connections             # get a connection id
create_cell <project_id> SQL ...  # after_cell_id positions it
```

**Review chart output**
```
run_project <project_id>
get_run_status <project_id> <run_id>   # wait for COMPLETED
list_cells <project_id>                # get staticId
get_chart_image <project_id> <run_id> <static_cell_id>
```

**Find all cells that reference a table**
```
search_cells <project_id> "orders"
```

**Safe bulk rename**
```
find_and_replace <project_id> "old_table" "new_table"   # dry_run=True (default)
find_and_replace <project_id> "old_table" "new_table" dry_run=False
```
