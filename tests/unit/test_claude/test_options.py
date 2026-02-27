"""Tests for OptionsBuilder."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.claude.options import OptionsBuilder


class TestOptionsBuilderMinimal:
    def test_build_returns_options_with_cwd(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert opts.cwd == str(tmp_path)

    def test_build_sets_bypass_permissions(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert opts.permission_mode == "bypassPermissions"

    def test_build_without_session_id_leaves_resume_none(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert opts.resume is None


class TestOptionsBuilderSessionId:
    def test_build_with_session_id_sets_resume(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path), session_id="sess-abc-123")
        assert opts.resume == "sess-abc-123"


class TestOptionsBuilderModel:
    def test_build_with_model_override_sets_model(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path), model="claude-opus-4-5")
        assert opts.model == "claude-opus-4-5"

    def test_build_reads_model_from_cli_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"model": "claude-sonnet-4-5"}))
        builder = OptionsBuilder(claude_dir=tmp_path)
        opts = builder.build(cwd=str(tmp_path))
        assert opts.model == "claude-sonnet-4-5"

    def test_model_override_beats_cli_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"model": "claude-sonnet-4-5"}))
        builder = OptionsBuilder(claude_dir=tmp_path)
        opts = builder.build(cwd=str(tmp_path), model="claude-opus-4-5")
        assert opts.model == "claude-opus-4-5"

    def test_build_without_model_and_no_settings_leaves_model_none(
        self, tmp_path: Path
    ) -> None:
        builder = OptionsBuilder(claude_dir=tmp_path)  # no settings.json
        opts = builder.build(cwd=str(tmp_path))
        assert opts.model is None


class TestOptionsBuilderBetas:
    def test_build_with_betas_sets_betas(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path), betas=["context-1m-2025-08-07"])
        assert opts.betas == ["context-1m-2025-08-07"]


class TestOptionsBuilderSystemPrompt:
    def test_system_prompt_uses_preset_format(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert isinstance(opts.system_prompt, dict)
        assert opts.system_prompt.get("type") == "preset"


class TestOptionsBuilderCanUseTool:
    def test_can_use_tool_set_when_validator_and_approved_dir_provided(
        self, tmp_path: Path
    ) -> None:
        validator = MagicMock()
        builder = OptionsBuilder(security_validator=validator)
        opts = builder.build(
            cwd=str(tmp_path), approved_directory=str(tmp_path)
        )
        assert opts.can_use_tool is not None
        assert callable(opts.can_use_tool)

    def test_can_use_tool_none_when_no_validator(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert opts.can_use_tool is None

    def test_can_use_tool_none_when_no_approved_directory(
        self, tmp_path: Path
    ) -> None:
        validator = MagicMock()
        builder = OptionsBuilder(security_validator=validator)
        opts = builder.build(cwd=str(tmp_path))
        assert opts.can_use_tool is None


class TestOptionsBuilderEnvAndStderr:
    def test_env_clears_claudecode_variable(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert opts.env.get("CLAUDECODE") == ""

    def test_stderr_callback_is_set(self, tmp_path: Path) -> None:
        builder = OptionsBuilder()
        opts = builder.build(cwd=str(tmp_path))
        assert opts.stderr is not None
        assert callable(opts.stderr)
