# /// script
# dependencies = [
#   "fastmcp",
#   "requests",
#   "uvicorn",
# ]
# ///

"""
Hex Notebook MCP Server
=======================
An MCP server for interacting with Hex notebooks and projects.

Covers: project discovery, cell CRUD, notebook execution,
run monitoring, chart retrieval, data connection inspection,
user/collection management, and project sharing.

KEY CONCEPTS (read before using tools):
  - A Hex PROJECT has two versions: DRAFT and PUBLISHED.
    All cell edits target the DRAFT only. Running a project executes
    the PUBLISHED version. To see draft changes in the app, publish
    from the Hex UI first.
  - A cell has two IDs:
      id       — scoped to a specific version; changes on publish
      staticId — stable across all versions; use for run chart images
  - Cell types supported via API: CODE, SQL, MARKDOWN
  - Rate limits: 20 run requests/min, 60 run requests/hr

Requires: HEX_API_KEY environment variable.
Optional:  HEX_BASE_URL (defaults to https://app.hex.tech/api/v1)
"""

import os
import re
import time
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HEX_API_KEY = os.getenv("HEX_API_KEY")
HEX_BASE_URL = os.getenv("HEX_BASE_URL", "https://app.hex.tech/api/v1")
MCP_API_KEY = os.getenv("MCP_API_KEY")

if not HEX_API_KEY:
    raise EnvironmentError(
        "HEX_API_KEY is not set.  Export it before starting the server:\n"
        "  export HEX_API_KEY='hxtp_...'"
    )

HEADERS = {
    "Authorization": f"Bearer {HEX_API_KEY}",
    "Content-Type": "application/json",
}

# Rate-limit awareness (Hex enforces 20 req/min, 60 req/hr on RunProject)
RUN_RATE_LIMIT = {"calls": 0, "window_start": time.time()}

log = logging.getLogger("hex-mcp")

# ---------------------------------------------------------------------------
# HTTP helper with retry & structured errors
# ---------------------------------------------------------------------------

_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _hex(method: str, path: str, *, params: dict | None = None,
         data: dict | None = None) -> Any:
    """Send a request to the Hex API and return parsed JSON."""
    url = f"{HEX_BASE_URL}{path}"
    resp = _session.request(
        method, url, headers=HEADERS, params=params, json=data, timeout=30,
    )
    if not resp.ok:
        body = resp.text
        try:
            err = resp.json()
            reason = err.get("reason", body)
            trace = err.get("traceId", "n/a")
        except Exception:
            reason, trace = body, "n/a"
        raise Exception(
            f"Hex API {resp.status_code} on {method} {path}: {reason} "
            f"(traceId={trace})"
        )
    if resp.status_code == 204:
        return {"status": "success"}
    return resp.json()


# ---------------------------------------------------------------------------
# Cell content helpers
# ---------------------------------------------------------------------------

def _extract_source(cell: dict) -> str:
    """Pull the source string out of a cell response regardless of type."""
    contents = cell.get("contents", {})
    sql = contents.get("sqlCell", {})
    code = contents.get("codeCell", {})
    markdown = contents.get("markdownCell", {})
    return (
        (sql.get("source") if sql else None)
        or (code.get("source") if code else None)
        or (markdown.get("source") if markdown else None)
        or ""
    )


def _cell_summary(cell: dict) -> dict:
    """Compact representation of a cell for list views."""
    return {
        "id": cell["id"],
        "staticId": cell.get("staticId"),
        "type": cell.get("cellType", "UNKNOWN"),
        "label": cell.get("label", ""),
        "source_preview": _extract_source(cell)[:120],
    }


def _build_cell_contents(cell_type: str, source: str,
                          data_connection_id: str | None = None) -> dict:
    """Build the contents dict for create/update requests."""
    if cell_type == "SQL":
        sql: dict[str, Any] = {"source": source}
        if data_connection_id:
            sql["dataConnectionId"] = data_connection_id
        return {"sqlCell": sql}
    if cell_type == "MARKDOWN":
        return {"markdownCell": {"source": source}}
    return {"codeCell": {"source": source}}


# ===================================================================
# MCP Server
# ===================================================================


