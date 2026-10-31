"""The nagare-director skill: source under .apm/, deployed copies committed.

CI has no ``apm`` binary, so this guards what ``apm audit --ci`` checks
locally: every deployed copy (``.claude/skills/`` for Claude,
``.agents/skills/`` for Copilot/Codex/Cursor/Gemini) is byte-identical to its
source.  After editing the skill, run ``apm install`` (or ``make apm``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".apm" / "skills" / "nagare-director"
DEPLOYED = [
    ROOT / ".claude" / "skills" / "nagare-director",
    ROOT / ".agents" / "skills" / "nagare-director",
]


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.mark.parametrize("deployed", DEPLOYED, ids=lambda p: str(p.relative_to(ROOT)))
def test_deployed_copy_matches_source(deployed):
    assert _files(deployed) == _files(SOURCE), "run `apm install` to redeploy the skill"


def test_frontmatter_is_valid():
    text = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md needs YAML frontmatter"
    front = match.group(1)
    assert front.isascii(), "APM requires ASCII-only frontmatter values"
    assert re.search(r"^name: nagare-director$", front, re.M), "name must match the directory"
    description = re.search(r"^description: (.+)$", front, re.M)
    assert description and len(description.group(1)) <= 1024


def test_skill_names_only_real_tool_commands():
    """Every `director_tools.sh <cmd>` the skill tells an agent to run exists."""
    from nagare_clip.director.tools.cli import build_parser

    commands = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    text = "\n".join(p.read_text(encoding="utf-8") for p in SOURCE.rglob("*.md"))
    used = set(re.findall(r"director_tools\.sh (\w+)", text))
    assert used and used <= commands
    assert (ROOT / "scripts" / "director_tools.sh").is_file()
