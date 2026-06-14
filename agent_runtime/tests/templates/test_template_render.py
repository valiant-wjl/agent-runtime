"""Tests for runtime.template_render.render_template."""

import pathlib
import re

import pytest

from agent_runtime.template_render import TemplateError, render_template

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
META = REPO_ROOT / "templates" / "meta"
PROJECT = REPO_ROOT / "templates" / "project"


def test_render_user_substitution():
    out = render_template(META / "CLAUDE.md.template", {"USER_NAME": "Test User"})
    assert "Test User" in out
    assert "{{USER_NAME}}" not in out


def test_render_missing_variable_raises():
    with pytest.raises(TemplateError) as exc:
        render_template(META / "EVERGREEN.md.template", {"USER_NAME": "X"})
    assert "PROJECTS_LIST" in exc.value.missing_vars
    assert exc.value.path.name == "EVERGREEN.md.template"


def test_render_project_template():
    out = render_template(
        PROJECT / "CLAUDE.md.template",
        {"PROJECT_NAME": "billing", "PSM": "lark.apaas.spring_billing", "USER_NAME": "X"},
    )
    assert "billing" in out
    assert "lark.apaas.spring_billing" in out
    assert "{{PROJECT_NAME}}" not in out
    assert "{{PSM}}" not in out


def test_all_templates_have_valid_syntax():
    """Every *.template file should be parseable by render_template (no broken syntax).

    Also regression-guards the IMPORTANT-1 finding: single-brace literals
    like ``{auto · compile-wiki}`` in EVERGREEN.md.template must survive
    rendering verbatim (must not be treated as placeholders).

    NOTE: this is a SYNTAX-only check. Vars are fabricated from whatever names
    the template uses, so a typo like ``{{USER_NAM}}`` would silently pass.
    Cross-checking template var names against the bootstrap prompts list is
    M3-T06's job, not this test's.
    """
    template_files = list(REPO_ROOT.glob("templates/**/*.template"))
    assert len(template_files) >= 6, (
        f"expected at least 6 .template files, found {len(template_files)}"
    )
    for tf in template_files:
        text = tf.read_text(encoding="utf-8")
        names = set(re.findall(r"\{\{(\w+)\}\}", text))
        vars_ = {n: f"<{n}>" for n in names}
        out = render_template(tf, vars_)
        # No strict {{NAME}} placeholder (no whitespace) may survive.
        # Note: literals like ``{{ }}`` (whitespace inside braces) ARE preserved
        # verbatim by the renderer per its docstring, so this regex deliberately
        # only matches the strict form the renderer treats as placeholders.
        leftover = re.findall(r"\{\{\w+\}\}", out)
        assert not leftover, f"unrendered placeholder in {tf}: {leftover}"
        # Single-brace literals survive (regression for {auto · compile-wiki})
        if tf.name == "EVERGREEN.md.template" and "meta" in tf.parts:
            assert "{auto · compile-wiki}" in out


def test_persona_templates_do_not_leak_approval_token():
    """Persona templates must not contain the literal [APPROVAL_REQUIRED] /
    [/APPROVAL_REQUIRED] tokens.

    Regression for the 2026-05-05 bug: when these tokens appear in
    instructional text, claude quotes them back during self-introduction or
    capability descriptions, and the (now-strict) parser may still trip on
    edge cases. Combined with the parser fix (US-001), making persona files
    token-free is defense-in-depth.

    These four files are the persona / instruction surface — they must
    describe the approval mechanism abstractly (e.g. "审批闭环",
    "审批块"), without writing the literal opening or closing tag.
    """
    persona_files = [
        META / "CLAUDE.md.template",
        META / "AGENTS.md.template",
        META / "EVERGREEN.md.template",
        PROJECT / "CLAUDE.md.template",
    ]
    leaks: list[str] = []
    for tf in persona_files:
        text = tf.read_text(encoding="utf-8")
        if "[APPROVAL_REQUIRED]" in text:
            leaks.append(f"{tf.relative_to(REPO_ROOT)}: contains '[APPROVAL_REQUIRED]'")
        if "[/APPROVAL_REQUIRED]" in text:
            leaks.append(f"{tf.relative_to(REPO_ROOT)}: contains '[/APPROVAL_REQUIRED]'")
    assert not leaks, (
        "Persona templates must not contain literal APPROVAL tokens "
        "(claude self-quotes them and triggers parse_approval_block "
        "false-positives). Found: " + "; ".join(leaks)
    )
