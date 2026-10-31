"""Deterministic director tools for the agent skill (no LLM call, ever).

A general coding agent (Claude Code, Copilot, Codex, ...) plays the director
interactively with the user through the ``nagare-director`` skill
(``.apm/skills/nagare-director/``) and these tools:

* ``view``    — the whole video under one display numbering (read-only);
* ``dump``    — the director files on disk, as an edit file in display numbers;
* ``preview`` — what the current or a candidate edit will play (read-only);
* ``write``   — validate an edit and write ``{stem}_director.json``,
  ``director/order.json`` and ``director/plan.md`` — all or nothing.

The validation is the LLM director's own (:mod:`~nagare_clip.director.loop`,
:mod:`~nagare_clip.director.director_llm`), so the two cannot drift.
"""
