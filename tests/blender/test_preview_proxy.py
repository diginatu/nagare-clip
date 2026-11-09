"""The VSE preview must ask for the proxy size the pipeline builds.

The Video_Editing template's preview reads PROXY_100; with `proxy_size: 50`
it would look for proxy_100.avi, find none and decode the original.
Requires Blender on PATH; skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "blender_preview_proxy.py"

pytestmark = pytest.mark.skipif(shutil.which("blender") is None, reason="blender not on PATH")


@pytest.mark.parametrize("size", [50, 25])
def test_every_sequencer_space_previews_at_proxy_size(size):
    out = subprocess.run(
        ["blender", "-b", "--factory-startup", "--python-exit-code", "1",
         "--python", str(SCRIPT), "--", str(size)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    line = next(ln for ln in reversed(out.splitlines()) if ln.startswith("RESULT "))
    sizes = json.loads(line.removeprefix("RESULT "))
    assert sizes, "no SEQUENCE_EDITOR space in the template"
    assert set(sizes) == {f"PROXY_{size}"}
