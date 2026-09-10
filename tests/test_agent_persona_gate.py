"""
test_agent_persona_gate.py - Unit tests for server.py's validate_agent_dispatch
MCP tool (TOOL 9).

SIBLING TEST SUITE: this file is a deliberate, scenario-parallel port of
claude-workflow-engine/tests/test_agent_persona_policy.py, which tests the
original (now hook-unreachable, per ADR-006) check_agent_persona() this tool
ports. Kept parallel on purpose -- a scenario added to one should be mirrored
into the other by inspection, per the anti-drift guardrail on
validate_agent_dispatch's own implementation in server.py.

Covers validate_agent_dispatch: blocks general-purpose (or unset)
subagent_type dispatches that lack an injected library persona, blocks a
'---persona---' block that names an agent but never actually carries its
skills (empty skills: field, or declared skills with no matching SKILL.md
read path), blocks a self-consistent-but-incomplete persona block that omits
a real mandatory skill for a known library agent (ground-truth cross-check
against a knowledge-graph/_master/agents_all.json registry resolved via the
`library_path` parameter, fail-open when that registry cannot be located --
this tool is called from every project, not just ones that use
claude-global-library), allows named built-in subagent types, allows the
'[GENERIC-OK]' escape hatch, and never raises on malformed input.

Unlike the source hook (which re-resolved GLOBAL_LIBRARY_PATH fresh on every
process spawn), this MCP server is a single long-lived stdio process per
session, so `validate_agent_dispatch` exposes an explicit `library_path`
parameter as the highest-priority override -- most of the ground-truth tests
below exercise that parameter directly rather than monkeypatching an
internal resolver function.

Windows-safe: ASCII only, no Unicode characters.
"""

import json

import server


def _call(subagent_type="", prompt="", library_path=""):
    """Invoke validate_agent_dispatch and parse its JSON response.

    Returns:
        Tuple of (allowed: bool, response: dict) for convenient assertions
        against both the top-level verdict and the full payload.
    """
    raw = server.validate_agent_dispatch(
        subagent_type=subagent_type, prompt=prompt, library_path=library_path
    )
    response = json.loads(raw)
    return response["allowed"], response


_COMPLIANT_PROMPT = (
    "---persona---\n"
    "agent: deep-web-researcher\n"
    "kg_route: rnd-intelligence\n"
    "skills: [research-methodology-core]\n"
    "---\n"
    "You are deep-web-researcher.\n\n"
    "FIRST, before doing anything, READ these skill files and apply them:\n"
    "- skills/research-methodology-core/SKILL.md\n\n"
    "TASK: research X"
)