class _BearerKeyVerifier(TokenVerifier):
    """Validates requests against a static bearer token (MCP_API_KEY)."""

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self._api_key = api_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._api_key:
            return None
        return AccessToken(token=token, client_id="mcp-client", scopes=[])


_auth = _BearerKeyVerifier(MCP_API_KEY) if MCP_API_KEY else None
mcp = FastMCP("Hex-Notebook-Editor", auth=_auth)


# -------------------------------------------------------------------
# WORKSPACE / USER CONTEXT
# -------------------------------------------------------------------

@mcp.resource("hex://context")
def hex_context() -> str:
    """
    Hex API reference context. Read this before using other tools.

    Draft vs Published
    ------------------
    All cell edits (create/update/delete) target the DRAFT version only.
    Running a project executes the PUBLISHED version — not the draft.
    If you edited cells and want to verify the run reflects those edits,
    you must publish from the Hex UI first.

    Cell IDs
    --------
    Each cell has two IDs:
      id       — version-scoped; changes each time the project is published
      staticId — stable across all versions; required for run chart images

    Cell types
    ----------
    Supported via API: CODE (Python/R), SQL, MARKDOWN

    Rate limits
    -----------
    Run project: 20 requests/min, 60 requests/hr

    Common workflows
    ----------------
    Inspect a notebook:  list_cells → get_cell
    Edit a cell:         list_cells → update_cell → run_project → get_run_status
    Add a cell:          list_cells → create_cell (use after_cell_id to position)
    View chart output:   run_project → get_run_status (wait COMPLETED) → get_chart_image
    Find table usage:    get_queried_tables
    """
    return hex_context.__doc__


@mcp.tool()
def get_current_user() -> dict:
    """
    Get the identity of the authenticated API key owner.

    Returns email, name, role, and org ID. Call this at session start
    to confirm which user and org the API key belongs to.

    Role values: ADMIN | MANAGER | EDITOR | EXPLORER | MEMBER | GUEST |
                 EMBEDDED_USER | ANONYMOUS
    """
    me = _hex("GET", "/users/me")
    return {
        "id": me.get("id"),
        "email": me.get("email"),
        "name": me.get("name"),
        "role": me.get("role"),
        "org_id": me.get("org", {}).get("id"),
        "token_expires": me.get("token", {}).get("exp"),
    }


@mcp.tool()
def list_users(
    limit: int = 25,
    group_id: str | None = None,
) -> dict:
    """
    List workspace users.

    Useful for looking up user IDs needed when sharing projects.
    - group_id: filter to members of a specific group
    """
    params: dict[str, Any] = {"limit": min(limit, 100)}
    if group_id:
        params["groupId"] = group_id
    result = _hex("GET", "/users", params=params)
    users = result.get("values", [])
    return {
        "count": len(users),
        "users": [
            {
                "id": u["id"],
                "email": u["email"],
                "name": u.get("name", ""),
                "role": u.get("role"),
                "last_login": u.get("lastLoginDate"),
            }
            for u in users
        ],
    }


# -------------------------------------------------------------------
# PROJECT DISCOVERY
# -------------------------------------------------------------------

@mcp.tool()
def list_projects(
    limit: int = 25,
    sort_by: str = "LAST_EDITED_AT",
    sort_direction: str = "DESC",
    include_archived: bool = False,
    status_filter: str | None = None,
    category_filter: str | None = None,
    owner_email: str | None = None,
    collection_id: str | None = None,
) -> dict:
    """
    List projects in the workspace.

    Useful for finding the project_id you need before any cell operations.
    Returns id, title, owner, last-edited timestamp, and status.

    - sort_by: CREATED_AT | LAST_EDITED_AT | LAST_PUBLISHED_AT
    - sort_direction: ASC | DESC
    - status_filter / category_filter: optional workspace status/category name
    - owner_email: filter to a specific owner
    - collection_id: filter to projects in a specific collection
    """
    params: dict[str, Any] = {
        "limit": min(limit, 100),
        "sortBy": sort_by,
        "sortDirection": sort_direction,
        "includeArchived": include_archived,
    }
    if status_filter:
        params["statuses"] = status_filter
    if category_filter:
        params["categories"] = category_filter
    if owner_email:
        params["ownerEmail"] = owner_email
    if collection_id:
        params["collectionId"] = collection_id

    result = _hex("GET", "/projects", params=params)
    projects = result.get("values", [])
    return {
        "count": len(projects),
        "projects": [
            {
                "id": p["id"],
                "title": p["title"],
                "owner": p.get("owner", {}).get("email", ""),
                "last_edited": p.get("lastEditedAt", ""),
                "status": (p.get("status") or {}).get("name", ""),
                "type": p.get("type", ""),
            }
            for p in projects
        ],
    }


