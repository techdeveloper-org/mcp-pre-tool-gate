# mcp-pre-tool-gate

A FastMCP server providing **Pre Tool Gate** capabilities for Claude Code workflows.

---

## Overview

Pre-execution policy enforcement gate for Claude Code. Validates all tool calls before execution, enforces 8 policy checks, provides dynamic skill hints, and detects failure patterns. Consolidates 2,027 LOC from pre-tool-enforcer.py into a FastMCP server.

---

## Tools

| Tool | Description |
|------|-------------|
| `validate_tool_call` | Run all 8 pre-execution policy checks on a proposed tool call |
| `check_task_breakdown` | Verify task has been broken into trackable phases/steps |
| `check_skill_selected` | Confirm appropriate skill has been selected for the task |
| `check_level_completion` | Check Level 1/2 pipeline stages are complete |
| `get_enforcer_state` | Retrieve current enforcement flags and session state |
| `check_failure_patterns` | Match against known failure patterns to provide hints |
| `get_dynamic_skill_hint` | Return dynamic skill recommendation for current context |
| `reset_enforcer_flags` | Reset all enforcement flags for a new task cycle |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/techdeveloper-org/mcp-pre-tool-gate.git
cd mcp-pre-tool-gate
```

### 2. Install dependencies

```bash
pip install mcp fastmcp
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

---

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `CLAUDE_SESSION_DIR` | Path to session storage directory (default: ~/.claude/sessions) |
| `CLAUDE_POLICIES_DIR` | Path to policies directory (default: auto-detected) |

---

## Usage in Claude Code

Add to your `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "pre-tool-gate": {
      "command": "python",
      "args": [
        "/path/to/mcp-pre-tool-gate/server.py"
      ],
      "env": {}
    }
  }
}
```

---

## Benefits

- Prevents common mistakes before they happen (fail-fast at tool call time)
- Enforces workflow discipline: task breakdown, skill selection, level completion
- Provides actionable skill hints rather than cryptic errors
- Saves context window by catching issues before they cascade

---

## Requirements

- Python 3.8+
- `mcp fastmcp`
- See `requirements.txt` for pinned versions

---

## Project Context

This MCP server is part of the **Claude Workflow Engine** ecosystem — a LangGraph-based
orchestration pipeline for automating Claude Code development workflows.

Related repos:
- [`claude-workflow-engine`](https://github.com/techdeveloper-org/claude-workflow-engine) — Main pipeline
- [`mcp-base`](https://github.com/techdeveloper-org/mcp-base) — Shared base utilities used by all MCP servers

---

## License

Private — techdeveloper-org
