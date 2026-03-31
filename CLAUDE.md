# mcp-pre-tool-gate — Claude Project Context

**Type:** FastMCP Server
**Transport:** stdio
**Python:** 3.8+

---

## What This Server Does

Pre-execution policy enforcement gate for Claude Code. Validates all tool calls before execution, enforces 8 policy checks, provides dynamic skill hints, and detects failure patterns. Consolidates 2,027 LOC from pre-tool-enforcer.py into a FastMCP server.

---

## Entry Point

```
server.py
```

Run via `python server.py` — communicates over stdio using the MCP protocol.

---

## Available Tools

- `validate_tool_call` — Run all 8 pre-execution policy checks on a proposed tool call
- `check_task_breakdown` — Verify task has been broken into trackable phases/steps
- `check_skill_selected` — Confirm appropriate skill has been selected for the task
- `check_level_completion` — Check Level 1/2 pipeline stages are complete
- `get_enforcer_state` — Retrieve current enforcement flags and session state
- `check_failure_patterns` — Match against known failure patterns to provide hints
- `get_dynamic_skill_hint` — Return dynamic skill recommendation for current context
- `reset_enforcer_flags` — Reset all enforcement flags for a new task cycle

---

## Shared Utilities (in this repo)

- `base/` — Shared MCP infrastructure package (response builder, decorators, persistence, clients)
- `mcp_errors.py` — Structured error response helpers
- `input_validator.py` — Null-byte strip, length limits, prompt injection detection
- `rate_limiter.py` — Token bucket rate limiter (enable via ENABLE_RATE_LIMITING=1)

---

## Environment Variables

- `CLAUDE_SESSION_DIR` — Path to session storage directory (default: ~/.claude/sessions)
- `CLAUDE_POLICIES_DIR` — Path to policies directory (default: auto-detected)

---

## Development

### Running locally

```bash
# Install deps
pip install -r requirements.txt

# Run the MCP server (stdio mode)
python server.py
```

### Testing a tool call manually

```python
import subprocess, json

proc = subprocess.Popen(
    ["python", "server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
# Send MCP initialize + tool call via stdin
```

### File structure

```
mcp-pre-tool-gate/
+-- server.py          # Main FastMCP server (entry point)
+-- base/              # Shared base package (response, decorators, persistence, clients)
+-- mcp_errors.py      # Error helpers
+-- input_validator.py # Input validation
+-- rate_limiter.py    # Rate limiting
+-- requirements.txt
+-- .gitignore
+-- README.md
+-- CLAUDE.md
```

---

## Key Rules

1. Do NOT edit `base/` directly — it is a copy from `mcp-base` repo
2. To update shared utilities, edit in `mcp-base` and re-copy
3. Keep `server.py` as the single entry point
4. All tool handlers must use `@mcp_tool_handler` decorator for consistent error handling
5. All responses must use `success()` / `error()` / `MCPResponse` builder from `base.response`

---

**Last Updated:** 2026-03-31