@mcp.tool()
def get_project(project_id: str) -> dict:
    """
    Get detailed metadata for a single project: title, description,
    owner, schedules, status, categories, sharing, and app-view analytics.
    """
    p = _hex("GET", f"/projects/{project_id}", params={"includeSharing": True})
    return {
        "id": p["id"],
        "title": p["title"],
        "description": p.get("description"),
        "type": p.get("type"),
        "owner": p.get("owner", {}).get("email"),
        "creator": p.get("creator", {}).get("email"),
        "status": (p.get("status") or {}).get("name"),
        "categories": [c["name"] for c in p.get("categories", [])],
        "last_edited": p.get("lastEditedAt"),
        "last_published": p.get("lastPublishedAt"),
        "created_at": p.get("createdAt"),
        "archived_at": p.get("archivedAt"),
        "schedule_count": len(p.get("schedules", [])),
        "app_views_30d": p.get("analytics", {}).get("appViews", {}).get("lastThirtyDays", 0),
        "app_views_all_time": p.get("analytics", {}).get("appViews", {}).get("allTime", 0),
        "reviews_required": p.get("reviews", {}).get("required", False),
    }


@mcp.tool()
def create_project(title: str, description: str | None = None) -> dict:
    """
    Create a new blank project in the workspace.

    Returns the new project's id, which you can then use with
    create_cell() to populate it.
    """
    body: dict[str, Any] = {"title": title}
    if description:
        body["description"] = description
    p = _hex("POST", "/projects", data=body)
    return {
        "id": p["id"],
        "title": p["title"],
        "type": p.get("type"),
        "created_at": p.get("createdAt"),
    }


@mcp.tool()
def update_project_status(project_id: str, status: str | None = None) -> dict:
    """
    Set or clear the status/endorsement label on a project.

    - status: the status name to apply (e.g. 'Endorsed', 'Deprecated'),
              or null/empty string to remove the current status.

    Status names are workspace-defined. Use get_project() to see the
    current status of a project.
    """
    body: dict[str, Any] = {"status": status or None}
    p = _hex("PATCH", f"/projects/{project_id}", data=body)
    return {
        "id": p["id"],
        "title": p["title"],
        "status": (p.get("status") or {}).get("name"),
    }


@mcp.tool()
def get_queried_tables(project_id: str, limit: int = 100) -> dict:
    """
    List all warehouse tables referenced by SQL cells in a project.
    Useful for impact analysis — know exactly which tables a notebook touches.
    """
    result = _hex(
        "GET", f"/projects/{project_id}/queriedTables",
        params={"limit": min(limit, 100)},
    )
    tables = result.get("values", [])
    return {
        "count": len(tables),
        "tables": [
            {
                "table": t["tableName"],
                "connection": t["dataConnectionName"],
                "connection_id": t["dataConnectionId"],
            }
            for t in tables
        ],
    }


@mcp.tool()
def list_collections(limit: int = 25) -> dict:
    """
    List all collections in the workspace.

    Collections are named groups of projects. Use collection_id with
    list_projects() to filter projects by collection.
    """
    result = _hex("GET", "/collections", params={"limit": min(limit, 100)})
    collections = result.get("values", [])
    return {
        "count": len(collections),
        "collections": [
            {
                "id": c["id"],
                "name": c["name"],
                "description": c.get("description", ""),
                "creator_email": c.get("creator", {}).get("email", ""),
            }
            for c in collections
        ],
    }


# -------------------------------------------------------------------
# CELL OPERATIONS  (the core editing tools)
# -------------------------------------------------------------------

