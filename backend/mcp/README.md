# VERA NEPA MCP Server

An MCP (Model Context Protocol) server that gives any MCP-compatible chatbot
(Claude Desktop, etc.) structured access to the VERA NEPA SQLite database.

## Requirements

- Python 3.11+
- A populated `nepa.db` at the repo root (run `python backend/db/ingest.py` first)

## Installation

The project uses the virtualenv at `backend/venv`. Install the MCP package into it:

```bash
backend/venv/bin/pip install -r backend/mcp/requirements.txt
```

Or, if using a fresh venv:

```bash
python3 -m venv backend/venv
backend/venv/bin/pip install -r backend/mcp/requirements.txt
```

## Running

**stdio transport** (default — used by Claude Desktop and most MCP clients):

```bash
backend/venv/bin/python backend/mcp/server.py
```

**SSE transport** (HTTP, useful for debugging or custom integrations):

```bash
backend/venv/bin/python backend/mcp/server.py --sse --port 8001
```

## Wiring into Claude Desktop

Add the following block to your Claude Desktop MCP config file
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "vera-nepa": {
      "command": "/absolute/path/to/vera/backend/venv/bin/python",
      "args": ["/absolute/path/to/vera/backend/mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop after saving. The `vera-nepa` server will appear in the
tools list.

## Available Tools

| Tool | Purpose |
|---|---|
| `get_database_stats` | High-level overview — call this first |
| `search_projects` | Search/filter projects by keyword, type, agency, state, date |
| `get_project` | Full project detail: metadata, documents, milestones, flag summary |
| `list_agencies` | All agencies with project counts |
| `list_states` | All states with project counts |
| `get_project_documents` | Documents for a specific project |
| `search_document_content` | Full-text search inside document text |
| `get_project_flags` | Compliance flags for one project |
| `get_flags_across_projects` | Flags across all projects with filters |
| `get_project_timeline` | Milestone timeline for one project |

## Example Chatbot Queries

- "What's in the database?" → `get_database_stats`
- "Show me EIS projects in Colorado" → `search_projects(process_type="EIS", state="Colorado")`
- "Which BLM projects have high-severity flags?" → `get_flags_across_projects(agency="BLM", severity="high")`
- "Find projects mentioning tribal consultation" → `search_document_content("tribal consultation")`
- "What's the timeline for project X?" → `get_project_timeline(project_id="X")`
- "What compliance issues does project X have?" → `get_project_flags(project_id="X")`
