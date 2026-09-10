"""
Pre-Tool Gate MCP Server - Policy enforcement before every tool call.

Consolidates pre-tool-enforcer.py (2,027 LOC) into a FastMCP server.
Enforces 8 policy checks before allowing Write/Edit/NotebookEdit:

1. Checkpoint review pending (Level 3.3)
2. Task breakdown pending (Level 3.1) - TaskCreate must be called first
3. Skill/agent selection pending (Level 3.5) - Skill must be invoked first
4. Context read complete (Level 3.0) - Project context must be read
5. Level 1 sync complete - Session/context sync done
6. Level 2 standards complete - Standards loaded
7. Bash Windows command blocking (Level 3.7) - Block del/copy/dir/xcopy
8. Python Unicode blocking (Level 3.7) - Block non-ASCII in .py on Windows

Also provides:
- Dynamic skill context hints (file extension -> skill suggestion)
- Failure KB pattern matching (known failure prevention)
- Tool-specific optimization hints (Grep head_limit, Read offset/limit)
- Explicitly-invoked agent-persona dispatch validation (see TOOL 9) - this
  server has no live PreToolUse hook registration (claude-workflow-engine
  ADR-006, Hook-Free Execution Model), so every tool here, including TOOL 9,
  is called on-demand by the orchestrating session, never triggered
  automatically.

Backend: Direct file I/O (flag files, flow-trace.json, failure-kb.json)
Transport: stdio

Tools (9):
  validate_tool_call, check_task_breakdown, check_skill_selected,
  check_level_completion, get_enforcer_state, check_failure_patterns,
  get_dynamic_skill_hint, reset_enforcer_flags, validate_agent_dispatch
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils.path_resolver import get_claude_home, get_config_dir, get_policies_dir

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both names are probed so this server runs under either major version; the
# API used below (tool decorator, run(transport=...)) is identical in both.
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from base.response import to_json
from base.decorators import mcp_tool_handler
from base.persistence import SessionIdResolver

mcp = MCPServer(
    "pre-tool-gate",
    instructions="Policy enforcement gate - validates tool calls before execution"
)

# Paths
MEMORY_PATH = get_config_dir()
FLAG_DIR = get_claude_home()
CURRENT_SESSION_FILE = MEMORY_PATH / ".current-session.json"
LOGS_PATH = MEMORY_PATH / "logs" / "sessions"

# Failure knowledge base, resolved through path_resolver so CLAUDE_POLICIES_DIR
# is honored; defaults to ~/.claude/policies.
FAILURE_KB_FILE = (
    get_policies_dir() / "03-execution-system" / "failure-prevention" / "failure-kb.json"
)

# Read-only annotation reused by every tool that only inspects state on disk.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Singleton session resolver
_session_resolver = SessionIdResolver(MEMORY_PATH)

# Constants
CHECKPOINT_MAX_AGE_MINUTES = 60
FLAG_TTL_SECONDS = 90
BLOCKED_TOOLS = {"Write", "Edit", "NotebookEdit"}
ALWAYS_ALLOWED = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "Task"}

# Windows commands to block
WINDOWS_BLOCKED_CMDS = [
    "del ", "del/", "copy ", "copy/", "xcopy", "move ", "ren ", "rename ",
    "dir ", "dir/", "type ", "cls", "md ", "mkdir ", "rd ", "rmdir ",
    "attrib ", "cacls ", "comp ", "compact ", "diskcomp", "diskcopy",
    "format ", "label ", "mode ", "more ", "net ", "path ", "print ",
    "recover ", "replace ", "sort ", "subst ", "tree ", "vol ",
]

# ---------------------------------------------------------------------------
# TOOL 9 support: agent-persona dispatch validation constants.
#
# SIBLING IMPLEMENTATION NOTICE: this block, and _resolve_agents_registry /
# _find_agent_record / _extract_agent_name / _extract_declared_skills /
# _skill_read_path_present / validate_agent_dispatch below, are a faithful
# port of claude-workflow-engine/hooks/pre_tool_enforcer/policies/agent_persona.py's
# check_agent_persona(). That file is the sibling implementation -- if you
# change the semantics here, check whether it needs the same change there
# (and vice versa), or the two copies will silently diverge.
# ---------------------------------------------------------------------------

_GENERIC_TYPES = ("general-purpose", "claude", "")
_PERSONA_MARKER = "---persona---"
_ESCAPE_HATCH = "[GENERIC-OK]"

# Auto-exempt pattern: dispatches whose prompt targets writing a brand-new
# skills/{name}/SKILL.md or agents/{name}/agent.md file. There is never a
# persona to inject for these -- the file being authored IS the persona/skill,
# and it does not exist yet.
_SKILL_OR_AGENT_AUTHOR_PATTERN = re.compile(
    r"skills[/\\][a-z0-9][a-z0-9\-]*[/\\]SKILL\.md|agents[/\\][a-z0-9][a-z0-9\-]*[/\\]agent\.md",
    re.IGNORECASE,
)

# Persona block body: everything between '---persona---' and the next line
# that is just dashes (the template's closing '---').
_PERSONA_BLOCK_RE = re.compile(r"---persona---(.*?)^-{3,}\s*$", re.DOTALL | re.MULTILINE)

# 'skills:' field value, captured up to the next top-level 'key:' line (or
# end of block). Works for both inline ('skills: [a, b]') and YAML-list forms.
_SKILLS_FIELD_RE = re.compile(r"skills\s*:\s*(.*?)(?:\n[ \t]*[A-Za-z_][\w-]*\s*:|\Z)", re.DOTALL | re.IGNORECASE)

# A skill slug: lowercase, hyphen-separated words with zero or more hyphens.
# The quantifier must be zero-or-more ('*'), not one-or-more ('+'), or
# hyphen-less skill names (e.g. 'docker', 'kubernetes') are silently dropped.
_SKILL_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

# 'agent: <name>' line inside the persona block.
_AGENT_NAME_RE = re.compile(r"^\s*agent\s*:\s*([a-z0-9][a-z0-9\-]*)\s*$", re.IGNORECASE | re.MULTILINE)

# The mandated 'WORKING DIRECTORY & LIBRARY PATH: <path>. ...' line every
# dispatch prompt is supposed to start with.
_LIBRARY_PATH_LINE_RE = re.compile(r"WORKING DIRECTORY\s*&\s*LIBRARY PATH\s*:\s*([^\n]+?)(?:\.\s|\n|$)", re.IGNORECASE)

_BLOCK_MSG_NO_PERSONA = (
    "[PRE-TOOL BLOCKED] Generic subagent spawned without a library persona!\n"
    "  Tool       : Agent/Task spawn with subagent_type=general-purpose (or unset)\n"
    "  Problem    : No agents/{name}/agent.md persona was injected into the prompt.\n"
    "  Required   : Route to a library agent, read its Skill Dependencies, then\n"
    "               inject its persona as a '---persona---' YAML block at the top\n"
    "               of the prompt (see ORCHESTRATION_TEMPLATE.md STEP 0.05,\n"
    "               Subagent Dispatch Contract).\n"
    "  Escape hatch: prefix the prompt/description with [GENERIC-OK] for a\n"
    "               genuinely generic one-off task with no matching persona,\n"
    "               OR a recurring task with no persona to inject because the\n"
    "               persona/skill IS what the task produces (e.g. authoring a\n"
    "               new SKILL.md / agent.md for a library that doesn't have\n"
    "               that persona yet).\n"
    "  Note       : Local code exploration should use the built-in 'Explore'\n"
    "               subagent_type instead -- it is not gated by this policy.\n"
    "  Action     : Add subagent_type of a named library agent, or inject the\n"
    "               '---persona---' block, or prefix with [GENERIC-OK]."
)

_BLOCK_MSG_HOLLOW_PERSONA = (
    "[PRE-TOOL BLOCKED] Persona injected but its skills were not actually carried!\n"
    "  Tool       : Agent/Task spawn with subagent_type=general-purpose (or unset)\n"
    "  Problem    : {detail}\n"
    "  Required   : Per ORCHESTRATION_TEMPLATE.md STEP 0.05, the persona block's\n"
    "               'skills:' field must list the agent's full skill set (mandatory\n"
    "               AGENT_USES_SKILL edges + OPTIONAL_SKILL edges), and the prompt\n"
    "               body must instruct the subagent to READ each one at its real\n"
    "               'skills/<name>/SKILL.md' path before doing anything else. A\n"
    "               persona name alone is a hollow dispatch -- the subagent never\n"
    "               receives the skill knowledge it is supposed to apply.\n"
    "  Escape hatch: prefix the prompt/description with [GENERIC-OK] only if this\n"
    "               agent genuinely has zero skill dependencies (rare -- check its\n"
    "               agent.md Skill Dependencies section first)."
)

_BLOCK_MSG_INCOMPLETE_SKILLS = (
    "[PRE-TOOL BLOCKED] Persona's declared skills do not match its real mandatory set!\n"
    "  Tool       : Agent/Task spawn with subagent_type=general-purpose (or unset)\n"
    "  Agent      : {agent_name}\n"
    "  Problem    : knowledge-graph/_master/agents_all.json lists this agent's mandatory\n"
    "               skills as: {mandatory}\n"
    "               but the persona block's 'skills:' field is missing: {missing}\n"
    "  Required   : The 'skills:' field must include EVERY mandatory skill for this\n"
    "               agent (plus any task-relevant optional ones) -- a self-consistent\n"
    "               but incomplete or wrong skill list is still a hollow dispatch, just\n"
    "               one that passes a naive format check. Copy the mandatory list above\n"
    "               into 'skills:' and add a matching 'skills/<name>/SKILL.md' read\n"
    "               instruction for each.\n"
    "  Escape hatch: prefix the prompt/description with [GENERIC-OK] only if this is a\n"
    "               deliberate, reduced-scope dispatch that genuinely does not need the\n"
    "               full mandatory set for this specific subtask."
)


def _get_session_id() -> str:
    """Get current session ID via SessionIdResolver singleton."""
    return _session_resolver.get()


def _flag_paths(flag_name: str, session_id: str) -> list:
    """Return every filesystem location a flag may live at, newest scheme first.

    Args:
        flag_name: Flag identifier, e.g. 'task-breakdown-pending'.
        session_id: Session the flag belongs to.

    Returns:
        List of candidate Paths: the session-folder location followed by the
        legacy ~/.claude/ location.
    """
    return [
        LOGS_PATH / session_id / "flags" / f"{flag_name}.json",
        FLAG_DIR / f".{flag_name}-{session_id}.json",
    ]


def _unlink_flag(flag_name: str, session_id: str) -> None:
    """Delete a flag from every location it may occupy.

    Both the session-folder and the legacy location are removed, so an expired
    flag cannot be resurrected from whichever copy was left behind.
    """
    for path in _flag_paths(flag_name, session_id):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _find_flag(flag_name: str, session_id: str) -> Optional[dict]:
    """Find a session-specific flag file, auto-expiring stale flags.

    A flag older than CHECKPOINT_MAX_AGE_MINUTES is deleted from every location
    and treated as absent. Unreadable or malformed flag files are skipped rather
    than treated as active, so a corrupt flag cannot permanently block a tool.

    Args:
        flag_name: Flag identifier, e.g. 'checkpoint-pending'.
        session_id: Session the flag belongs to.

    Returns:
        The parsed flag dict when a live flag exists, otherwise None.
    """
    if not session_id:
        return None

    for flag_file in _flag_paths(flag_name, session_id):
        try:
            data = json.loads(flag_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        created = data.get("created_at", "")
        if created:
            try:
                age = datetime.now() - datetime.fromisoformat(created)
            except (TypeError, ValueError):
                return data
            if age > timedelta(minutes=CHECKPOINT_MAX_AGE_MINUTES):
                _unlink_flag(flag_name, session_id)
                return None
        return data

    return None


def _check_flag_with_ttl(flag_name: str, session_id: str, ttl_seconds: int = 90):
    """Check a flag against a short TTL, clearing it when expired.

    Args:
        flag_name: Flag identifier.
        session_id: Session the flag belongs to.
        ttl_seconds: Age past which the flag is considered expired.

    Returns:
        Tuple of (is_active, flag_data). Data is None when the flag is absent
        or expired.
    """
    data = _find_flag(flag_name, session_id)
    if not data:
        return False, None

    created = data.get("created_at", "")
    if created:
        try:
            age = (datetime.now() - datetime.fromisoformat(created)).total_seconds()
        except (TypeError, ValueError):
            return True, data
        if age > ttl_seconds:
            _unlink_flag(flag_name, session_id)
            return False, None

    return True, data


def _load_flow_trace(session_id: str) -> Optional[dict]:
    """Load latest flow-trace entry for session."""
    if not session_id:
        return None
    trace_file = LOGS_PATH / session_id / "flow-trace.json"
    if not trace_file.exists():
        return None
    try:
        data = json.loads(trace_file.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data[-1]
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _pipeline_step_present(trace: dict, step_name: str) -> bool:
    """Check if pipeline step exists in flow-trace."""
    if not trace:
        return False
    for entry in trace.get("pipeline", []):
        if entry.get("step") == step_name:
            return True
    return False


# =============================================================================
# TOOL 1: FULL VALIDATION PIPELINE
# =============================================================================

@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def validate_tool_call(
    tool_name: Annotated[str, Field(
        description="Tool about to be called, e.g. 'Write', 'Edit', 'Bash', "
                    "'Read', 'Grep'.")],
    tool_input: Annotated[str, Field(
        description="JSON object string of the proposed tool parameters. "
                    "Invalid JSON is treated as an empty parameter set.")] = "{}",
) -> str:
    """Run all policy checks for a tool call. Returns allow/block decision.

    Checks in order:
    1. Checkpoint pending (Level 3.3)
    2. Task breakdown pending (Level 3.1)
    3. Skill selection pending (Level 3.5)
    4. Context read complete (Level 3.0)
    5. Level 1 sync complete
    6. Level 2 standards complete
    7. Bash Windows command blocking (Level 3.7)
    8. Python Unicode blocking (Level 3.7)

    Args:
        tool_name: Tool being called (Write, Edit, Bash, Read, Grep, etc.)
        tool_input: JSON string of tool parameters
    """
    try:
        params = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
        params = {}

    session_id = _get_session_id()
    hints = []
    blocks = []

    # Always-allowed tools skip blocking checks
    if tool_name in ALWAYS_ALLOWED:
        return to_json({
            "allowed": True,
            "tool": tool_name,
            "hints": [],
            "blocks": [],
            "reason": "Tool is always allowed"
        })

    # Only run blocking checks for file-modification tools
    if tool_name in BLOCKED_TOOLS:
        # Check 1: Checkpoint pending
        flag = _find_flag("checkpoint-pending", session_id)
        if flag:
            blocks.append(f"Review checkpoint pending - user must confirm before {tool_name}")

        # Check 2: Task breakdown pending
        if not blocks:
            active, data = _check_flag_with_ttl("task-breakdown-pending", session_id, FLAG_TTL_SECONDS)
            if active:
                blocks.append(f"Task breakdown pending - call TaskCreate before {tool_name}")

        # Check 3: Skill selection pending
        if not blocks:
            active, data = _check_flag_with_ttl("skill-selection-pending", session_id, FLAG_TTL_SECONDS)
            if active:
                skill = data.get("required_skill", "unknown") if data else "unknown"
                blocks.append(f"Skill selection pending - invoke '{skill}' before {tool_name}")

        # Check 4-6: Flow-trace pipeline checks (fail-open if no trace)
        if not blocks:
            trace = _load_flow_trace(session_id)
            if trace:
                if not _pipeline_step_present(trace, "LEVEL_1_CONTEXT") and \
                   not _pipeline_step_present(trace, "LEVEL_1_SESSION"):
                    blocks.append("Level 1 Sync System not complete yet")
                elif not _pipeline_step_present(trace, "LEVEL_2_STANDARDS"):
                    blocks.append("Level 2 Standards System not complete yet")

    # Check 7: Bash Windows commands
    if tool_name == "Bash" and sys.platform == "win32":
        command = params.get("command", "").strip().lower()
        for blocked_cmd in WINDOWS_BLOCKED_CMDS:
            if command.startswith(blocked_cmd.strip().lower()):
                blocks.append(f"Windows command '{blocked_cmd.strip()}' blocked - use Unix equivalent")
                break

    # Check 8: Python Unicode blocking
    if tool_name in ("Write", "Edit") and sys.platform == "win32":
        file_path = params.get("file_path", "")
        content = params.get("content", "") or params.get("new_string", "")
        if file_path.endswith(".py") and content:
            try:
                content.encode("ascii")
            except UnicodeEncodeError:
                blocks.append("Non-ASCII characters in .py file on Windows (cp1252 unsafe)")

    # Optimization hints (non-blocking)
    if tool_name == "Grep":
        if not params.get("head_limit"):
            hints.append("Add head_limit to Grep (mandatory optimization)")
    elif tool_name == "Read":
        file_path = params.get("file_path", "")
        if file_path and not params.get("offset") and not params.get("limit"):
            p = Path(file_path)
            if p.exists() and p.is_file():
                try:
                    lines = sum(1 for _ in open(p, "rb"))
                    if lines > 500:
                        hints.append(f"Large file ({lines} lines) - use offset/limit")
                except Exception:
                    pass

    allowed = len(blocks) == 0

    return to_json({
        "allowed": allowed,
        "tool": tool_name,
        "session_id": session_id,
        "hints": hints,
        "blocks": blocks,
        "checks_run": 8 if tool_name in BLOCKED_TOOLS else 2,
        "reason": blocks[0] if blocks else "All checks passed"
    })


# =============================================================================
# TOOL 2: TASK BREAKDOWN CHECK
# =============================================================================

@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def check_task_breakdown() -> str:
    """Check if task breakdown is pending for current session."""
    try:
        session_id = _get_session_id()
        active, data = _check_flag_with_ttl("task-breakdown-pending", session_id, FLAG_TTL_SECONDS)
        return to_json({
            "success": True,
            "pending": active,
            "session_id": session_id,
            "flag_data": data,
            "ttl_seconds": FLAG_TTL_SECONDS
        })
    except Exception as e:
        return to_json({"success": False, "error": str(e)})


# =============================================================================
# TOOL 3: SKILL SELECTION CHECK
# =============================================================================

@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def check_skill_selected() -> str:
    """Check if skill/agent selection is pending for current session."""
    try:
        session_id = _get_session_id()
        active, data = _check_flag_with_ttl("skill-selection-pending", session_id, FLAG_TTL_SECONDS)
        return to_json({
            "success": True,
            "pending": active,
            "session_id": session_id,
            "required_skill": data.get("required_skill", "") if data else "",
            "required_type": data.get("required_type", "") if data else "",
        })
    except Exception as e:
        return to_json({"success": False, "error": str(e)})


# =============================================================================
# TOOL 4: LEVEL COMPLETION CHECK
# =============================================================================

@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def check_level_completion(
    level: Annotated[str, Field(
        description="Which pipeline levels to report on: 'level1', 'level2', "
                    "or 'all'.")] = "all",
) -> str:
    """Check if pipeline levels are complete in flow-trace.

    Args:
        level: 'level1', 'level2', or 'all'
    """
    try:
        session_id = _get_session_id()
        trace = _load_flow_trace(session_id)

        if not trace:
            return to_json({
                "success": True,
                "session_id": session_id,
                "trace_found": False,
                "message": "No flow-trace found (fail-open)"
            })

        l1_context = _pipeline_step_present(trace, "LEVEL_1_CONTEXT")
        l1_session = _pipeline_step_present(trace, "LEVEL_1_SESSION")
        l2_standards = _pipeline_step_present(trace, "LEVEL_2_STANDARDS")

        return to_json({
            "success": True,
            "session_id": session_id,
            "trace_found": True,
            "level1_context": l1_context,
            "level1_session": l1_session,
            "level1_complete": l1_context and l1_session,
            "level2_standards": l2_standards,
            "level2_complete": l2_standards,
            "all_complete": l1_context and l1_session and l2_standards
        })
    except Exception as e:
        return to_json({"success": False, "error": str(e)})


# =============================================================================
# TOOL 5: ENFORCER STATE
# =============================================================================

@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def get_enforcer_state() -> str:
    """Get current enforcer state snapshot (all flags + flow-trace status)."""
    try:
        session_id = _get_session_id()

        checkpoint = _find_flag("checkpoint-pending", session_id)
        task_active, task_data = _check_flag_with_ttl("task-breakdown-pending", session_id, FLAG_TTL_SECONDS)
        skill_active, skill_data = _check_flag_with_ttl("skill-selection-pending", session_id, FLAG_TTL_SECONDS)
        trace = _load_flow_trace(session_id)

        return to_json({
            "success": True,
            "session_id": session_id,
            "flags": {
                "checkpoint_pending": checkpoint is not None,
                "task_breakdown_pending": task_active,
                "skill_selection_pending": skill_active,
                "required_skill": skill_data.get("required_skill", "") if skill_data else ""
            },
            "pipeline": {
                "trace_exists": trace is not None,
                "level1_complete": (
                    _pipeline_step_present(trace, "LEVEL_1_CONTEXT") and
                    _pipeline_step_present(trace, "LEVEL_1_SESSION")
                ) if trace else False,
                "level2_complete": _pipeline_step_present(trace, "LEVEL_2_STANDARDS") if trace else False
            },
            "blocked_tools": sorted(BLOCKED_TOOLS),
            "always_allowed": sorted(ALWAYS_ALLOWED)
        })
    except Exception as e:
        return to_json({"success": False, "error": str(e)})


# =============================================================================
# TOOL 6: FAILURE KB PATTERNS
# =============================================================================

def _load_failure_kb() -> tuple:
    """Load the failure knowledge base keyed by tool name.

    Returns:
        Tuple of (kb_dict, error_message). The dict is empty and the message is
        populated when the file is absent or malformed, so the caller can report
        that the knowledge base was unavailable instead of silently emitting
        fewer hints.
    """
    try:
        data = json.loads(FAILURE_KB_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"Failure KB not found: {FAILURE_KB_FILE}"
    except (json.JSONDecodeError, OSError) as e:
        return {}, f"Failure KB unreadable: {str(e)[:150]}"
    if not isinstance(data, dict):
        return {}, "Failure KB root is not an object keyed by tool name"
    return data, ""


def _hints_from_kb(patterns: list, tool_name: str, params: dict) -> list:
    """Derive hints for a tool call from its knowledge-base patterns.

    Translation patterns are matched against the first token of a Bash command;
    every other pattern contributes its description as an advisory hint. Hints
    are always advisory - this function never blocks a call.

    Args:
        patterns: Knowledge-base entries recorded for this tool.
        tool_name: Tool being called.
        params: Parsed tool parameters.

    Returns:
        List of hint strings.
    """
    hints = []
    command = str(params.get("command", "")).strip().lower()
    first_token = command.split(maxsplit=1)[0] if command else ""

    for pattern in patterns:
        if not isinstance(pattern, dict):
            continue
        solution = pattern.get("solution") or {}
        if not isinstance(solution, dict):
            continue

        if solution.get("type") == "translate" and tool_name == "Bash":
            mapping = solution.get("mapping") or {}
            replacement = mapping.get(first_token) if isinstance(mapping, dict) else None
            if replacement:
                hints.append(
                    f"Translate Windows '{first_token}' -> Unix '{replacement}' "
                    f"(failure pattern {pattern.get('pattern_id', 'unknown')})"
                )
            continue

        description = pattern.get("description") or solution.get("description")
        if description:
            hints.append(
                f"{description} (failure pattern "
                f"{pattern.get('pattern_id', 'unknown')})"
            )
    return hints


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def check_failure_patterns(
    tool_name: Annotated[str, Field(
        description="Tool to look up in the failure knowledge base, e.g. "
                    "'Edit', 'Read', 'Bash', 'Grep'.")],
    tool_input: Annotated[str, Field(
        description="JSON object string of the proposed tool parameters, "
                    "matched against known failure patterns.")] = "{}",
) -> str:
    """Check known failure patterns from failure-kb.json for a tool call.

    Returns non-blocking hints about known failure patterns.

    Args:
        tool_name: Tool name (Edit, Read, Bash, etc.)
        tool_input: JSON string of tool parameters
    """
    try:
        params = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
        params = {}

    hints = []
    kb, kb_error = _load_failure_kb()
    tool_patterns = kb.get(tool_name, []) if isinstance(kb, dict) else []

    hints.extend(_hints_from_kb(tool_patterns, tool_name, params))

    # Built-in checks that the knowledge base does not encode.
    if tool_name == "Edit":
        old_str = params.get("old_string", "")
        if old_str and re.match(r"^\s*\d+", old_str):
            hints.append("Edit old_string may contain line number prefix - strip before Edit")
        if not params.get("file_path"):
            hints.append("Edit requires file_path parameter")

    elif tool_name == "Grep":
        if not params.get("head_limit"):
            hints.append("Grep without head_limit can return excessive output")

    elif tool_name == "Read":
        fp = params.get("file_path", "")
        if fp and not params.get("offset") and not params.get("limit"):
            hints.append("Consider adding offset/limit for large files")

    response = {
        "success": True,
        "tool": tool_name,
        "hints": sorted(set(hints)),
        "kb_loaded": bool(kb),
        "kb_path": str(FAILURE_KB_FILE),
        "kb_patterns_for_tool": len(tool_patterns),
        "patterns_checked": len(hints),
    }
    if kb_error:
        response["kb_warning"] = kb_error
    return to_json(response)


# =============================================================================
# TOOL 7: DYNAMIC SKILL HINT
# =============================================================================

# File extension -> skill mapping
_EXT_SKILL_MAP = {
    ".java": "java-spring-boot-microservices",
    ".kt": "kotlin-core",
    ".py": "python-core",
    ".ts": "typescript-core",
    ".tsx": "react-core",
    ".js": "javascript-core",
    ".jsx": "react-core",
    ".swift": "swiftui-core",
    ".xml": "android-xml-ui",
    ".css": "css-core",
    ".scss": "css-core",
    ".html": "html5-core",
    ".sql": "rdbms-core",
    ".yml": "docker",
    ".yaml": "kubernetes",
    ".tf": "kubernetes",
    ".md": None,
    ".json": "json-core",
    ".graphql": "graphql-core",
}


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def get_dynamic_skill_hint(
    file_path: Annotated[str, Field(
        description="Path of the file being worked on. The extension and "
                    "parent directories determine the suggested skill.")],
) -> str:
    """Get skill/agent hint based on file extension.

    Args:
        file_path: File path being accessed
    """
    try:
        path = Path(file_path)
        ext = path.suffix.lower()
        skill = _EXT_SKILL_MAP.get(ext)

        # Special cases. The workflow check inspects the parent directories,
        # not the filename: a filename can never contain a path separator, so
        # matching '.github/workflows' against the name alone never fires.
        name = path.name.lower()
        parents = [p.lower() for p in path.parts[:-1]]
        if name == "dockerfile" or name.endswith(".dockerfile"):
            skill = "docker"
        elif name == "jenkinsfile":
            skill = "jenkins-pipeline"
        elif ".github" in parents and "workflows" in parents:
            skill = "github-actions-ci"

        return to_json({
            "success": True,
            "file": file_path,
            "extension": ext,
            "suggested_skill": skill,
            "has_suggestion": skill is not None
        })
    except Exception as e:
        return to_json({"success": False, "error": str(e)})


# =============================================================================
# TOOL 8: RESET FLAGS
# =============================================================================

@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def reset_enforcer_flags(
    flag_name: Annotated[str, Field(
        description="Flag to clear: 'task-breakdown-pending', "
                    "'skill-selection-pending', 'checkpoint-pending', or 'all' "
                    "to clear every flag for the current session.")] = "all",
) -> str:
    """Reset enforcement flags for current session.

    Args:
        flag_name: 'task-breakdown-pending', 'skill-selection-pending',
                   'checkpoint-pending', or 'all'
    """
    try:
        session_id = _get_session_id()
        if not session_id:
            return to_json({"success": False, "error": "No active session"})

        flags_to_clear = []
        if flag_name == "all":
            flags_to_clear = ["task-breakdown-pending", "skill-selection-pending", "checkpoint-pending"]
        else:
            flags_to_clear = [flag_name]

        cleared = []
        for fname in flags_to_clear:
            # New location
            fp = LOGS_PATH / session_id / "flags" / f"{fname}.json"
            if fp.exists():
                fp.unlink(missing_ok=True)
                cleared.append(fname)
            # Legacy location
            legacy = FLAG_DIR / f".{fname}-{session_id}.json"
            if legacy.exists():
                legacy.unlink(missing_ok=True)
                if fname not in cleared:
                    cleared.append(fname)

        return to_json({
            "success": True,
            "session_id": session_id,
            "cleared": cleared,
            "count": len(cleared)
        })
    except Exception as e:
        return to_json({"success": False, "error": str(e)})


# =============================================================================
# TOOL 9: AGENT PERSONA DISPATCH VALIDATION
# =============================================================================
#
# Port of check_agent_persona() -- see the SIBLING IMPLEMENTATION NOTICE above
# the TOOL 9 support constants for the anti-drift pointer to the original.


def _resolve_agents_registry(prompt: str, library_path: str = "") -> Optional[list]:
    """Best-effort locate and parse knowledge-graph/_master/agents_all.json.

    Tries, in order: the explicit `library_path` override parameter, the
    GLOBAL_LIBRARY_PATH environment variable, then the 'WORKING DIRECTORY &
    LIBRARY PATH: <path>' line ORCHESTRATION_TEMPLATE.md mandates at the top
    of every dispatch prompt. Returns the parsed list of agent records, or
    None if no candidate path resolves to a readable, parseable registry.

    This tool is called from every project, not just projects that use
    claude-global-library -- callers MUST treat None as fail-open (skip the
    ground-truth check entirely), never as a reason to block. A project that
    does not use this library, or whose library copy is unreachable for some
    incidental reason, must not have its dispatches blocked by an
    infrastructure lookup failure.

    Args:
        prompt: The dispatch prompt text to search for a library path line.
        library_path: Optional explicit override, highest priority. Exists
            because this server is a single long-lived stdio process per
            session -- an env var resolved once at process start could go
            stale or be absent for the session, unlike the original hook
            which re-resolved the environment on every process spawn.

    Returns:
        Parsed list of agent records, or None if unresolvable.
    """
    candidates = []
    if library_path:
        candidates.append(library_path)
    env_path = os.environ.get("GLOBAL_LIBRARY_PATH")
    if env_path:
        candidates.append(env_path)
    path_match = _LIBRARY_PATH_LINE_RE.search(prompt)
    if path_match:
        candidates.append(path_match.group(1).strip().strip('"').strip("'"))

    for base in candidates:
        try:
            registry_path = os.path.join(base, "knowledge-graph", "_master", "agents_all.json")
            if not os.path.isfile(registry_path):
                continue
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        agents = data.get("agents") if isinstance(data, dict) else data
        if isinstance(agents, list) and agents:
            return agents
    return None


def _find_agent_record(agents: list, name: str) -> Optional[dict]:
    """Return the agent record whose 'name' field matches (case-insensitive),
    or None if not found. `agents` is the list _resolve_agents_registry returns.
    """
    lowered = name.strip().lower()
    for record in agents:
        if isinstance(record, dict) and str(record.get("name", "")).strip().lower() == lowered:
            return record
    return None


def _extract_agent_name(persona_block_text: str) -> Optional[str]:
    """Return the 'agent: <name>' value from a persona block, or None."""
    match = _AGENT_NAME_RE.search(persona_block_text)
    return match.group(1).strip().lower() if match else None


def _extract_declared_skills(persona_block_text: str) -> list:
    """Return the deduped, lowercased skill slugs listed in a persona block's
    'skills:' field. Empty list if the field is missing, empty, or unparseable.
    """
    field_match = _SKILLS_FIELD_RE.search(persona_block_text)
    if not field_match:
        return []
    tokens = _SKILL_TOKEN_RE.findall(field_match.group(1))
    declared = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in declared:
            declared.append(lowered)
    return declared


def _skill_read_path_present(prompt: str, skill: str) -> bool:
    """True if the prompt instructs reading skill's real SKILL.md path."""
    pattern = re.compile(r"skills[/\\]" + re.escape(skill) + r"[/\\]SKILL\.md", re.IGNORECASE)
    return bool(pattern.search(prompt))


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def validate_agent_dispatch(
    subagent_type: Annotated[str, Field(
        description="The subagent_type parameter of the proposed Agent/Task "
                    "dispatch, e.g. 'general-purpose', 'Explore', or empty.")] = "",
    prompt: Annotated[str, Field(
        description="Full prompt (or description) text of the proposed "
                    "dispatch, including any '---persona---' block.")] = "",
    library_path: Annotated[str, Field(
        description="Optional override for GLOBAL_LIBRARY_PATH -- the "
                    "absolute path to claude-global-library used to resolve "
                    "knowledge-graph/_master/agents_all.json for the "
                    "ground-truth mandatory-skill check. Falls back to the "
                    "GLOBAL_LIBRARY_PATH environment variable, then to a "
                    "'WORKING DIRECTORY & LIBRARY PATH:' line inside prompt.")] = "",
) -> str:
    """Validate a proposed general-purpose subagent dispatch for a real,
    skill-carrying library persona, mirroring claude-workflow-engine's
    (now unreachable, per ADR-006) PreToolUse check_agent_persona() policy.

    This tool is explicitly invoked by the orchestrating session before a
    general-purpose Agent/Task dispatch -- no live hook calls it. Per
    ORCHESTRATION_TEMPLATE.md STEP 0.05's Subagent Dispatch Contract, call
    this before issuing an Agent/Task spawn with subagent_type unset,
    'general-purpose', or 'claude'; if `allowed` is false, fix the prompt per
    `blocks`/`detail` and re-check before dispatching.

    Three failure modes are gated (fail-closed unless an escape hatch or an
    auto-exemption applies):
      1. No '---persona---' block at all.
      2. A '---persona---' block present but hollow -- no 'skills:' field, an
         empty one, or declared skills with no matching
         'skills/<name>/SKILL.md' read instruction in the prompt body.
      3. A '---persona---' block that is internally self-consistent but still
         wrong -- it names a real library agent yet omits one or more of that
         agent's actual mandatory skills per
         knowledge-graph/_master/agents_all.json. This check is fail-open: if
         the registry cannot be located, or the named agent is not found in
         it, the dispatch is allowed through on checks 1-2 alone.

    Named built-in subagent types (e.g. Explore, Plan) are never gated. An
    explicit '[GENERIC-OK]' marker in the prompt/description is an escape
    hatch. Dispatches that target writing a new skills/{name}/SKILL.md or
    agents/{name}/agent.md file are auto-exempt (no marker needed) when no
    persona block is present at all.

    Args:
        subagent_type: The proposed dispatch's subagent_type.
        prompt: The proposed dispatch's prompt (or description) text.
        library_path: Optional GLOBAL_LIBRARY_PATH override.

    Returns:
        JSON string: {"allowed": bool, "reason": str, "blocks": [...],
        "detail": str}.
    """
    blocked = False
    message = ""

    if subagent_type.strip().lower() not in _GENERIC_TYPES:
        blocked, message = False, ""
    elif _ESCAPE_HATCH in prompt:
        blocked, message = False, ""
    elif _PERSONA_MARKER not in prompt:
        # The skill/agent-authoring exemption only applies to prompts with no
        # persona block: its premise is "the target agent.md/SKILL.md doesn't
        # exist yet, so there is nothing to inject". Once a '---persona---'
        # block is present, that premise no longer holds.
        if _SKILL_OR_AGENT_AUTHOR_PATTERN.search(prompt):
            blocked, message = False, ""
        else:
            blocked, message = True, _BLOCK_MSG_NO_PERSONA
    else:
        block_match = _PERSONA_BLOCK_RE.search(prompt)
        if not block_match:
            blocked, message = True, _BLOCK_MSG_HOLLOW_PERSONA.format(
                detail="'---persona---' marker found but the block never closes with "
                "a matching '---' line, so no skills: field could be parsed."
            )
        else:
            declared_skills = _extract_declared_skills(block_match.group(1))
            if not declared_skills:
                blocked, message = True, _BLOCK_MSG_HOLLOW_PERSONA.format(
                    detail="the persona block has no 'skills:' field (or it is empty) -- "
                    "no skill knowledge is being injected into the subagent."
                )
            else:
                missing = [s for s in declared_skills if not _skill_read_path_present(prompt, s)]
                if missing:
                    blocked, message = True, _BLOCK_MSG_HOLLOW_PERSONA.format(
                        detail="skills declared in the persona block have no matching "
                        "'skills/<name>/SKILL.md' read instruction in the prompt: " + ", ".join(missing)
                    )
                else:
                    agents = _resolve_agents_registry(prompt, library_path)
                    if agents is not None:
                        agent_name = _extract_agent_name(block_match.group(1))
                        if agent_name is not None:
                            record = _find_agent_record(agents, agent_name)
                            if record is not None:
                                # In-domain mandatory skills live in "mandatory_skills"; a skill
                                # whose SKILL.md sits in a different domain's KG is instead
                                # recorded under "cross_domain_mandatory_skills" -- both are
                                # equally mandatory.
                                in_domain = record.get("mandatory_skills")
                                cross_domain = record.get("cross_domain_mandatory_skills")
                                mandatory = list(in_domain or [])
                                for skill in cross_domain or []:
                                    if skill not in mandatory:
                                        mandatory.append(skill)
                                if isinstance(mandatory, list) and mandatory:
                                    declared_set = set(declared_skills)
                                    missing_mandatory = [s for s in mandatory if s.lower() not in declared_set]
                                    if missing_mandatory:
                                        blocked, message = True, _BLOCK_MSG_INCOMPLETE_SKILLS.format(
                                            agent_name=agent_name,
                                            mandatory=", ".join(mandatory),
                                            missing=", ".join(missing_mandatory),
                                        )

    return to_json({
        "allowed": not blocked,
        "reason": message.splitlines()[0] if message else "All checks passed",
        "blocks": [message] if message else [],
        "detail": message,
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