@mcp.tool()
def list_cells(project_id: str, limit: int = 100) -> dict:
    """
    REQUIRED FIRST STEP. Lists all cells in a project's draft notebook.

    Returns cell id, staticId, type, label, and a source preview.
    Use the cell id for subsequent get/update/delete operations.
    Use staticId when fetching chart images from completed runs.

    NOTE: Targets the DRAFT version only, not the published version.
    """
    result = _hex("GET", "/cells", params={
        "projectId": project_id,
        "limit": min(limit, 100),
    })
    cells = result.get("values", [])
    summaries = [_cell_summary(c) for c in cells]
    return {"count": len(summaries), "cells": summaries}


@mcp.tool()
def get_cell(cell_id: str) -> dict:
    """
    Read the full source code of a single cell.

    Returns the cell type, label, full source code, and (for SQL cells)
    the data connection id it's bound to.
    """
    cell = _hex("GET", f"/cells/{cell_id}")
    contents = cell.get("contents", {})
    return {
        "id": cell["id"],
        "staticId": cell.get("staticId"),
        "type": cell.get("cellType"),
        "label": cell.get("label", ""),
        "source": _extract_source(cell),
        "data_connection_id": cell.get("dataConnectionId"),
        "sql_cell": contents.get("sqlCell"),
        "code_cell": contents.get("codeCell"),
        "markdown_cell": contents.get("markdownCell"),
    }


@mcp.tool()
def search_cells(project_id: str, query: str, case_sensitive: bool = False) -> dict:
    """
    Search through all SQL, CODE, and MARKDOWN cells in a project for a regex pattern.

    Examples:
      - Find all references to a table: search_cells(pid, "dim_users")
      - Find TODOs: search_cells(pid, "TODO|FIXME|HACK")
      - Find a variable: search_cells(pid, "df_revenue")

    Returns matching cell ids, labels, types, and the first match snippet.
    """
    result = _hex("GET", "/cells", params={
        "projectId": project_id,
        "limit": 100,
    })
    cells = result.get("values", [])
    flags = 0 if case_sensitive else re.IGNORECASE
    matches = []

    for cell in cells:
        source = _extract_source(cell)
        m = re.search(query, source, flags)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(source), m.end() + 30)
            matches.append({
                "id": cell["id"],
                "label": cell.get("label", ""),
                "type": cell.get("cellType", ""),
                "snippet": f"...{source[start:end]}...",
            })

    return {"count": len(matches), "matches": matches}


@mcp.tool()
def create_cell(
    project_id: str,
    cell_type: str,
    source: str,
    after_cell_id: str | None = None,
    label: str = "Claude Generated",
    data_connection_id: str | None = None,
) -> dict:
    """
    Add a new cell to the draft notebook.

    - cell_type: 'CODE' | 'SQL' | 'MARKDOWN'
    - source: the Python/SQL/Markdown content for the cell
    - after_cell_id: place the new cell after this cell id (omit to append at end)
    - label: human-readable label shown in the notebook
    - data_connection_id: required for SQL cells — the warehouse connection to use.
      Use list_data_connections() or get_queried_tables() to find valid ids.
      Not needed for SQL cells that query other SQL cell outputs (dataframe SQL).

    IMPORTANT: This edits the DRAFT version only. The published app is not
    affected until you publish from the Hex UI.
    """
    c_type = cell_type.upper()
    if c_type not in ("CODE", "SQL", "MARKDOWN"):
        return {"error": "cell_type must be 'CODE', 'SQL', or 'MARKDOWN'"}

    body: dict[str, Any] = {
        "projectId": project_id,
        "cellType": c_type,
        "label": label,
        "contents": _build_cell_contents(c_type, source, data_connection_id if c_type == "SQL" else None),
    }
    if after_cell_id:
        body["location"] = {"insertAfterCellId": after_cell_id}

    new_cell = _hex("POST", "/cells", data=body)
    position = f"after {after_cell_id}" if after_cell_id else "at the end"
    return {
        "id": new_cell.get("id"),
        "staticId": new_cell.get("staticId"),
        "type": c_type,
        "label": label,
        "position": position,
    }


