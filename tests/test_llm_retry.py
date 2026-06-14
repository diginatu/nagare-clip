"""Tests for the shared LLM-retry helpers (pure, no network)."""

from __future__ import annotations

import pytest

from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts


class TestRetryAttempts:
    def test_default_is_three_total(self):
        assert retry_attempts({}) == 3  # default max_retries=2 -> 3 attempts

    def test_zero_retries_is_single_attempt(self):
        assert retry_attempts({"max_retries": 0}) == 1

    def test_explicit_count(self):
        assert retry_attempts({"max_retries": 4}) == 5

    def test_negative_clamped_to_single(self):
        assert retry_attempts({"max_retries": -3}) == 1


class TestCfgForAttempt:
    def test_attempt_zero_keeps_base_temperature(self):
        cfg = {"temperature": 0.15}
        assert cfg_for_attempt(cfg, 0)["temperature"] == pytest.approx(0.15)

    def test_nudges_temperature_up_per_attempt(self):
        cfg = {"temperature": 0.1, "retry_temp_step": 0.2, "retry_temp_cap": 0.8}
        assert cfg_for_attempt(cfg, 1)["temperature"] == pytest.approx(0.3)
        assert cfg_for_attempt(cfg, 2)["temperature"] == pytest.approx(0.5)

    def test_capped_at_retry_temp_cap(self):
        cfg = {"temperature": 0.1, "retry_temp_step": 0.5, "retry_temp_cap": 0.8}
        assert cfg_for_attempt(cfg, 5)["temperature"] == pytest.approx(0.8)

    def test_does_not_mutate_base_cfg(self):
        cfg = {"temperature": 0.1}
        cfg_for_attempt(cfg, 3)
        assert cfg["temperature"] == pytest.approx(0.1)

    def test_absent_temperature_not_fabricated_on_retry(self):
        # No configured temperature -> rides the provider default on every
        # attempt; the retry must not inject one.
        assert "temperature" not in cfg_for_attempt({}, 1)

    def test_null_temperature_not_nudged_and_no_crash(self):
        # temperature: null (the escape hatch for temp-restricted models) must
        # survive a retry without a float(None) crash and without being nudged.
        out = cfg_for_attempt({"temperature": None}, 2)
        assert out["temperature"] is None