class TestValidateAgentDispatch:
    def test_blocks_general_purpose_without_persona(self):
        allowed, response = _call(subagent_type="general-purpose", prompt="go research X")
        assert allowed is False
        assert "PRE-TOOL BLOCKED" in response["detail"]
        assert response["blocks"]

    def test_allows_when_persona_carries_its_skills(self):
        allowed, response = _call(subagent_type="general-purpose", prompt=_COMPLIANT_PROMPT)
        assert allowed is True
        assert response["detail"] == ""
        assert response["blocks"] == []

    def test_allows_when_generic_ok_escape_hatch_present(self):
        allowed, response = _call(
            subagent_type="general-purpose", prompt="[GENERIC-OK] one-off cleanup task"
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_allows_named_builtin_subagent_type(self):
        allowed, response = _call(subagent_type="Explore", prompt="find config loader")
        assert allowed is True
        assert response["detail"] == ""

    def test_blocks_when_subagent_type_missing_entirely(self):
        allowed, _ = _call(subagent_type="", prompt="go research X")
        assert allowed is False

    def test_blocks_when_subagent_type_is_claude(self):
        allowed, _ = _call(subagent_type="claude", prompt="go research X")
        assert allowed is False

    def test_blocks_empty_prompt(self):
        allowed, _ = _call(subagent_type="general-purpose", prompt="")
        assert allowed is False

    def test_allows_skill_md_authoring_without_marker(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt="Write the file `skills/r-statistical-computing-core/SKILL.md` for claude-global-library.",
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_allows_agent_md_authoring_without_marker(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt="Author agents/zig-mathematics-expert/agent.md for Domain 67.",
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_allows_skill_md_authoring_with_backslash_path(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt=r"Write skills\r-advanced-modeling-core\SKILL.md now.",
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_still_blocks_unrelated_generic_prompt_mentioning_skills_word(self):
        allowed, _ = _call(
            subagent_type="general-purpose",
            prompt="Review our skills and agents directory structure and summarize it.",
        )
        assert allowed is False

    # --- hollow-persona coverage (the actual bug this hardening closes) ---

    def test_blocks_persona_with_no_skills_field(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt="---persona---\nagent: deep-web-researcher\n---\nresearch X",
        )
        assert allowed is False
        assert "skills:" in response["detail"]

    def test_blocks_persona_with_empty_skills_list(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt="---persona---\nagent: deep-web-researcher\nskills: []\n---\nresearch X",
        )
        assert allowed is False
        assert "skills:" in response["detail"]

    def test_blocks_persona_with_declared_skill_but_no_read_path(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt=(
                "---persona---\nagent: deep-web-researcher\n"
                "skills: [research-methodology-core]\n---\nresearch X, no read instruction"
            ),
        )
        assert allowed is False
        assert "research-methodology-core" in response["detail"]

    def test_blocks_persona_with_unclosed_block(self):
        allowed, _ = _call(
            subagent_type="general-purpose",
            prompt="---persona---\nagent: deep-web-researcher\nskills: [research-methodology-core]\nno closing dashes here",
        )
        assert allowed is False

    def test_allows_persona_with_multiline_yaml_skills_list(self):
        prompt = (
            "---persona---\n"
            "agent: orchestrator-agent\n"
            "skills:\n"
            "  - ai-agents-core\n"
            "  - system-design\n"
            "---\n"
            "READ these skill files first:\n"
            "- skills/ai-agents-core/SKILL.md\n"
            "- skills/system-design/SKILL.md\n"
            "TASK: plan the workflow"
        )
        allowed, response = _call(subagent_type="general-purpose", prompt=prompt)
        assert allowed is True
        assert response["detail"] == ""

    def test_allows_persona_with_backslash_skill_read_path(self):
        prompt = (
            "---persona---\nagent: deep-web-researcher\n"
            "skills: [research-methodology-core]\n---\n"
            r"READ: skills\research-methodology-core\SKILL.md" + "\nTASK: research X"
        )
        allowed, response = _call(subagent_type="general-purpose", prompt=prompt)
        assert allowed is True
        assert response["detail"] == ""

    def test_blocks_persona_when_only_some_declared_skills_have_read_paths(self):
        prompt = (
            "---persona---\n"
            "agent: orchestrator-agent\n"
            "skills: [ai-agents-core, system-design]\n"
            "---\n"
            "READ:\n- skills/ai-agents-core/SKILL.md\n"
            "TASK: plan the workflow"
        )
        allowed, response = _call(subagent_type="general-purpose", prompt=prompt)
        assert allowed is False
        detail_lines = response["detail"].splitlines()
        problem_line = [line for line in detail_lines if "Problem" in line][0]
        assert "system-design" in problem_line
        assert "ai-agents-core" not in problem_line

    def test_escape_hatch_still_bypasses_hollow_persona_check(self):
        allowed, response = _call(
            subagent_type="general-purpose",
            prompt="[GENERIC-OK] ---persona---\nagent: x\n---\nno skills here, but explicitly generic-ok",
        )
        assert allowed is True
        assert response["detail"] == ""


_FAKE_REGISTRY_AGENTS = [
    {
        "name": "hallucination-detector",
        "mandatory_skills": ["hallucination-detection-core", "uncertainty-quantification-core"],
    },
    {
        "name": "orchestrator-agent",
        "mandatory_skills": ["ai-agents-core", "prompt-engineering-core", "system-design"],
    },
    {
        "name": "iam-security-engineer",
        "mandatory_skills": ["iam-security-core", "zero-trust-architecture-core"],
        "cross_domain_mandatory_skills": ["authentication-core"],
    },
    {
        "name": "python-backend-engineer",
        # 'docker' has zero hyphens -- this is the exact real-library shape
        # that regressed _SKILL_TOKEN_RE (issue: single-word skill names were
        # silently dropped from the parsed declared-skills list).
        "mandatory_skills": ["fastapi-core", "docker", "rdbms-core"],
    },
]


def _write_fake_registry(tmp_path, agents=_FAKE_REGISTRY_AGENTS):
    """Write a fixture knowledge-graph/_master/agents_all.json under tmp_path.

    Returns:
        The tmp_path itself, suitable to pass as the `library_path` override.
    """
    kg_dir = tmp_path / "knowledge-graph" / "_master"
    kg_dir.mkdir(parents=True)
    (kg_dir / "agents_all.json").write_text(
        json.dumps({"agents": agents}), encoding="utf-8"
    )
    return str(tmp_path)


def _compliant_prompt_for(agent_name, skills):
    skills_yaml = ", ".join(skills)
    reads = "\n".join("- skills/{0}/SKILL.md".format(s) for s in skills)
    return (
        "---persona---\n"
        "agent: {0}\n"
        "skills: [{1}]\n"
        "---\n"
        "READ these skill files first:\n"
        "{2}\n"
        "TASK: do the thing"
    ).format(agent_name, skills_yaml, reads)


class TestHyphenLessSkillNamesAreNotDropped:
    """Regression coverage for a real bug in the source: _SKILL_TOKEN_RE
    required at least one hyphen in every skill token, so single-word skill
    names like 'docker' or 'kubernetes' -- both real, currently-shipping
    skills in claude-global-library -- were silently dropped from the parsed
    declared-skills list even when present verbatim in the persona block's
    skills: field, causing a false INCOMPLETE_SKILLS block. See the fixed
    server._SKILL_TOKEN_RE quantifier ('*' not '+').
    """

    def test_extract_declared_skills_includes_hyphen_less_token(self):
        block_text = "\nagent: python-backend-engineer\nskills: [fastapi-core, docker, rdbms-core]\n"
        declared = server._extract_declared_skills(block_text)
        assert "docker" in declared, "hyphen-less skill token 'docker' must not be dropped"
        assert declared == ["fastapi-core", "docker", "rdbms-core"]

    def test_extract_declared_skills_includes_kubernetes(self):
        block_text = "\nskills: [nosql-core, kubernetes, redis-core]\n"
        declared = server._extract_declared_skills(block_text)
        assert "kubernetes" in declared

    def test_allows_dispatch_when_hyphen_less_mandatory_skill_is_declared(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for("python-backend-engineer", ["fastapi-core", "docker", "rdbms-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is True, "correctly-declared hyphen-less mandatory skill must not block dispatch: " + response["detail"]
        assert response["detail"] == ""

    def test_still_blocks_when_hyphen_less_mandatory_skill_is_genuinely_missing(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        # 'docker' genuinely omitted this time -- must still correctly block.
        prompt = _compliant_prompt_for("python-backend-engineer", ["fastapi-core", "rdbms-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is False
        assert "docker" in response["detail"]


class TestGroundTruthMandatorySkillCheck:
    """Covers failure mode 3: a persona block that is internally
    self-consistent (checks 1-2 pass) but omits a real mandatory skill for a
    known library agent, per a fixture agents_all.json registry resolved via
    the `library_path` parameter.
    """

    def test_blocks_when_registry_agent_missing_a_mandatory_skill(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for("hallucination-detector", ["hallucination-detection-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is False
        assert "uncertainty-quantification-core" in response["detail"]
        assert "hallucination-detector" in response["detail"]

    def test_allows_when_registry_agent_has_all_mandatory_skills(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for(
            "hallucination-detector", ["hallucination-detection-core", "uncertainty-quantification-core"]
        )
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_allows_extra_optional_skills_beyond_mandatory_set(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for(
            "hallucination-detector",
            ["hallucination-detection-core", "uncertainty-quantification-core", "rag-faithfulness-core"],
        )
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_blocks_when_persona_omits_a_cross_domain_mandatory_skill(self, tmp_path):
        """authentication-core lives in a different domain's KG than
        iam-security-engineer, so it is recorded under
        cross_domain_mandatory_skills rather than mandatory_skills -- the
        ground-truth check must still treat it as mandatory.
        """
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for("iam-security-engineer", ["iam-security-core", "zero-trust-architecture-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is False
        assert "authentication-core" in response["detail"]
        assert "iam-security-engineer" in response["detail"]

    def test_allows_when_persona_includes_the_cross_domain_mandatory_skill(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for(
            "iam-security-engineer",
            ["iam-security-core", "zero-trust-architecture-core", "authentication-core"],
        )
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_fails_open_when_agent_name_not_in_registry(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = _compliant_prompt_for("some-project-local-custom-agent", ["some-custom-skill-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_fails_open_when_registry_path_does_not_resolve(self, tmp_path):
        # library_path points at a real directory with no knowledge-graph/
        # subtree at all -- must fail open, not raise or block.
        prompt = _compliant_prompt_for("hallucination-detector", ["hallucination-detection-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=str(tmp_path)
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_fails_open_on_malformed_registry_json(self, tmp_path):
        kg_dir = tmp_path / "knowledge-graph" / "_master"
        kg_dir.mkdir(parents=True)
        (kg_dir / "agents_all.json").write_text("{not valid json", encoding="utf-8")
        prompt = _compliant_prompt_for("hallucination-detector", ["hallucination-detection-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=str(tmp_path)
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_escape_hatch_bypasses_mandatory_skill_check_too(self, tmp_path):
        library_path = _write_fake_registry(tmp_path)
        prompt = "[GENERIC-OK] " + _compliant_prompt_for("hallucination-detector", ["hallucination-detection-core"])
        allowed, response = _call(
            subagent_type="general-purpose", prompt=prompt, library_path=library_path
        )
        assert allowed is True
        assert response["detail"] == ""

    def test_real_lookup_with_no_library_path_or_env_var_fails_open(self, monkeypatch):
        # No library_path override and no GLOBAL_LIBRARY_PATH env var, and the
        # compliant prompt fixture carries no 'WORKING DIRECTORY & LIBRARY
        # PATH:' line -- _resolve_agents_registry must return None and the
        # dispatch must fail open rather than block or raise.
        monkeypatch.delenv("GLOBAL_LIBRARY_PATH", raising=False)
        allowed, response = _call(subagent_type="general-purpose", prompt=_COMPLIANT_PROMPT)
        assert allowed is True
        assert response["detail"] == ""


class TestResolveAgentsRegistry:
    """Direct coverage of server._resolve_agents_registry's three candidate
    sources and their priority order: library_path param > GLOBAL_LIBRARY_PATH
    env var > 'WORKING DIRECTORY & LIBRARY PATH:' line in the prompt.
    """

    def test_resolves_via_library_path_param(self, tmp_path):
        kg_dir = tmp_path / "knowledge-graph" / "_master"
        kg_dir.mkdir(parents=True)
        (kg_dir / "agents_all.json").write_text(
            '{"agents": [{"name": "foo-agent", "mandatory_skills": ["bar-core"]}]}',
            encoding="utf-8",
        )
        agents = server._resolve_agents_registry("irrelevant prompt text", str(tmp_path))
        assert agents == [{"name": "foo-agent", "mandatory_skills": ["bar-core"]}]

    def test_resolves_via_env_var(self, tmp_path, monkeypatch):
        kg_dir = tmp_path / "knowledge-graph" / "_master"
        kg_dir.mkdir(parents=True)
        (kg_dir / "agents_all.json").write_text(
            '{"agents": [{"name": "foo-agent", "mandatory_skills": ["bar-core"]}]}',
            encoding="utf-8",
        )
        monkeypatch.setenv("GLOBAL_LIBRARY_PATH", str(tmp_path))
        agents = server._resolve_agents_registry("irrelevant prompt text")
        assert agents == [{"name": "foo-agent", "mandatory_skills": ["bar-core"]}]

    def test_resolves_via_working_directory_line(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GLOBAL_LIBRARY_PATH", raising=False)
        kg_dir = tmp_path / "knowledge-graph" / "_master"
        kg_dir.mkdir(parents=True)
        (kg_dir / "agents_all.json").write_text(
            '{"agents": [{"name": "foo-agent", "mandatory_skills": ["bar-core"]}]}',
            encoding="utf-8",
        )
        prompt = "WORKING DIRECTORY & LIBRARY PATH: {0}. Read from here.\nTASK: x".format(str(tmp_path))
        agents = server._resolve_agents_registry(prompt)
        assert agents == [{"name": "foo-agent", "mandatory_skills": ["bar-core"]}]

    def test_library_path_param_takes_priority_over_env_var(self, tmp_path, monkeypatch):
        real_dir = tmp_path / "real"
        stale_dir = tmp_path / "stale"
        for base, name in ((real_dir, "real-agent"), (stale_dir, "stale-agent")):
            kg_dir = base / "knowledge-graph" / "_master"
            kg_dir.mkdir(parents=True)
            (kg_dir / "agents_all.json").write_text(
                json.dumps({"agents": [{"name": name, "mandatory_skills": []}]}),
                encoding="utf-8",
            )
        monkeypatch.setenv("GLOBAL_LIBRARY_PATH", str(stale_dir))
        agents = server._resolve_agents_registry("irrelevant prompt text", str(real_dir))
        assert agents[0]["name"] == "real-agent"

    def test_returns_none_when_nothing_resolves(self, monkeypatch):
        monkeypatch.delenv("GLOBAL_LIBRARY_PATH", raising=False)
        agents = server._resolve_agents_registry("no path info here at all")
        assert agents is None

    def test_returns_none_on_malformed_registry_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GLOBAL_LIBRARY_PATH", raising=False)
        kg_dir = tmp_path / "knowledge-graph" / "_master"
        kg_dir.mkdir(parents=True)
        (kg_dir / "agents_all.json").write_text("{not valid json", encoding="utf-8")
        prompt = "WORKING DIRECTORY & LIBRARY PATH: {0}. Read from here.\nTASK: x".format(str(tmp_path))
        agents = server._resolve_agents_registry(prompt)
        assert agents is None