@mcp.tool()
def update_cell(
    cell_id: str,
    source: str | None = None,
    data_connection_id: str | None = None,
) -> dict:
    """
    Update an existing cell's source code or data connection.

    Auto-detects whether the cell is SQL, CODE, or MARKDOWN and applies
    the update to the correct content field.

    Only the fields you provide will be changed — omit a field to leave
    it unchanged.

    NOTE: Cell labels cannot be changed via the Hex API.
    NOTE: This edits the DRAFT version only.
    """
    current = _hex("GET", f"/cells/{cell_id}")
    cell_type = current.get("cellType", "CODE").upper()

    body: dict[str, Any] = {}

    if source is not None:
        body["contents"] = _build_cell_contents(cell_type, source)

    if data_connection_id is not None and cell_type == "SQL":
        body["dataConnectionId"] = data_connection_id

    if not body:
        return {"error": "Nothing to update — provide at least one of: source, data_connection_id"}

    _hex("PATCH", f"/cells/{cell_id}", data=body)
    return {
        "status": "updated",
        "cell_id": cell_id,
        "type": cell_type,
        "fields_changed": list(body.keys()),
        "next_step": "Run the project to verify your changes (runs the published version).",
    }


@mcp.tool()
def delete_cell(cell_id: str) -> dict:
    """
    Delete a cell from the draft notebook.

    WARNING: This is irreversible in the draft. The published version
    is not affected until you re-publish.
    """
    _hex("DELETE", f"/cells/{cell_id}")
    return {"status": "deleted", "cell_id": cell_id}


# -------------------------------------------------------------------
# PROJECT EXECUTION  (run, poll, cancel)
# -------------------------------------------------------------------

