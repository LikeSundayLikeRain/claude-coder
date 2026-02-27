"""Builds ClaudeAgentOptions from CLI settings with full feature parity."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import structlog
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import SdkBeta

from src.claude.monitor import _make_can_use_tool_callback

logger = structlog.get_logger()

DEFAULT_CLAUDE_DIR = Path.home() / ".claude"


class OptionsBuilder:
    """Constructs ClaudeAgentOptions reading config from CLI settings."""

    def __init__(
        self,
        claude_dir: Optional[Path] = None,
        security_validator: Any = None,
        cli_path: Optional[str] = None,
    ) -> None:
        self._claude_dir = claude_dir or DEFAULT_CLAUDE_DIR
        self._security_validator = security_validator
        self._cli_path = cli_path
        self._cli_settings: Optional[dict[str, Any]] = None

    def _read_cli_settings(self) -> dict[str, Any]:
        """Read and cache ~/.claude/settings.json."""
        if self._cli_settings is not None:
            return self._cli_settings

        settings_path = self._claude_dir / "settings.json"
        if settings_path.exists():
            try:
                self._cli_settings = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("failed_to_read_cli_settings", error=str(e))
                self._cli_settings = {}
        else:
            self._cli_settings = {}
        return self._cli_settings

    def build(
        self,
        cwd: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        betas: Optional[list[SdkBeta]] = None,
        approved_directory: Optional[str] = None,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with full CLI parity."""
        cli_settings = self._read_cli_settings()

        # Model: explicit override > CLI settings > None (SDK default)
        resolved_model = model or cli_settings.get("model") or None

        # System prompt: preserve CLAUDE.md loading with mobile append
        system_prompt: Any = {
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "You are being accessed via Telegram. "
                "Keep responses concise for mobile reading."
            ),
        }

        # Security callback
        can_use_tool = None
        if self._security_validator and approved_directory:
            can_use_tool = _make_can_use_tool_callback(
                self._security_validator,
                Path(cwd),
                Path(approved_directory),
            )

        def _log_stderr(line: str) -> None:
            logger.debug("claude_cli_stderr", line=line.rstrip())

        # Clear CLAUDECODE env var so the bundled CLI doesn't refuse to
        # start when the bot itself is launched from inside a Claude session.
        # The SDK merges os.environ with this dict, so we override to empty.
        clean_env: dict[str, str] = {"CLAUDECODE": ""}

        return ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
            model=resolved_model,
            resume=session_id or None,
            betas=betas or [],
            can_use_tool=can_use_tool,
            cli_path=self._cli_path or None,
            stderr=_log_stderr,
            env=clean_env,
            include_partial_messages=True,
            # Load full Claude Code toolset (includes Skill tool) and
            # user/project settings so the CLI discovers plugins & skills
            # natively — keyword-triggered skills work without our help.
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project"],
        )
