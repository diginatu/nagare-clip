"""`blender.prefetch` must reach the saved .blend's SequenceEditor.use_prefetch.

Prefetch draws text strips in a background thread while the UI draws them
too; FreeType is not thread-safe and Blender crashes in
FTC_CMapCache_Lookup, so a user turns it off here. Requires Blender on PATH;
skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nagare_clip.config import DEFAULTS

SCRIPT = Path(__file__).parent / "blender_prefetch.py"

pytestmark = pytest.mark.skipif(shutil.which("blender") is None, reason="blender not on PATH")


@pytest.mark.parametrize("enabled", [False, True])
def test_prefetch_is_saved_in_the_blend(enabled):
    out = subprocess.run(
        ["blender", "-b", "--factory-startup", "--python-exit-code", "1",
         "--python", str(SCRIPT), "--", "1" if enabled else "0"],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    line = next(ln for ln in reversed(out.splitlines()) if ln.startswith("RESULT "))
    assert json.loads(line.removeprefix("RESULT ")) is enabled


def test_prefetch_default_keeps_blender_behaviour():
    assert DEFAULTS["blender"]["prefetch"] is True
