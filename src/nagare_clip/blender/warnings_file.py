"""Blender's own warnings, written where the finished-cut report can read them.

The clamp and overlap notices in ``blender.timeline`` are the only sign that a
requested interval did not fit, or that Blender built a strip a different
length than was predicted -- and they go to the same stream as Blender's
unrelated ``bl_pkg``/``cattrs`` extension tracebacks that the operator prompt
tells the operator to ignore, so in practice they scroll past unread.

No bpy import, so the host-side report can import the filename contract too.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

WARNINGS_FILENAME = "blender_warnings.json"


class _Collector(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.append(record.getMessage())
        except Exception:  # noqa: BLE001 - a broken format string must not abort the build
            self._sink.append(str(record.msg))


@contextmanager
def capture_warnings() -> Iterator[list[str]]:
    """Collect every WARNING+ message logged inside the block.

    Attaches to the root logger so it catches the module-level
    ``logging.warning(...)`` calls the Blender stage makes throughout.
    """
    collected: list[str] = []
    handler = _Collector(collected)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield collected
    finally:
        root.removeHandler(handler)


def write_warnings(directory: Path, warnings: list[str]) -> None:
    """Write the warnings contract beside the .blend (always, even when empty).

    An empty list is still written: a stale file from a previous run would
    otherwise report warnings the current scene never produced.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / WARNINGS_FILENAME).write_text(
            json.dumps({"warnings": warnings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        logging.warning("blender: could not write %s: %s", WARNINGS_FILENAME, e)
