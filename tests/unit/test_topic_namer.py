# tests/unit/test_topic_namer.py
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.projects.topic_namer import generate_topic_name


@pytest.mark.asyncio
async def test_generate_topic_name_returns_haiku_response():
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Fix authentication flow")]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.projects.topic_namer.anthropic.AsyncAnthropic", return_value=mock_client):
        name = await generate_topic_name(
            messages=["fix the login bug", "Looking at auth.py..."],
            dir_name="my-app",
        )
    assert name == "Fix authentication flow"
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_generate_topic_name_truncates_long_names():
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="A" * 100)]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.projects.topic_namer.anthropic.AsyncAnthropic", return_value=mock_client):
        name = await generate_topic_name(messages=["test"], dir_name="app")
    assert len(name) <= 50


@pytest.mark.asyncio
async def test_generate_topic_name_fallback_on_error():
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

    with patch("src.projects.topic_namer.anthropic.AsyncAnthropic", return_value=mock_client):
        name = await generate_topic_name(messages=["test"], dir_name="my-app")
    assert name is None


@pytest.mark.asyncio
async def test_generate_topic_name_strips_quotes():
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='"Fix login flow"')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.projects.topic_namer.anthropic.AsyncAnthropic", return_value=mock_client):
        name = await generate_topic_name(messages=["fix login"], dir_name="app")
    assert name == "Fix login flow"
