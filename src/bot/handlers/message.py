"""Helper functions for message handling (used by orchestrator)."""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from ...claude.exceptions import (
    ClaudeError,
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from ..utils.html_format import escape_html

logger = structlog.get_logger()


def _format_process_error(error_str: str) -> str:
    """Format a Claude process/SDK error with the actual details."""
    safe_error = escape_html(error_str)
    if len(safe_error) > 500:
        safe_error = safe_error[:500] + "..."

    return (
        f"❌ <b>Claude Process Error</b>\n\n"
        f"{safe_error}\n\n"
        "<b>What you can do:</b>\n"
        "• Try your request again\n"
        "• Use /new to start a fresh session if the problem persists\n"
        "• Check /status for current session state"
    )


def _format_error_message(error: Exception | str) -> str:
    """Format error messages for user-friendly display.

    Accepts an exception object (preferred) or a string for backward
    compatibility.  When an exception is provided, the error type is used
    to produce a specific, actionable message.
    """
    if isinstance(error, str):
        error_str = error
        error_obj: Exception | None = None
    else:
        error_str = str(error)
        error_obj = error

    if isinstance(error_obj, ClaudeTimeoutError):
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if isinstance(error_obj, ClaudeMCPError):
        server_hint = ""
        if error_obj.server_name:
            server_hint = f" (<code>{escape_html(error_obj.server_name)}</code>)"
        return (
            f"🔌 <b>MCP Server Error</b>{server_hint}\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running and reachable\n"
            "• Verify <code>MCP_CONFIG_PATH</code> points to a valid config\n"
            "• Ask the administrator to check MCP server logs"
        )

    if isinstance(error_obj, ClaudeParsingError):
        return (
            "📄 <b>Response Parsing Error</b>\n\n"
            f"Claude returned a response that could not be parsed:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Try your request again\n"
            "• Rephrase your prompt if the problem persists"
        )

    if isinstance(error_obj, ClaudeSessionError):
        return (
            "🔄 <b>Session Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if isinstance(error_obj, ClaudeProcessError):
        return _format_process_error(error_str)

    if isinstance(error_obj, ClaudeError):
        safe_error = escape_html(error_str)
        if len(safe_error) > 500:
            safe_error = safe_error[:500] + "..."
        return (
            f"❌ <b>Claude Error</b>\n\n"
            f"{safe_error}\n\n"
            f"Try again or use /new to start a fresh session."
        )

    # Fall back to keyword matching for string-only callers
    error_lower = error_str.lower()

    if "usage limit reached" in error_lower or "usage limit" in error_lower:
        return error_str

    if "tool not allowed" in error_lower:
        return error_str

    if "no conversation found" in error_lower:
        return (
            "🔄 <b>Session Not Found</b>\n\n"
            "The previous Claude session could not be found or has expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if "rate limit" in error_lower:
        return (
            "⏱️ <b>Rate Limit Reached</b>\n\n"
            "Too many requests in a short time period.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment before trying again\n"
            "• Use simpler requests\n"
            "• Check your current usage with /status"
        )

    if "timed out after" in error_lower or "claude sdk timed out" in error_lower:
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if "overloaded" in error_lower:
        return (
            "🏗️ <b>Claude is Overloaded</b>\n\n"
            "The service is experiencing high demand.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a few minutes and try again\n"
            "• Try a simpler request"
        )

    return _format_process_error(error_str)


def _update_working_directory_from_claude_response(
    claude_response, context, settings, user_id
):
    """Update the working directory based on Claude's response content."""
    patterns = [
        r"(?:^|\n).*?cd\s+([^\s\n]+)",
        r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",
        r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",
        r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",
    ]

    content = claude_response.content.lower()
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                new_path = match.strip().strip("\"'`")

                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    new_path = (current_dir / new_path).resolve()
                else:
                    new_path = Path(new_path).resolve()

                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.exists()
                ):
                    context.user_data["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Claude response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return

            except (ValueError, OSError) as e:
                logger.debug(
                    "Invalid path in Claude response", path=match, error=str(e)
                )
                continue
