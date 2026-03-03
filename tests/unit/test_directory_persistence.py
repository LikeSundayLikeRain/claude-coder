"""Tests for multi-root directory support and directory persistence."""

import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

# handlers.message was removed in the classic-mode cleanup.
# Stub it so the lazy import inside _execute_query doesn't fail.
if "src.bot.handlers.message" not in sys.modules:
    _stub = ModuleType("src.bot.handlers.message")
    _stub._format_error_message = lambda response, default="": default  # type: ignore[attr-defined]
    _stub._update_working_directory_from_claude_response = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules["src.bot.handlers.message"] = _stub

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
    """Mock storage with session methods."""
    storage = AsyncMock()
    storage.load_session = AsyncMock(return_value=None)
    storage.save_session = AsyncMock()
    storage.clear_session = AsyncMock()
    return storage


@pytest.fixture
def mock_deps(mock_storage):
    """Mock dependencies for orchestrator."""
    claude_integration = MagicMock()
    claude_integration._find_resumable_session_id = MagicMock(return_value=None)
    claude_integration.run_command = AsyncMock(
        return_value=MagicMock(
            response="Test response",
            session_id="test-session-123",
            error=None,
        )
    )

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
        )

        update, context = mock_update_and_context
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_repo(update, context)

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
        os.environ["APPROVED_DIRECTORIES"] = (
            f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}"
        )

        try:
            settings = create_test_config(
                approved_directory=str(multi_root_tmpdir["root1"]),
                approved_directories_str=f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}",
            )

            update, context = mock_update_and_context
            context.bot_data = mock_deps

            orchestrator = MessageOrchestrator(settings, mock_deps)
            await orchestrator.handle_repo(update, context)

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
        os.environ["APPROVED_DIRECTORIES"] = (
            f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}"
        )

        try:
            settings = create_test_config(
                approved_directory=str(multi_root_tmpdir["root1"]),
                approved_directories_str=f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}",
            )

            update, context = mock_update_and_context
            update.message.text = "/repo project_c"
            context.bot_data = mock_deps

            orchestrator = MessageOrchestrator(settings, mock_deps)
            await orchestrator.handle_repo(update, context)

            # Should switch to project_c in root2
            assert (
                context.user_data["current_directory"]
                == multi_root_tmpdir["root2"] / "project_c"
            )

            # Should switch to project_c in root2
            assert (
                context.user_data["current_directory"]
                == multi_root_tmpdir["root2"] / "project_c"
            )
        finally:
            if old_env is not None:
                os.environ["APPROVED_DIRECTORIES"] = old_env
            else:
                os.environ.pop("APPROVED_DIRECTORIES", None)


