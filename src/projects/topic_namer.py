"""Haiku-powered topic name generation from conversation context."""

from __future__ import annotations

from typing import Optional

import anthropic
import structlog

logger = structlog.get_logger()

HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_NAME_LENGTH = 50


async def generate_topic_name(
    messages: list[str],
    dir_name: str,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Generate a concise topic name from conversation snippets.

    Args:
        messages: Recent conversation messages.
        dir_name: Directory name for context.
        api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.

    Returns None on failure (caller should keep existing name).
    """
    if not messages:
        return None

    snippet = "\n".join(msg[:200] for msg in messages[:6])
    prompt = (
        f"Generate a concise topic title (3-6 words, no quotes) for this "
        f"coding session in {dir_name}/:\n\n{snippet}"
    )

    try:
        client = anthropic.AsyncAnthropic(
            api_key=api_key,  # falls back to ANTHROPIC_API_KEY env var if None
        )
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=30,
            messages=[{"role": "user", "content": prompt}],
        )
        name = response.content[0].text.strip().strip('"').strip("'")
        return name[:MAX_NAME_LENGTH] if name else None
    except anthropic.AuthenticationError:
        logger.warning(
            "topic_name_generation_skipped",
            reason="No Anthropic API key configured",
        )
        return None
    except Exception as e:
        logger.warning("topic_name_generation_failed", error=str(e))
        return None
