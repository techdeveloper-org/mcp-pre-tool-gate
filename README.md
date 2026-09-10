![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue)
![License MIT](https://img.shields.io/badge/License-MIT-green)
![Part of claude-workflow-engine](https://img.shields.io/badge/Part%20of-claude--workflow--engine-orange)
![MCP](https://img.shields.io/badge/Protocol-MCP%20stdio-lightgrey)

# mcp-pre-tool-gate

A FastMCP server that acts as a pre-execution policy enforcement gate for Claude Code tool calls. Before any file-modification tool (Write, Edit, NotebookEdit) executes, `mcp-pre-tool-gate` runs 8 sequential policy checks — verifying checkpoint state, task breakdown, skill selection, Level 1 sync completion, Level 2 standards completion, and platform safety rules — and returns an allow or block decision with actionable error messages. It also injects dynamic skill context hints based on file extension, matches tool call patterns against a known failure knowledge base to prevent common mistakes before they cascade, and validates that a general-purpose subagent dispatch carries a real, skill-carrying library persona. This server is one of 13 MCP servers in the [Claude Workflow Engine](https://github.com/techdeveloper-org/claude-workflow-engine) ecosystem.

**No live hook triggers any of these tools.** `claude-workflow-engine`'s [ADR-006](https://github.com/techdeveloper-org/claude-workflow-engine/blob/main/docs/architecture/ADR-006-hook-free-execution.md) (Hook-Free Execution Model, executed 2026-08-04) removed the `PreToolUse`/`PostToolUse`/`UserPromptSubmit` hook registrations from `~/.claude/settings.json` to eliminate their per-tool-call latency. Every tool in this server — including `validate_tool_call` and `validate_agent_dispatch` — is explicitly invoked by the orchestrating Claude Code session as a deliberate, on-demand self-check, not triggered automatically by any live hook. Enforcement is opt-in: a session that never calls these tools gets no policy checks. See the "How It Works" section below for what an explicit call looks like in practice.

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [The 8 Policy Checks](#the-8-policy-checks)
- [Tool Reference](#tool-reference)
- [Installation](#installation)
- [Configuration](#configuration)
- [Invocation Model](#invocation-model)
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
- **Agent-persona dispatch validation** — `validate_agent_dispatch` checks a proposed general-purpose `Agent`/`Task` dispatch for a real `---persona---` block, that its declared `skills:` are actually carried (matching `skills/<name>/SKILL.md` read instructions in the prompt body), and — when a `claude-global-library` `knowledge-graph/_master/agents_all.json` registry can be resolved — that the declared skill set matches the named agent's real mandatory set. Fail-open by design when the registry can't be found; escape hatches (`[GENERIC-OK]`, skill/agent-authoring auto-exemption) preserved from the original hook-era policy
- **Zero-configuration startup** — paths resolved automatically via `path_resolver.py`; no required environment variables for basic operation
- **FastMCP stdio transport** — standard MCP JSON-RPC over stdio; works with any MCP-compatible client

---

## How It Works

There is no live hook wired into this server's path — every tool call below is something the orchestrating Claude Code session decides to make explicitly, as a self-check, before or after it takes some other action. See [Invocation Model](#invocation-model) for why.

```
Orchestrating session is about to call a tool (e.g., Write, Edit, Bash)
and explicitly chooses to self-check first by calling mcp-pre-tool-gate
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
    allowed: true  -->  session proceeds with the tool call it was about to make
    allowed: false -->  session sees the block reason and adjusts before proceeding
```

Optimization hints (Grep head_limit, Read offset/limit for large files) are appended as non-blocking `hints` regardless of the allow/block decision.

A second, independent self-check follows the same explicit-call model for agent dispatches:

```
Orchestrating session is about to spawn a general-purpose Agent/Task subagent
and explicitly chooses to self-check first by calling mcp-pre-tool-gate
         |
         v
validate_agent_dispatch(subagent_type, prompt, library_path)
         |
         +-- subagent_type is a named built-in (e.g. Explore, Plan)?
         |        YES --> allowed: true, no persona required
         |
         +-- prompt contains [GENERIC-OK], or targets authoring a new
         |   skills/{name}/SKILL.md or agents/{name}/agent.md?
         |        YES --> allowed: true (escape hatch / auto-exemption)
         |
         +-- '---persona---' block present, well-formed, 'skills:' non-empty,
         |   every declared skill has a matching 'skills/<name>/SKILL.md'
         |   read instruction in the prompt body?
         |        NO  --> allowed: false, blocks explain what is missing
         |
         +-- agents_all.json registry resolvable (library_path param ->
             GLOBAL_LIBRARY_PATH env var -> 'WORKING DIRECTORY & LIBRARY
             PATH:' line in the prompt) and the named agent found in it?
                  YES --> declared skills must cover every real mandatory +
                          cross-domain-mandatory skill, or allowed: false
                  NO  --> fail-open, allowed: true on the checks above alone
         |
         v
    allowed: true  -->  session proceeds with the dispatch
    allowed: false -->  session fixes the prompt per `blocks`/`detail` and re-checks
```

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
| `validate_agent_dispatch` | Validate a proposed general-purpose `Agent`/`Task` dispatch for a real, skill-carrying library persona; returns `{"allowed", "reason", "blocks", "detail"}` | `subagent_type` (str, default `""`), `prompt` (str, default `""`), `library_path` (str, optional `GLOBAL_LIBRARY_PATH` override, default `""`) |

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

## Invocation Model

`mcp-pre-tool-gate` registers as an MCP server (`~/.claude/settings.json` `mcpServers`) but is **not** wired to any Claude Code hook event. Register it like any other MCP server:

```json
{
  "mcpServers": {
    "pre-tool-gate": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-pre-tool-gate/server.py"],
      "env": {}
    }
  }
}
```

**Why no hook.** `claude-workflow-engine` previously ran this same policy logic as a synchronous `PreToolUse` hook — a Python process spawn on every single tool call, `matcher: ""`, 60s timeout, `async: false` (per the user's global instruction that all hooks must be synchronous). Combined with a `PostToolUse` hook on the same pattern, that was up to 120s of blocking latency before Claude ever saw a prompt, and a hook crash blocked the tool call outright. [ADR-006](https://github.com/techdeveloper-org/claude-workflow-engine/blob/main/docs/architecture/ADR-006-hook-free-execution.md) (Hook-Free Execution Model) removed the `PreToolUse`/`PostToolUse`/`UserPromptSubmit` registrations entirely (executed 2026-08-04) to eliminate that latency, and named "re-implement the check as an explicitly-invoked MCP tool" as the precedent pattern for restoring a check's *logic* without restoring the *hook*.

**What that means in practice:** every tool in this server — `validate_tool_call`, `validate_agent_dispatch`, and the rest — is a deterministic, pipe-testable self-check the orchestrating session chooses to call, not something that fires on its own. There is no live mechanism forcing that call to happen before a given `Write`/`Edit`/`Agent`/`Task` call; a session can simply not call it. What this server upgrades is "trust the policy was followed" to "a concrete tool call exists that will catch a violation if you make it" — it is a callable self-check, not a hard gate, and enforcement stays opt-in (the consequence ADR-006 states explicitly: "enforcement becomes opt-in. Policies do not apply on sessions where the plugin is never invoked").

**Example flow (agent dispatch):**

```
Orchestrating session, following ORCHESTRATION_TEMPLATE.md STEP 0.05's
Subagent Dispatch Contract, is about to call Agent/Task with
subagent_type="general-purpose"
       |
       v
Session explicitly calls mcp__pre-tool-gate__validate_agent_dispatch(
    subagent_type, prompt)
       |
       +-- allowed: true  -> session proceeds with the Agent/Task call
       |
       +-- allowed: false -> session fixes the prompt per `blocks`/`detail`,
                             then re-checks before dispatching
```

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

### Example 6: Agent dispatch blocked — no persona injected

```json
// Request
{
  "tool": "validate_agent_dispatch",
  "arguments": {
    "subagent_type": "general-purpose",
    "prompt": "go research competitor pricing"
  }
}

// Response
{
  "allowed": false,
  "reason": "[PRE-TOOL BLOCKED] Generic subagent spawned without a library persona!",
  "blocks": ["[PRE-TOOL BLOCKED] Generic subagent spawned without a library persona!\n  Tool       : Agent/Task spawn with subagent_type=general-purpose (or unset)\n  ..."],
  "detail": "[PRE-TOOL BLOCKED] Generic subagent spawned without a library persona!\n  ..."
}
```

### Example 7: Full enforcer state snapshot

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
├── server.py               # FastMCP server — 9 tools, 8 policy checks + agent-persona dispatch validation
├── input_validator.py      # Input sanitization (null-byte strip, length limits)
├── rate_limiter.py         # Token bucket rate limiter (optional enforcement)
├── mcp_errors.py           # MCP-specific error types
├── requirements.txt        # Runtime dependencies (mcp, fastmcp)
├── requirements-dev.txt    # Dev/test dependencies (pytest), on top of requirements.txt
├── tests/                  # pytest suite
│   ├── conftest.py         # sys.path setup so tests can `import server`
│   └── test_agent_persona_gate.py  # validate_agent_dispatch coverage
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
2. Keep `server.py` focused on the policy checks and tools it already owns — do not add unrelated functionality.
3. If you add a new policy check, add it as check #N in the `validate_tool_call` docstring and update the check counter in the return value.
4. All new tools must use the `@mcp_tool_handler` decorator from `base/decorators.py`.
5. Do not add runtime dependencies beyond `mcp` and `fastmcp` without discussion (test-only dependencies like `pytest` belong in `requirements-dev.txt`, not `requirements.txt`).
6. Run `pytest tests/` and test your changes by calling the relevant tool explicitly from a live Claude Code session (see [Invocation Model](#invocation-model) — there is no hook to configure).
7. Submit a pull request with a clear description of the change and the problem it solves.

---

## License

MIT License. See [LICENSE](LICENSE) for full text.

Copyright (c) 2026 techdeveloper-org