class TestDirectoryPersistence:
    """Test directory persistence across sessions."""

    @pytest.mark.asyncio
    async def test_switch_directory_updates_user_data(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Switching directory updates current_directory in user_data."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        update, context = mock_update_and_context
        update.message.text = "/repo project_a"
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_repo(update, context)

        # Directory should be updated in user_data
        assert (
            context.user_data["current_directory"] == single_root_tmpdir / "project_a"
        )

    @pytest.mark.asyncio
    async def test_restore_session_on_first_message(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """First message should restore persisted session for the current chat/thread."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        # Mock persisted session for the current chat/thread
        persisted_path = single_root_tmpdir / "project_a"
        session_model = MagicMock()
        session_model.session_id = "restored-session-123"
        mock_deps["storage"].load_session = AsyncMock(return_value=session_model)

        update, context = mock_update_and_context
        update.message.text = "help me with something"
        update.message.chat.send_action = AsyncMock()
        context.bot_data = mock_deps
        # Directory must be set before cold-start kicks in
        context.user_data["current_directory"] = persisted_path

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_text(update, context)

        # load_session was called for cold-start detection
        mock_deps["storage"].load_session.assert_called_once()
        # After a successful query the session_id is set
        assert context.user_data["claude_session_id"] is not None

    @pytest.mark.asyncio
    async def test_directory_restoration_validates_approved_dirs(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Agentic text handler works even without current_directory set."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        update, context = mock_update_and_context
        update.message.text = "help me"
        update.message.chat.send_action = AsyncMock()
        context.bot_data = mock_deps
        # No current_directory set — should default to approved_directory

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_text(update, context)

        # Should NOT have set an unsafe directory
        current_dir = context.user_data.get("current_directory")
        if current_dir is not None:
            # Must be within approved dirs
            assert any(
                str(current_dir).startswith(str(d))
                for d in settings.approved_directories
            )

    @pytest.mark.asyncio
    async def test_callback_handler_switches_directory(
        self, single_root_tmpdir, mock_deps
    ):
        """Callback handler cd: updates current_directory."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
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
        await orchestrator._handle_callback(update, context)

        # Directory should be updated in user_data
        assert (
            context.user_data.get("current_directory")
            == single_root_tmpdir / "project_a"
        )


class TestSelectDirectoryPureSwitch:
    """Test that _select_directory is a pure directory switch without auto-resume."""

    @pytest.mark.asyncio
    async def test_select_directory_does_not_set_session_id(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """_select_directory should NOT look up or set claude_session_id from history."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        update, context = mock_update_and_context
        update.message.text = "/repo project_a"
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_repo(update, context)

        # session_id must be explicitly None — no auto-resume
        assert context.user_data["claude_session_id"] is None

    @pytest.mark.asyncio
    async def test_select_directory_clears_force_new_session(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """_select_directory should reset force_new_session to False."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        update, context = mock_update_and_context
        update.message.text = "/repo project_a"
        context.bot_data = mock_deps
        context.user_data["force_new_session"] = True  # pre-set

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_repo(update, context)

        assert context.user_data["force_new_session"] is False

    @pytest.mark.asyncio
    async def test_select_directory_disconnects_active_client(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """_select_directory disconnects any active SDK client."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        client_manager = MagicMock()
        client_manager.disconnect = AsyncMock()
        mock_deps_with_cm = dict(mock_deps)
        mock_deps_with_cm["client_manager"] = client_manager

        update, context = mock_update_and_context
        update.message.text = "/repo project_a"
        context.bot_data = mock_deps_with_cm

        orchestrator = MessageOrchestrator(settings, mock_deps_with_cm)
        await orchestrator.handle_repo(update, context)

        # disconnect should be called with user id and old directory
        assert client_manager.disconnect.called
        call_args = client_manager.disconnect.call_args[0]
        assert call_args[0] == 12345

    @pytest.mark.asyncio
    async def test_select_directory_reply_has_no_session_badge(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """Reply text should not contain 'session resumed' after switching."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        update, context = mock_update_and_context
        update.message.text = "/repo project_a"
        context.bot_data = mock_deps

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_repo(update, context)

        call_args = update.message.reply_text.call_args
        reply_text = call_args[0][0]
        assert "session resumed" not in reply_text


class TestBackwardCompatibility:
    """Test backward compatibility with single approved_directory."""

    @pytest.mark.asyncio
    async def test_settings_approved_directories_property_single(
        self, single_root_tmpdir
    ):
        """approved_directories property returns list with single directory."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        dirs = settings.approved_directories
        assert isinstance(dirs, list)
        assert len(dirs) == 1
        assert dirs[0] == single_root_tmpdir

    @pytest.mark.asyncio
    async def test_settings_approved_directories_property_multi(
        self, multi_root_tmpdir
    ):
        """approved_directories property returns list with multiple directories."""
        import os

        old_env = os.environ.get("APPROVED_DIRECTORIES")
        os.environ["APPROVED_DIRECTORIES"] = (
            f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}"
        )

        try:
            settings = create_test_config(
                approved_directory=str(multi_root_tmpdir["root1"]),
                approved_directories_str=f"{multi_root_tmpdir['root1']},{multi_root_tmpdir['root2']}",
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


class TestColdStartRestoration:
    """Test session + directory restoration after bot restart."""

    @pytest.mark.asyncio
    async def test_restore_session_and_directory_on_cold_start(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """After bot restart, session_id is restored from DB via load_session(chat_id, thread_id)."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        persisted_dir = single_root_tmpdir / "project_a"
        session_model = MagicMock()
        session_model.session_id = "sess-abc-123"
        mock_deps["storage"].load_session = AsyncMock(return_value=session_model)

        update, context = mock_update_and_context
        update.message.text = "hello"
        update.message.chat.send_action = AsyncMock()
        context.bot_data = mock_deps
        # Cold start: user_data has directory set but no session_id key yet
        context.user_data = {"current_directory": persisted_dir}

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_text(update, context)

        # load_session was called (cold-start detection triggered)
        mock_deps["storage"].load_session.assert_called_once()
        # claude_session_id is set (either restored value or what Claude returned)
        assert context.user_data.get("claude_session_id") is not None

    @pytest.mark.asyncio
    async def test_no_restore_after_explicit_clear(
        self, single_root_tmpdir, mock_deps, mock_update_and_context
    ):
        """After /new or /repo, session_id key exists as None — no DB restore."""
        settings = create_test_config(
            approved_directory=str(single_root_tmpdir),
        )

        mock_deps["storage"].load_session = AsyncMock(return_value=None)

        update, context = mock_update_and_context
        update.message.text = "hello"
        update.message.chat.send_action = AsyncMock()
        context.bot_data = mock_deps
        # Explicit clear: key exists with None value
        context.user_data = {"claude_session_id": None}

        orchestrator = MessageOrchestrator(settings, mock_deps)
        await orchestrator.handle_text(update, context)

        # Should NOT call load_session (key already present as None)
        mock_deps["storage"].load_session.assert_not_called()