@mcp.tool()
def run_project(
    project_id: str,
    update_published_results: bool = False,
    use_cached_sql: bool = True,
    input_params: dict | None = None,
    view_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Trigger a run of the latest PUBLISHED version of a project.

    IMPORTANT: This runs the published version, not the draft. Cell edits
    made since the last publish will NOT be included in the run.

    - update_published_results: if True, refreshes the app cache with new
      results. Cannot be used together with input_params.
    - use_cached_sql: if False, forces all SQL cells to re-query the warehouse
    - input_params: dict of input overrides, e.g. {"date_filter": "2026-01-01"}.
      Only parameters exposed in the published app can be set. Cannot be
      combined with update_published_results or view_id.
    - view_id: run with the inputs from a saved view (mutually exclusive with
      input_params)
    - dry_run: validate the call structure without actually running

    Rate limit: 20 requests/min, 60/hr.
    """
    body: dict[str, Any] = {
        "dryRun": dry_run,
        "updatePublishedResults": update_published_results,
        "useCachedSqlResults": use_cached_sql,
    }
    if input_params:
        body["inputParams"] = input_params
    if view_id:
        body["viewId"] = view_id

    result = _hex("POST", f"/projects/{project_id}/runs", data=body)
    run_id = result.get("runId")
    return {
        "run_id": run_id,
        "status": result.get("status", "PENDING"),
        "run_url": result.get("runUrl"),
        "run_status_url": result.get("runStatusUrl"),
        "project_version": result.get("projectVersion"),
        "trace_id": result.get("traceId"),
        "next_step": f"Poll with get_run_status('{project_id}', '{run_id}')",
    }


@mcp.tool()
def get_run_status(project_id: str, run_id: str) -> dict:
    """
    Check the status of a project run.

    Status values: PENDING | RUNNING | COMPLETED | ERRORED | KILLED |
                   UNABLE_TO_ALLOCATE_KERNEL

    Also returns start/end times, elapsed duration, and how the run
    was triggered (API | SCHEDULED | APP_REFRESH).
    """
    r = _hex("GET", f"/projects/{project_id}/runs/{run_id}")
    return {
        "run_id": r["runId"],
        "status": r["status"],
        "run_trigger": r.get("runTrigger"),
        "project_version": r.get("projectVersion"),
        "start_time": r.get("startTime"),
        "end_time": r.get("endTime"),
        "elapsed_ms": r.get("elapsedTime"),
        "run_url": r.get("runUrl"),
        "trace_id": r.get("traceId"),
    }


@mcp.tool()
def cancel_run(project_id: str, run_id: str) -> dict:
    """Cancel a running or pending project run."""
    _hex("DELETE", f"/projects/{project_id}/runs/{run_id}")
    return {"status": "cancelled", "run_id": run_id}


@mcp.tool()
def get_project_runs(
    project_id: str,
    status_filter: str | None = None,
    limit: int = 10,
) -> dict:
    """
    Get recent runs for a project (all trigger types by default).

    - status_filter: PENDING | RUNNING | ERRORED | COMPLETED | KILLED
    - limit: max results (1-100)
    """
    params: dict[str, Any] = {"limit": min(limit, 100)}
    if status_filter:
        params["statusFilter"] = status_filter.upper()

    result = _hex("GET", f"/projects/{project_id}/runs", params=params)
    runs = result.get("runs", [])
    return {
        "count": len(runs),
        "runs": [
            {
                "run_id": r["runId"],
                "status": r["status"],
                "run_trigger": r.get("runTrigger"),
                "start": r.get("startTime"),
                "end": r.get("endTime"),
                "elapsed_ms": r.get("elapsedTime"),
            }
            for r in runs
        ],
    }


# -------------------------------------------------------------------
# CHART IMAGE RETRIEVAL
# -------------------------------------------------------------------

@mcp.tool()
def get_chart_image(
    project_id: str,
    run_id: str,
    static_cell_id: str,
) -> dict:
    """
    Get the rendered PNG of a chart cell from a COMPLETED run.

    Use the cell's staticId (stable across versions), NOT the regular cell id.
    You can find staticId in the list_cells() output.

    Returns base64-encoded PNG image data and MIME type.
    The run must have status=COMPLETED before calling this.
    """
    result = _hex(
        "GET",
        f"/projects/{project_id}/runs/{run_id}/cells/{static_cell_id}/image",
    )
    return {
        "static_cell_id": result.get("staticId"),
        "mime_type": result.get("mimeType"),
        "image_base64_length": len(result.get("imageBase64", "")),
        "image_base64": result.get("imageBase64", ""),
    }


@mcp.tool()
def get_cell_image_live(cell_id: str) -> dict:
    """
    Get the rendered PNG of a chart cell from the CURRENT notebook session.

    Uses the regular cell id (NOT staticId) — the version-scoped id from
    list_cells() output.

    This returns the chart as it currently appears in the Logic view,
    which reflects the draft state rather than a specific completed run.
    Use get_chart_image() for images from a specific run.
    """
    result = _hex("GET", f"/cells/{cell_id}/image")
    return {
        "cell_id": result.get("id"),
        "mime_type": result.get("mimeType"),
        "image_base64_length": len(result.get("imageBase64", "")),
        "image_base64": result.get("imageBase64", ""),
    }


# -------------------------------------------------------------------
# DATA CONNECTIONS  (inspect which warehouses are available)
# -------------------------------------------------------------------

@mcp.tool()
def list_data_connections(limit: int = 50) -> dict:
    """
    List all data connections in the workspace.

    Returns connection id, name, and type (snowflake, bigquery, postgres, etc.).
    Use the id when creating SQL cells that need a data_connection_id.
    """
    result = _hex("GET", "/data-connections", params={"limit": min(limit, 100)})
    connections = result.get("values", [])
    return {
        "count": len(connections),
        "connections": [
            {
                "id": c["id"],
                "name": c["name"],
                "type": c.get("type", ""),
                "description": c.get("description", ""),
            }
            for c in connections
        ],
    }


@mcp.tool()
def get_data_connection(connection_id: str) -> dict:
    """
    Get full details for a data connection: type, schema filters,
    refresh schedule, sharing settings, and SSH tunnel status.
    """
    c = _hex("GET", f"/data-connections/{connection_id}")
    return {
        "id": c["id"],
        "name": c["name"],
        "type": c.get("type"),
        "description": c.get("description"),
        "ssh_tunnel": c.get("connectViaSsh", False),
        "magic_enabled": c.get("includeMagic", False),
        "writeback_enabled": c.get("allowWritebackCells", False),
        "schema_filters": c.get("schemaFilters"),
        "refresh_schedule": c.get("schemaRefreshSchedule"),
    }


# -------------------------------------------------------------------
# CONVENIENCE / POWER TOOLS
# -------------------------------------------------------------------

@mcp.tool()
def find_and_replace(
    project_id: str,
    find: str,
    replace: str,
    cell_ids: list[str] | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Find and replace text across all (or specific) SQL/CODE/MARKDOWN cells.

    By default runs in dry_run=True mode so you can review changes first.
    Set dry_run=False to actually apply the replacements.

    - find: regex pattern to search for
    - replace: replacement string (supports regex backreferences like \\1)
    - cell_ids: limit to these cells (omit to scan all cells)
    - dry_run: preview changes without saving (default True)

    Returns a list of cells that would be (or were) modified.
    """
    result = _hex("GET", "/cells", params={
        "projectId": project_id,
        "limit": 100,
    })
    cells = result.get("values", [])

    changes = []
    for cell in cells:
        cell_type = cell.get("cellType", "").upper()
        if cell_type not in ("CODE", "SQL", "MARKDOWN"):
            continue
        if cell_ids and cell["id"] not in cell_ids:
            continue

        original = _extract_source(cell)
        updated = re.sub(find, replace, original)

        if updated != original:
            change = {
                "cell_id": cell["id"],
                "label": cell.get("label", ""),
                "type": cell_type,
                "replacements": len(re.findall(find, original)),
            }

            if not dry_run:
                body = {"contents": _build_cell_contents(cell_type, updated)}
                _hex("PATCH", f"/cells/{cell['id']}", data=body)
                change["applied"] = True

            changes.append(change)

    mode = "DRY RUN (no changes saved)" if dry_run else "APPLIED"
    return {
        "mode": mode,
        "cells_affected": len(changes),
        "changes": changes,
    }


@mcp.tool()
def notebook_outline(project_id: str) -> dict:
    """
    Get a high-level outline of the notebook structure.

    Shows each cell's position, type, label, and first line of source.
    Perfect for quickly orienting yourself in a large notebook.
    """
    result = _hex("GET", "/cells", params={
        "projectId": project_id,
        "limit": 100,
    })
    cells = result.get("values", [])

    outline = []
    for i, cell in enumerate(cells, 1):
        source = _extract_source(cell)
        first_line = source.split("\n")[0][:80] if source else ""
        outline.append({
            "position": i,
            "type": cell.get("cellType", "?"),
            "label": cell.get("label", ""),
            "first_line": first_line,
            "id": cell["id"],
            "staticId": cell.get("staticId"),
        })

    return {"total_cells": len(outline), "outline": outline}


@mcp.tool()
def duplicate_cell(
    project_id: str,
    cell_id: str,
    new_label: str | None = None,
) -> dict:
    """
    Duplicate an existing cell. The copy is placed immediately after
    the original. Useful for iterating on a query variant without
    losing the original.
    """
    original = _hex("GET", f"/cells/{cell_id}")
    cell_type = original.get("cellType", "CODE").upper()
    source = _extract_source(original)
    label = new_label or f"{original.get('label', '')} (copy)"
    data_connection_id = original.get("dataConnectionId") if cell_type == "SQL" else None

    body: dict[str, Any] = {
        "projectId": project_id,
        "cellType": cell_type,
        "label": label,
        "contents": _build_cell_contents(cell_type, source, data_connection_id),
        "location": {"insertAfterCellId": cell_id},
    }

    new_cell = _hex("POST", "/cells", data=body)
    return {
        "id": new_cell.get("id"),
        "staticId": new_cell.get("staticId"),
        "label": label,
        "type": cell_type,
    }


# ===================================================================
# Entrypoint
# ===================================================================

if __name__ == "__main__":
    # Default to stdio so the server works when a client spawns it as a
    # subprocess.  Set MCP_TRANSPORT=streamable-http (plus MCP_HOST / MCP_PORT)
    # to run it as a long-lived HTTP service instead.
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=transport,
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("MCP_PORT", "8000")),
        )
