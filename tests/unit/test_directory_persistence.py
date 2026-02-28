"""Tests for multi-root directory support and directory persistence."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


@pytest.fixture
def multi_root_tmpdir():
    """Create temporary directory structure with multiple roots."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        # Create two workspace roots
        root1 = base / "workspace1"
        root2 = base / "workspace2"
        root1.mkdir()
        root2.mkdir()

        # Create subdirectories in each root
        (root1 / "project_a").mkdir()
        (root1 / "project_b").mkdir()
        (root1 / "project_a" / ".git").mkdir()  # project_a is a git repo

        (root2 / "project_c").mkdir()
        (root2 / "project_d").mkdir()
        (root2 / "project_c" / ".git").mkdir()  # project_c is a git repo

        yield {
            "base": base,
            "root1": root1,
            "root2": root2,
        }


@pytest.fixture
def single_root_tmpdir():
    """Create temporary directory structure with single root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        # Create subdirectories
        (base / "project_a").mkdir()
        (base / "project_b").mkdir()
        (base / "project_a" / ".git").mkdir()

        yield base


@pytest.fixture
async def mock_storage():
    """Mock storage with directory persistence methods."""
    storage = AsyncMock()
    storage.load_user_directory = AsyncMock(return_value=None)
    storage.save_user_directory = AsyncMock()
    return storage


@pytest.fixture
def mock_deps(mock_storage):
    """Mock dependencies for orchestrator."""
    claude_integration = MagicMock()
    claude_integration._find_resumable_session_id = MagicMock(return_value=None)
    claude_integration.run_command = AsyncMock(return_value=MagicMock(
        response="Test response",
        session_id="test-session-123",
        error=None,
    ))

    audit_logger = MagicMock()
    audit_logger.log_command = AsyncMock()

    return {
        "claude_integration": claude_integration,
        "storage": mock_storage,
        "security_validator": MagicMock(),
        "audit_logger": audit_logger,
    }


@pytest.fixture
def mock_update_and_context():
    """Mock Telegram update and context objects."""
    update = MagicMock()
    update.effective_user.id = 12345
    update.message.text = "/repo"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}

    return update, context


class TestMultiRootDirectorySupport:
    """Test multi-root directory support in /repo command."""

    @pytest.mark.asyncio
    async def test_single_root_backward_compatibility(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Single APPROVED_DIRECTORY should work as before."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
            agentic_mode=True,
        )

        update, context = mock_update_and_context
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.agentic_repo(update, context)

        # Should list subdirectories
        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args
        message_text = call_args[0][0]

        assert "project_a" in message_text
        assert "project_b" in message_text
        assert "\U0001f4e6" in message_text  # Git repo icon for project_a
        assert "\U0001f4c1" in message_text  # Folder icon for project_b

    @pytest.mark.asyncio
    async def test_multi_root_lists_all_workspaces(
        self, multi_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Multiple approved directories should all be listed."""
        import os

        # Set environment variable for multi-root config
        old_env = os.environ.get("APPROVED_DIRECTORIES")
        os.environ["APPROVED_DIRECTORIES"] = f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}"

        try:
            settings = create_test_config(
                approved_directory=str(multi_root_tmpdir["root1"]),
                approved_directories_str=f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}",
                agentic_mode=True,
            )

            update, context = mock_update_and_context
            context.bot_data = mock_deps

            orchestrator = MessageOrchestrator(settings, mock_deps)
            await orchestrator.agentic_repo(update, context)

            # New browser shows one root at a time (first root by default)
            update.message.reply_text.assert_called_once()
            call_args = update.message.reply_text.call_args
            message_text = call_args[0][0]

            # Should show browsing header
            assert "Browsing:" in message_text

            # Should show subdirectories from the first root
            assert "project_a" in message_text
            assert "project_b" in message_text

            # Should have inline keyboard with .. for multi-root navigation
            reply_markup = call_args[1].get("reply_markup")
            assert reply_markup is not None
        finally:
            if old_env is not None:
                os.environ["APPROVED_DIRECTORIES"] = old_env
            else:
                os.environ.pop("APPROVED_DIRECTORIES", None)

    @pytest.mark.asyncio
    async def test_switch_to_directory_in_second_root(
        self, multi_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Switching to a directory should search all roots."""
        import os

        old_env = os.environ.get("APPROVED_DIRECTORIES")
        os.environ["APPROVED_DIRECTORIES"] = f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}"

        try:
            settings = create_test_config(
                approved_directory=str(multi_root_tmpdir["root1"]),
                approved_directories_str=f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}",
                agentic_mode=True,
            )

            update, context = mock_update_and_context
            update.message.text = "/repo project_c"
            context.bot_data = mock_deps

            orchestrator = MessageOrchestrator(settings, mock_deps)
            await orchestrator.agentic_repo(update, context)

            # Should switch to project_c in root2
            assert context.user_data["current_directory"] == multi_root_tmpdir["root2"] / "project_c"

            # Should persist to database
            mock_deps["storage"].save_user_directory.assert_called_once_with(
                12345,
                str(multi_root_tmpdir["root2"] / "project_c")
            )
        finally:
            if old_env is not None:
                os.environ["APPROVED_DIRECTORIES"] = old_env
            else:
                os.environ.pop("APPROVED_DIRECTORIES", None)


class TestDirectoryPersistence:
    """Test directory persistence across sessions."""

    @pytest.mark.asyncio
    async def test_save_directory_on_switch(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Switching directory should persist to database."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
            agentic_mode=True,
        )

        update, context = mock_update_and_context
        update.message.text = "/repo project_a"
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.agentic_repo(update, context)

        # Should save to database
        mock_deps["storage"].save_user_directory.assert_called_once_with(
            12345,
            str(single_root_tmpdir / "project_a")
        )

    @pytest.mark.asyncio
    async def test_restore_directory_on_first_message(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """First message should restore persisted directory."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
            agentic_mode=True,
        )

        # Mock persisted directory
        persisted_path = str(single_root_tmpdir / "project_a")
        mock_deps["storage"].load_user_directory = AsyncMock(return_value=persisted_path)

        update, context = mock_update_and_context
        update.message.text = "help me with something"
        update.message.chat.send_action = AsyncMock()
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.agentic_text(update, context)

        # Should restore directory from database
        mock_deps["storage"].load_user_directory.assert_called_once_with(12345)
        assert context.user_data["current_directory"] == Path(persisted_path)

    @pytest.mark.asyncio
    async def test_directory_restoration_validates_approved_dirs(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Restored directory must be in approved directories."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
            agentic_mode=True,
        )

        # Mock persisted directory outside approved dirs
        mock_deps["storage"].load_user_directory = AsyncMock(return_value="/etc/passwd")

        update, context = mock_update_and_context
        update.message.text = "help me"
        update.message.chat.send_action = AsyncMock()
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.agentic_text(update, context)

        # Should NOT restore invalid directory
        assert context.user_data.get("current_directory") is None

    @pytest.mark.asyncio
    async def test_callback_handler_persists_directory(
        self, single_root_tmpdir, mock_deps
    ):
        """Callback handler should persist directory changes."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
            agentic_mode=True,
        )

        # Mock callback query
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.from_user.id = 12345
        update.callback_query.data = f"cd:{single_root_tmpdir / 'project_a'}"

        context = MagicMock()
        context.user_data = {}
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator._agentic_callback(update, context)

        # Should persist to database
        mock_deps["storage"].save_user_directory.assert_called_once_with(
            12345,
            str(single_root_tmpdir / "project_a")
        )


class TestBackwardCompatibility:
    """Test backward compatibility with single approved_directory."""

    @pytest.mark.asyncio
    async def test_settings_approved_directories_property_single(self, single_root_tmpdir):
        """approved_directories property returns list with single directory."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
            agentic_mode=True,
        )

        dirs = settings.approved_directories
        assert isinstance(dirs, list)
        assert len(dirs) == 1
        assert dirs[0] == single_root_tmpdir

    @pytest.mark.asyncio
    async def test_settings_approved_directories_property_multi(self, multi_root_tmpdir):
        """approved_directories property returns list with multiple directories."""
        import os

        old_env = os.environ.get("APPROVED_DIRECTORIES")
        os.environ["APPROVED_DIRECTORIES"] = f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}"

        try:
            settings = create_test_config(
                approved_directory=str(multi_root_tmpdir["root1"]),
                approved_directories_str=f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}",
                agentic_mode=True,
            )

            dirs = settings.approved_directories
            assert isinstance(dirs, list)
            assert len(dirs) == 2
            assert multi_root_tmpdir["root1"] in dirs
            assert multi_root_tmpdir["root2"] in dirs
        finally:
            if old_env is not None:
                os.environ["APPROVED_DIRECTORIES"] = old_env
            else:
                os.environ.pop("APPROVED_DIRECTORIES", None)
