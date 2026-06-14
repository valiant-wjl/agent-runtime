"""PromptHub MVP for digital-agent: enforce that every TRACKED_PROMPTS template
has its current sha256 logged in prompts/REGISTRY.md. Mirrors the fixer-side
test (issue-driven-fixer/tests/test_prompt_registry.py); same schema, same
error message format.

See prompts/REGISTRY.md for why this exists and how to update it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REGISTRY = REPO_ROOT / "prompts" / "REGISTRY.md"

TRACKED_PROMPTS = [
    "templates/meta/SOUL.md.template",
    "templates/meta/USER.md.template",
    "templates/meta/EVERGREEN.md.template",
    "templates/meta/CLAUDE.md.template",
    "templates/meta/AGENTS.md.template",
    "templates/meta/MEMORY.md.template",
]

_SHA_LINE_RE = re.compile(
    r"^- \*\*Last logged sha256\*\*: `(?P<sha>[0-9a-f]{64})`\s*$",
    flags=re.MULTILINE,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry_text() -> str:
    if not REGISTRY.exists():
        pytest.fail(f"REGISTRY.md missing at {REGISTRY}.")
    return REGISTRY.read_text()


def _registry_section_for(prompt_relpath: str, text: str) -> str:
    marker = f"## {prompt_relpath}"
    idx = text.find(marker)
    if idx == -1:
        pytest.fail(
            f"REGISTRY.md has no '## {prompt_relpath}' section. "
            f"Add one matching the schema in prompts/REGISTRY.md."
        )
    after = text[idx + len(marker):]
    end_h2 = after.find("\n## ")
    end_hr = after.find("\n---")
    end_candidates = [e for e in (end_h2, end_hr) if e != -1]
    end = min(end_candidates) if end_candidates else len(after)
    return after[:end]


@pytest.mark.parametrize("prompt_relpath", TRACKED_PROMPTS)
def test_prompt_sha256_matches_registry(prompt_relpath: str) -> None:
    prompt_path = REPO_ROOT / prompt_relpath
    assert prompt_path.exists(), f"tracked prompt missing on disk: {prompt_path}"

    actual_sha = _sha256(prompt_path)
    section = _registry_section_for(prompt_relpath, _registry_text())

    m = _SHA_LINE_RE.search(section)
    assert m, (
        f"REGISTRY.md section for '{prompt_relpath}' has no parseable "
        f"'Last logged sha256' line."
    )
    logged_sha = m.group("sha")
    assert actual_sha == logged_sha, (
        f"\n\nTEMPLATE CHANGED WITHOUT REGISTRY UPDATE\n"
        f"  template:       {prompt_relpath}\n"
        f"  current sha256: {actual_sha}\n"
        f"  logged sha256:  {logged_sha}\n\n"
        f"Fix: edit prompts/REGISTRY.md\n"
        f"  - bump 'Last logged sha256' to {actual_sha}\n"
        f"  - bump 'Last logged commit' (use the SHA you're about to create)\n"
        f"  - add a row to 'Change history' table describing the edit\n"
        f"Commit template + REGISTRY together so this test passes again."
    )
