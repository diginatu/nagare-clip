"""The video-editing skills package (``skills/``): one APM package, installed
into a VIDEO directory, never deployed into this repository.

``make apm`` smoke-installs it into a temp directory and runs ``apm audit
--ci`` there (it needs the apm CLI, which CI lacks); this file checks what CI
can: the package shape, each skill's frontmatter, that every tool command a
skill names exists, and that nothing is deployed at the repo root (so working
on nagare-clip itself never triggers a video-editing skill).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills"
SKILLS = sorted(p.parent for p in (PACKAGE / ".apm" / "skills").glob("*/SKILL.md"))


def test_package_manifest():
    manifest = yaml.safe_load((PACKAGE / "apm.yml").read_text(encoding="utf-8"))
    assert manifest["name"] and manifest["version"]
    assert manifest["includes"] == [".apm/"]
    assert [p.name for p in SKILLS] == ["nagare-director"]


def test_nothing_is_deployed_at_the_repo_root():
    for path in ("apm.yml", "apm.lock.yaml", ".apm", ".claude/skills", ".agents/skills"):
        assert not (ROOT / path).exists(), f"{path}: the skills install into a video directory"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_frontmatter_is_valid(skill):
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md needs YAML frontmatter"
    front = match.group(1)
    assert front.isascii(), "APM requires ASCII-only frontmatter values"
    assert re.search(rf"^name: {re.escape(skill.name)}$", front, re.M), "name must match the dir"
    description = re.search(r"^description: (.+)$", front, re.M)
    assert description and len(description.group(1)) <= 1024


def test_skill_names_only_real_tool_commands():
    """Every `director_tools.sh <cmd>` the skill tells an agent to run exists."""
    from nagare_clip.director.tools.cli import build_parser

    commands = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    skill = PACKAGE / ".apm" / "skills" / "nagare-director"
    text = "\n".join(p.read_text(encoding="utf-8") for p in skill.rglob("*.md"))
    used = set(re.findall(r"director_tools\.sh\"? (\w+)", text))
    assert used and used <= commands
    assert (ROOT / "scripts" / "director_tools.sh").is_file()


def test_skill_md_edit_example_passes_write_validation():
    """The edit-format example is what agents copy: write must accept it."""
    import json

    from nagare_clip.director.display import build_display_view
    from nagare_clip.director.run import SegmentTranscript
    from nagare_clip.director.tools.validate import validate_edit
    from nagare_clip.order import Segment

    text = (PACKAGE / ".apm" / "skills" / "nagare-director" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    block = re.search(r"## Edit format\s+```json\n(.*?)\n```", text, re.S)
    assert block, "SKILL.md must keep a ```json example under '## Edit format'"
    example = json.loads(block.group(1))

    lines = 212
    transcript = SegmentTranscript(
        edit_lines=[f"えーと line {i}" for i in range(1, lines + 1)],
        first_line=1,
        seg_times=[(i * 2.0, i * 2.0 + 1.5) for i in range(lines)],
        silences=None,
        gaps=[],
    )
    view = build_display_view([(Segment("video", None), transcript)])
    assert len(view.lines) == lines
    _edit, problems = validate_edit(view, example, has_plan=False, max_keep_lines=8)
    assert [str(p) for p in problems] == []
