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

The server listens on port 8000 and requires two environment variables:

```bash
export HEX_API_KEY="hxtp_..."   # Hex API key (authenticates to Hex)
export MCP_API_KEY="secret"     # bearer token clients must present
uv run app.py
```

If `MCP_API_KEY` is not set, the server runs without auth (not recommended for public deployments).

Optionally override the Hex base URL (e.g. for self-hosted Hex):

```bash
export HEX_BASE_URL="https://your-hex-instance.com/api/v1"
```

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
