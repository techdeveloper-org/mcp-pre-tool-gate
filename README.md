![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue)
![License MIT](https://img.shields.io/badge/License-MIT-green)
![Part of claude-workflow-engine](https://img.shields.io/badge/Part%20of-claude--workflow--engine-orange)
![MCP](https://img.shields.io/badge/Protocol-MCP%20stdio-lightgrey)

# mcp-pre-tool-gate

A FastMCP server that acts as a pre-execution policy enforcement gate for Claude Code tool calls. Before any file-modification tool (Write, Edit, NotebookEdit) executes, `mcp-pre-tool-gate` runs 8 sequential policy checks — verifying checkpoint state, task breakdown, skill selection, Level 1 sync completion, Level 2 standards completion, and platform safety rules — and returns an allow or block decision with actionable error messages. It also injects dynamic skill context hints based on file extension and matches tool call patterns against a known failure knowledge base to prevent common mistakes before they cascade. This server is one of 13 MCP servers in the [Claude Workflow Engine](https://github.com/techdeveloper-org/claude-workflow-engine) ecosystem and is invoked automatically by the Claude Code `PreToolUse` hook.

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [The 8 Policy Checks](#the-8-policy-checks)
- [Tool Reference](#tool-reference)
- [Installation](#installation)
- [Configuration](#configuration)
- [PreToolUse Hook Integration](#pretooluse-hook-integration)
- [Usage Examples](#usage-examples)
- [Project Structure](#project-structure)
- [Part of Claude Workflow Engine](#part-of-claude-workflow-engine)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **8-check policy pipeline** — sequential validation covering checkpoint state, task workflow, pipeline sync, and platform safety
- **Fail-fast enforcement** — blocks Write / Edit / NotebookEdit before they execute, not after
- **Fail-open for read tools** — Read, Grep, Glob, WebFetch, WebSearch, and Task are always allowed; they never pass through the block checks
- **Dynamic skill hints** — maps file extensions to the appropriate skill/agent from the global library (`.java` → `java-spring-boot-microservices`, `.py` → `python-core`, etc.)
- **Failure KB pattern matching** — scans a JSON knowledge base of known failure patterns and returns non-blocking, actionable hints (e.g., Edit old_string contains line number prefix; Grep missing head_limit)
- **Windows platform safety** — blocks Windows-only shell commands in Bash (del, copy, xcopy, dir, move, etc.) and blocks non-ASCII content in `.py` files (cp1252 safety)
- **Session-scoped flag system** — enforcement flags are scoped to the active session ID and auto-expire via configurable TTL (default 90 seconds) and age limits (default 60 minutes)
- **Flow-trace awareness** — reads `flow-trace.json` to verify Level 1 sync and Level 2 standards pipeline steps completed; fails open if no trace is found
- **Zero-configuration startup** — paths resolved automatically via `path_resolver.py`; no required environment variables for basic operation
- **FastMCP stdio transport** — standard MCP JSON-RPC over stdio; works with any MCP-compatible client

---

## How It Works

```
Claude Code calls a tool (e.g., Write, Edit, Bash)
         |
         v
PreToolUse hook fires -> invokes mcp-pre-tool-gate
         |
         v
validate_tool_call(tool_name, tool_input)
         |
         +-- tool in ALWAYS_ALLOWED (Read/Grep/Glob/WebFetch/WebSearch/Task)?
         |         |
         |        YES --> allowed: true, return immediately (no checks)
         |
         +-- tool in BLOCKED_TOOLS (Write/Edit/NotebookEdit)?
         |         |
         |        YES --> run checks 1-6 sequentially
         |
         +-- tool == "Bash" on Windows?
         |        YES --> run check 7 (Windows command blocking)
         |
         +-- tool in (Write/Edit) on Windows?
                  YES --> run check 8 (Python Unicode blocking)
         |
         v
    allowed: true  -->  Claude Code proceeds with tool execution
    allowed: false -->  Claude Code shows block reason; tool does NOT execute
```

Optimization hints (Grep head_limit, Read offset/limit for large files) are appended as non-blocking `hints` regardless of the allow/block decision.

---

## The 8 Policy Checks

All checks run in order. The first failing check short-circuits the remainder (except checks 7 and 8, which are independent platform checks applied to their respective tool types).

| # | Check | Level | Trigger Condition | Blocked Tools |
|---|-------|-------|-------------------|---------------|
| 1 | Checkpoint pending | Level 3.3 | `checkpoint-pending` flag exists for session | Write, Edit, NotebookEdit |
| 2 | Task breakdown pending | Level 3.1 | `task-breakdown-pending` flag active (TTL 90s) | Write, Edit, NotebookEdit |
| 3 | Skill selection pending | Level 3.5 | `skill-selection-pending` flag active (TTL 90s) | Write, Edit, NotebookEdit |
| 4 | Context read complete | Level 3.0 | `LEVEL_1_CONTEXT` step absent from flow-trace | Write, Edit, NotebookEdit |
| 5 | Level 1 sync complete | Level 1 | `LEVEL_1_SESSION` step absent from flow-trace | Write, Edit, NotebookEdit |
| 6 | Level 2 standards complete | Level 2 | `LEVEL_2_STANDARDS` step absent from flow-trace | Write, Edit, NotebookEdit |
| 7 | Bash Windows command blocking | Level 3.7 | Command starts with a Windows-only shell command | Bash (Windows only) |
| 8 | Python Unicode blocking | Level 3.7 | Non-ASCII content written to a `.py` file | Write, Edit (Windows only) |

**Fail-open behaviour for checks 4-6:** If no `flow-trace.json` exists for the current session, these three checks are skipped and the tool call is allowed. This prevents blocking during the very first pipeline run before any trace has been written.

**Flag TTL:** Flags 2 and 3 use a short TTL (90 seconds by default). Flags older than `CHECKPOINT_MAX_AGE_MINUTES` (60 minutes) are auto-deleted on access.

---

## Tool Reference

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `validate_tool_call` | Run all 8 policy checks and return allow/block decision with reason and hints | `tool_name` (str, required), `tool_input` (JSON string, default `{}`) |
| `check_task_breakdown` | Check whether the task-breakdown-pending flag is active for the current session | None |
| `check_skill_selected` | Check whether the skill-selection-pending flag is active and return the required skill name | None |
| `check_level_completion` | Inspect flow-trace to verify Level 1 and/or Level 2 pipeline steps completed | `level` (str: `"level1"` \| `"level2"` \| `"all"`, default `"all"`) |
| `get_enforcer_state` | Snapshot of all enforcement flags, pipeline completion status, blocked tool sets, and always-allowed tool sets | None |
| `check_failure_patterns` | Match a tool call against known failure patterns (from `failure-kb.json`) and return non-blocking hints | `tool_name` (str, required), `tool_input` (JSON string, default `{}`) |
| `get_dynamic_skill_hint` | Return the suggested skill/agent name based on a file's extension | `file_path` (str, required) |
| `reset_enforcer_flags` | Delete enforcement flag files for the current session | `flag_name` (str: `"task-breakdown-pending"` \| `"skill-selection-pending"` \| `"checkpoint-pending"` \| `"all"`, default `"all"`) |

### File Extension to Skill Mapping

`get_dynamic_skill_hint` uses the following built-in mapping:

| Extension / Filename | Suggested Skill |
|----------------------|-----------------|
| `.java` | `java-spring-boot-microservices` |
| `.kt` | `kotlin-core` |
| `.py` | `python-core` |
| `.ts` | `typescript-core` |
| `.tsx`, `.jsx` | `react-core` |
| `.js` | `javascript-core` |
| `.swift` | `swiftui-core` |
| `.xml` | `android-xml-ui` |
| `.css`, `.scss` | `css-core` |
| `.html` | `html5-core` |
| `.sql` | `rdbms-core` |
| `.yml` | `docker` |
| `.yaml` | `kubernetes` |
| `.tf` | `kubernetes` |
| `.json` | `json-core` |
| `.graphql` | `graphql-core` |
| `Dockerfile`, `*.dockerfile` | `docker` |
| `Jenkinsfile` | `jenkins-pipeline` |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/techdeveloper-org/mcp-pre-tool-gate.git
cd mcp-pre-tool-gate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

The only runtime dependencies are `mcp>=1.0.0` and `fastmcp>=0.1.0`.

### 3. Verify startup

```bash
python server.py
```

The server starts and waits for JSON-RPC messages on stdin. No output is expected at startup. Press `Ctrl+C` to stop.

---

## Configuration

### Environment Variables

No environment variables are required for basic operation. The server resolves all paths automatically via `utils/path_resolver.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Base config directory for session files and flag files |

### Flag File Locations

Enforcement flags are written by the `pre-tool-enforcer.py` hook (or the `mcp-pre-tool-gate` consumer). The server reads from two locations (new takes precedence over legacy):

| Location | Path Pattern |
|----------|-------------|
| New (per-session) | `~/.claude/logs/sessions/{session_id}/flags/{flag_name}.json` |
| Legacy | `~/.claude/.{flag_name}-{session_id}.json` |

### Flow Trace Location

```
~/.claude/logs/sessions/{session_id}/flow-trace.json
```

The server reads the last entry in this file (if it is a list) or the entire document (if it is an object) to check pipeline step completion.

### TTL Constants (source-level)

| Constant | Default | Purpose |
|----------|---------|---------|
| `CHECKPOINT_MAX_AGE_MINUTES` | 60 | Maximum age for any flag before auto-deletion |
| `FLAG_TTL_SECONDS` | 90 | TTL for task-breakdown and skill-selection flags |

---

## PreToolUse Hook Integration

The recommended way to use `mcp-pre-tool-gate` is as a Claude Code MCP server triggered by the `PreToolUse` hook. Add the following to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "pre-tool-gate": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-pre-tool-gate/server.py"],
      "env": {}
    }
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /absolute/path/to/hooks/pre-tool-enforcer.py"
          }
        ]
      }
    ]
  }
}
```

**Hook flow:**

```
User prompt submitted
       |
       v
Claude Code decides to call a tool (e.g., Write a file)
       |
       v
PreToolUse hook fires
       |
       v
pre-tool-enforcer.py (hook shim)
  -> calls mcp_tool: pre-tool-gate / validate_tool_call
       |
       +-- allowed: true  -> Claude Code proceeds, tool executes normally
       |
       +-- allowed: false -> Claude Code receives block decision
                             Tool does NOT execute
                             Reason shown in terminal
```

The hook operates synchronously (`async: false`). Claude Code waits for the hook result before proceeding.

---

## Usage Examples

### Example 1: Write allowed after all checks pass

```json
// Request
{
  "tool": "validate_tool_call",
  "arguments": {
    "tool_name": "Write",
    "tool_input": "{\"file_path\": \"src/service.py\", \"content\": \"# service code\"}"
  }
}

// Response
{
  "allowed": true,
  "tool": "Write",
  "session_id": "session-20260414-143200",
  "hints": [],
  "blocks": [],
  "checks_run": 8,
  "reason": "All checks passed"
}
```

### Example 2: Write blocked — task breakdown pending

```json
// Request
{
  "tool": "validate_tool_call",
  "arguments": {
    "tool_name": "Write",
    "tool_input": "{\"file_path\": \"src/models.py\", \"content\": \"class User: pass\"}"
  }
}

// Response
{
  "allowed": false,
  "tool": "Write",
  "session_id": "session-20260414-143200",
  "hints": [],
  "blocks": ["Task breakdown pending - call TaskCreate before Write"],
  "checks_run": 8,
  "reason": "Task breakdown pending - call TaskCreate before Write"
}
```

### Example 3: Bash blocked on Windows — del command

```json
// Request
{
  "tool": "validate_tool_call",
  "arguments": {
    "tool_name": "Bash",
    "tool_input": "{\"command\": \"del /f /q build\\\\output.txt\"}"
  }
}

// Response
{
  "allowed": false,
  "tool": "Bash",
  "session_id": "session-20260414-143200",
  "hints": [],
  "blocks": ["Windows command 'del' blocked - use Unix equivalent"],
  "checks_run": 2,
  "reason": "Windows command 'del' blocked - use Unix equivalent"
}
```

### Example 4: Read is always allowed (no checks run)

```json
// Request
{
  "tool": "validate_tool_call",
  "arguments": {
    "tool_name": "Read",
    "tool_input": "{\"file_path\": \"src/main.py\"}"
  }
}

// Response
{
  "allowed": true,
  "tool": "Read",
  "hints": [],
  "blocks": [],
  "reason": "Tool is always allowed"
}
```

### Example 5: Skill hint for a TypeScript file

```json
// Request
{
  "tool": "get_dynamic_skill_hint",
  "arguments": {
    "file_path": "src/components/UserCard.tsx"
  }
}

// Response
{
  "success": true,
  "file": "src/components/UserCard.tsx",
  "extension": ".tsx",
  "suggested_skill": "react-core",
  "has_suggestion": true
}
```

### Example 6: Full enforcer state snapshot

```json
// Request
{
  "tool": "get_enforcer_state",
  "arguments": {}
}

// Response
{
  "success": true,
  "session_id": "session-20260414-143200",
  "flags": {
    "checkpoint_pending": false,
    "task_breakdown_pending": false,
    "skill_selection_pending": true,
    "required_skill": "python-core"
  },
  "pipeline": {
    "trace_exists": true,
    "level1_complete": true,
    "level2_complete": false
  },
  "blocked_tools": ["Edit", "NotebookEdit", "Write"],
  "always_allowed": ["Glob", "Grep", "Read", "Task", "WebFetch", "WebSearch"]
}
```

---

## Project Structure

```
mcp-pre-tool-gate/
├── server.py               # FastMCP server — 8 tools, 8 policy checks
├── input_validator.py      # Input sanitization (null-byte strip, length limits)
├── rate_limiter.py         # Token bucket rate limiter (optional enforcement)
├── mcp_errors.py           # MCP-specific error types
├── requirements.txt        # Runtime dependencies (mcp, fastmcp)
├── base/                   # Copy of mcp-base shared library
│   ├── response.py         # MCPResponse builder + to_json helper
│   ├── decorators.py       # @mcp_tool_handler decorator
│   └── persistence.py      # SessionIdResolver
└── CLAUDE.md               # Project-local Claude Code instructions
```

The `base/` directory is a copy of the [`mcp-base`](https://github.com/techdeveloper-org/mcp-base) shared library. Each MCP server in the ecosystem includes its own copy to remain independently deployable.

---

## Part of Claude Workflow Engine

`mcp-pre-tool-gate` is one of 13 MCP servers in the Claude Workflow Engine ecosystem — a LangGraph orchestration pipeline that automates Claude Code development workflows across planning, implementation, review, and release phases.

| # | Server | Purpose |
|---|--------|---------|
| 1 | [mcp-session-mgr](https://github.com/techdeveloper-org/mcp-session-mgr) | Session lifecycle management |
| 2 | [mcp-git-ops](https://github.com/techdeveloper-org/mcp-git-ops) | Git operations (branch, commit, push, diff) |
| 3 | [mcp-github-api](https://github.com/techdeveloper-org/mcp-github-api) | GitHub PR, issue, and merge operations |
| 4 | [mcp-policy-enforcement](https://github.com/techdeveloper-org/mcp-policy-enforcement) | Policy compliance and module health checks |
| 5 | [mcp-token-optimizer](https://github.com/techdeveloper-org/mcp-token-optimizer) | Token reduction via AST navigation (60-85% savings) |
| 6 | **[mcp-pre-tool-gate](https://github.com/techdeveloper-org/mcp-pre-tool-gate)** | **Pre-tool validation — this server** |
| 7 | [mcp-post-tool-tracker](https://github.com/techdeveloper-org/mcp-post-tool-tracker) | Post-tool progress tracking and commit readiness |
| 8 | [mcp-standards-loader](https://github.com/techdeveloper-org/mcp-standards-loader) | Standards loading with hot-reload |
| 9 | [mcp-uml-diagram](https://github.com/techdeveloper-org/mcp-uml-diagram) | UML diagram generation (13 types) |
| 10 | [mcp-drawio-diagram](https://github.com/techdeveloper-org/mcp-drawio-diagram) | Draw.io editable diagram export |
| 11 | [mcp-jira-api](https://github.com/techdeveloper-org/mcp-jira-api) | Jira issue lifecycle management |
| 12 | [mcp-jenkins-ci](https://github.com/techdeveloper-org/mcp-jenkins-ci) | Jenkins CI/CD trigger and monitoring |
| 13 | [mcp-figma](https://github.com/techdeveloper-org/mcp-figma) | Figma design token and component extraction |

Main pipeline: [claude-workflow-engine](https://github.com/techdeveloper-org/claude-workflow-engine)
Shared base library: [mcp-base](https://github.com/techdeveloper-org/mcp-base)

---

## Contributing

Contributions are welcome. Please follow these guidelines:

1. Fork the repository and create a feature branch from `main`.
2. Keep `server.py` focused on the 8 policy checks and 8 tools — do not add unrelated functionality.
3. If you add a new policy check, add it as check #N in the `validate_tool_call` docstring and update the check counter in the return value.
4. All new tools must use the `@mcp_tool_handler` decorator from `base/decorators.py`.
5. Do not add runtime dependencies beyond `mcp` and `fastmcp` without discussion.
6. Test your changes with a live Claude Code session using the PreToolUse hook configuration shown above.
7. Submit a pull request with a clear description of the change and the problem it solves.

---

## License

MIT License. See [LICENSE](LICENSE) for full text.

Copyright (c) 2026 techdeveloper-org
