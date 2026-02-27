"""High-level Claude Code integration facade.

Thin client: no local session state. Uses ~/.claude/history.jsonl as
the shared session index and passes session_id directly to the SDK.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .history import filter_by_directory, read_claude_history
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate

logger = structlog.get_logger()


class ClaudeIntegration:
    """Main integration point for Claude Code.

    Stateless: all session state lives in Claude CLI's history.jsonl.
    """

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
    ) -> ClaudeResponse:
        """Run Claude Code command with session management.

        Args:
            prompt: The user's message.
            working_directory: Project directory.
            user_id: Telegram user ID (for logging only).
            session_id: Session to resume. If None and not force_new,
                        auto-resumes from history.jsonl.
            on_stream: Streaming callback.
            force_new: If True, always start a fresh session.
        """
        # Determine whether to resume
        should_resume = False

        if force_new:
            session_id = None
        elif not session_id:
            # Auto-resume: pick most recent session for this directory
            session_id = self._find_resumable_session_id(working_directory)

        should_resume = bool(session_id)

        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            should_resume=should_resume,
            force_new=force_new,
        )

        try:
            response = await self.sdk_manager.execute_command(
                prompt=prompt,
                working_directory=working_directory,
                session_id=session_id,
                continue_session=should_resume,
                stream_callback=on_stream,
            )
        except Exception as resume_error:
            if should_resume:
                logger.warning(
                    "Session resume failed, starting fresh",
                    failed_session_id=session_id,
                    error=str(resume_error),
                )
                response = await self.sdk_manager.execute_command(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=None,
                    continue_session=False,
                    stream_callback=on_stream,
                )
            else:
                raise

        logger.info(
            "Claude command completed",
            session_id=response.session_id,
            cost=response.cost,
            duration_ms=response.duration_ms,
            num_turns=response.num_turns,
            is_error=response.is_error,
        )

        return response

    def _find_resumable_session_id(
        self, working_directory: Path
    ) -> Optional[str]:
        """Find the most recent session for a directory from history.jsonl.

        Returns session_id or None.
        """
        try:
            entries = read_claude_history()
            filtered = filter_by_directory(entries, working_directory)

            if not filtered:
                return None

            # Entries are already sorted newest-first by read_claude_history
            return filtered[0].session_id
        except Exception as e:
            logger.warning(
                "Failed to read session history for auto-resume",
                error=str(e),
            )
            return None

    async def shutdown(self) -> None:
        """Shutdown integration."""
        logger.info("Claude integration shutdown complete")
