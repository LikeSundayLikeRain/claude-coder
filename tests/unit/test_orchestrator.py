"""Tests for the MessageOrchestrator."""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
from src.bot.progress import redact_secrets as _redact_secrets
from src.config import create_test_config


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def agentic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir))


@pytest.fixture
def classic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir))


@pytest.fixture
def group_thread_settings(tmp_dir):
    project_dir = tmp_dir / "project_a"
    project_dir.mkdir()
    config_file = tmp_dir / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_dir),
    )


@pytest.fixture
def private_thread_settings(tmp_dir):
    project_dir = tmp_dir / "project_a"
    project_dir.mkdir()
    config_file = tmp_dir / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_dir),
    )


@pytest.fixture
def deps():
    return {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "audit_logger": MagicMock(),
    }


def test_agentic_registers_commands(agentic_settings, deps):
    """Agentic mode registers start, new, interrupt, status, compact, model, repo, resume, commands, add, remove, history."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    # Collect all CommandHandler registrations
    from telegram.ext import CommandHandler

    cmd_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CommandHandler)
    ]
    commands = [h[0][0].commands for h in cmd_handlers]

    assert len(cmd_handlers) == 11
    assert frozenset({"start"}) in commands
    assert frozenset({"new"}) in commands
    assert frozenset({"interrupt"}) in commands
    assert frozenset({"status"}) in commands
    assert frozenset({"compact"}) in commands
    assert frozenset({"model"}) in commands
    assert frozenset({"repo"}) in commands
    assert frozenset({"resume"}) in commands
    assert frozenset({"commands"}) in commands
    assert frozenset({"remove"}) in commands
    assert frozenset({"history"}) in commands


def test_agentic_registers_remove(agentic_settings, deps):
    """Agentic mode registers /remove command (no /add — replaced by /start wizard)."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator._register_handlers(app)

    from telegram.ext import CommandHandler

    cmd_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CommandHandler)
    ]
    commands = [h[0][0].commands for h in cmd_handlers]

    assert frozenset({"add"}) not in commands
    assert frozenset({"remove"}) in commands


def test_agentic_registers_text_document_photo_handlers(agentic_settings, deps):
    """Agentic mode registers text, document, and photo message handlers."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler, MessageHandler

    msg_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], MessageHandler)
    ]
    cb_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    # 3 message handlers (text, unrecognized commands, combined photo+document)
    assert len(msg_handlers) == 3
    # 1 callback handler (for cd:, session:, skill:, model: patterns)
    assert len(cb_handlers) == 1


async def test_agentic_bot_commands(agentic_settings, deps):
    """Agentic mode returns a dict with private and group command sets."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    result = await orchestrator.get_bot_commands()

    assert isinstance(result, dict)
    assert "private" in result
    assert "group" in result

    private_names = [c.command for c in result["private"]]
    assert "start" in private_names
    assert "new" in private_names
    assert "interrupt" in private_names
    assert "status" in private_names
    assert "commands" in private_names

    group_names = [c.command for c in result["group"]]
    assert "start" in group_names
    assert "status" in group_names
    # Topic-specific commands (remove, history, etc.) are handled by
    # the bot but not shown in the group autocomplete menu
    assert "remove" not in group_names


async def test_agentic_start_no_keyboard(agentic_settings, deps):
    """Agentic /start sends brief message without inline keyboard."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "Alice"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": agentic_settings}
    for k, v in deps.items():
        context.bot_data[k] = v

    await orchestrator.handle_start(update, context)

    update.message.reply_text.assert_called_once()
    call_kwargs = update.message.reply_text.call_args
    # No reply_markup argument (no keyboard)
    assert (
        "reply_markup" not in call_kwargs.kwargs
        or call_kwargs.kwargs.get("reply_markup") is None
    )
    # Contains user name
    assert "Alice" in call_kwargs.args[0]


async def test_agentic_new_resets_session(agentic_settings, deps):
    """Agentic /new clears session and sends fallback confirmation when no client_manager."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"claude_session_id": "old-session-123"}
    context.bot_data = {}

    await orchestrator.handle_new(update, context)

    assert context.user_data["claude_session_id"] is None
    update.message.reply_text.assert_called_once_with(
        "Session reset. Will connect on your next message.",
    )


async def test_agentic_new_eagerly_connects_sdk(agentic_settings, deps, tmp_path):
    """/new should eagerly call get_or_connect to init SDK session."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    update = MagicMock()
    update.effective_user.id = 42
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"current_directory": project_dir}

    client_manager = MagicMock()
    mock_client = MagicMock()
    mock_client.session_id = "new-sess-123"
    client_manager.disconnect = AsyncMock()
    client_manager.get_or_connect = AsyncMock(return_value=mock_client)
    context.bot_data = {"client_manager": client_manager}

    await orchestrator.handle_new(update, context)

    client_manager.get_or_connect.assert_called_once()
    call_kwargs = client_manager.get_or_connect.call_args
    assert call_kwargs.kwargs.get("force_new") is True
    assert context.user_data["claude_session_id"] == "new-sess-123"
    assert context.user_data["force_new_session"] is False
    msg = update.message.reply_text.call_args[0][0]
    assert "Ready" in msg


async def test_agentic_new_connection_failure_falls_back(
    agentic_settings, deps, tmp_path
):
    """/new falls back gracefully if SDK connection fails."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    update = MagicMock()
    update.effective_user.id = 42
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"current_directory": project_dir}

    client_manager = MagicMock()
    client_manager.disconnect = AsyncMock()
    client_manager.get_or_connect = AsyncMock(side_effect=Exception("connection error"))
    context.bot_data = {"client_manager": client_manager}

    await orchestrator.handle_new(update, context)

    update.message.reply_text.assert_called_once()
    assert context.user_data.get("force_new_session") is True


async def test_agentic_status_compact(agentic_settings, deps):
    """Agentic /status returns session status with directory and workspace info."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}

    await orchestrator.handle_status(update, context)

    call_args = update.message.reply_text.call_args
    text = call_args.args[0]
    # New format includes HTML tags and more detailed info
    assert "<b>Session:</b> none (send a message to start)" in text
    assert "<b>Directory:</b>" in text


async def test_agentic_text_calls_claude(agentic_settings, deps):
    """Agentic text handler calls Claude via ClientManager and returns response."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    # Mock submit result from UserClient
    mock_submit_result = MagicMock()
    mock_submit_result.session_id = "session-abc"
    mock_submit_result.response_text = "Hello, I can help with that!"
    mock_submit_result.cost = None
    mock_submit_result.duration_ms = None
    mock_submit_result.num_turns = 1

    mock_client = AsyncMock()
    mock_client.submit = AsyncMock(return_value=mock_submit_result)
    mock_client.session_id = "session-abc"
    mock_client.is_connected = False

    client_manager = AsyncMock()
    client_manager.get_or_connect = AsyncMock(return_value=mock_client)
    client_manager.get_active_client = MagicMock(return_value=None)
    client_manager.update_session_id = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "Help me with this code"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    # Progress message mock
    progress_msg = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "client_manager": client_manager,
        "storage": None,
        "audit_logger": None,
    }

    await orchestrator.handle_text(update, context)

    # ClientManager was used to submit the query
    client_manager.get_or_connect.assert_called_once()
    mock_client.submit.assert_called_once()

    # Session ID updated
    assert context.user_data["claude_session_id"] == "session-abc"

    # Progress message finalized (edit_text called with done=True header)
    progress_msg.edit_text.assert_called()

    # Response sent without keyboard (reply_markup=None)
    response_calls = [
        c
        for c in update.message.reply_text.call_args_list
        if c != update.message.reply_text.call_args_list[0]
    ]
    for call in response_calls:
        assert call.kwargs.get("reply_markup") is None


async def test_agentic_callback_scoped_to_cd_pattern(agentic_settings, deps):
    """Agentic callback handler is registered with cd: pattern filter."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler

    cb_handlers = [
        call[0][0]
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    assert len(cb_handlers) == 1
    # The pattern attribute should match cd: prefixed data
    assert cb_handlers[0].pattern is not None
    assert cb_handlers[0].pattern.match("cd:my_project")


async def test_agentic_start_escapes_html_in_name(agentic_settings, deps):
    """Names with HTML-special characters are escaped safely."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "A<B>&C"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}

    await orchestrator.handle_start(update, context)

    call_kwargs = update.message.reply_text.call_args
    text = call_kwargs.args[0]
    # HTML-special characters should be escaped
    assert "A&lt;B&gt;&amp;C" in text
    # parse_mode is HTML
    assert call_kwargs.kwargs.get("parse_mode") == "HTML"


async def test_agentic_text_logs_failure_on_error(agentic_settings, deps):
    """Failed Claude runs are logged with success=False."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(side_effect=Exception("Claude broke"))

    audit_logger = AsyncMock()
    audit_logger.log_command = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "do something"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "audit_logger": audit_logger,
    }

    await orchestrator.handle_text(update, context)

    # Audit logged with success=False
    audit_logger.log_command.assert_called_once()
    call_kwargs = audit_logger.log_command.call_args
    assert call_kwargs.kwargs["success"] is False


# --- _redact_secrets / _summarize_tool_input tests ---


class TestRedactSecrets:
    """Ensure sensitive substrings are redacted from Bash command summaries."""

    def test_safe_command_unchanged(self):
        assert (
            _redact_secrets("poetry run pytest tests/ -v")
            == "poetry run pytest tests/ -v"
        )

    def test_anthropic_api_key_redacted(self):
        key = "sk-ant-api03-abc123def456ghi789jkl012mno345"
        cmd = f"ANTHROPIC_API_KEY={key}"
        result = _redact_secrets(cmd)
        assert key not in result
        assert "***" in result

    def test_sk_key_redacted(self):
        cmd = "curl -H 'Authorization: Bearer sk-1234567890abcdefghijklmnop'"
        result = _redact_secrets(cmd)
        assert "sk-1234567890abcdefghijklmnop" not in result
        assert "***" in result

    def test_github_pat_redacted(self):
        cmd = "git clone https://ghp_abcdefghijklmnop1234@github.com/user/repo"
        result = _redact_secrets(cmd)
        assert "ghp_abcdefghijklmnop1234" not in result
        assert "***" in result

    def test_aws_key_redacted(self):
        cmd = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = _redact_secrets(cmd)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "***" in result

    def test_flag_token_redacted(self):
        cmd = "mycli --token=supersecretvalue123"
        result = _redact_secrets(cmd)
        assert "supersecretvalue123" not in result
        assert "--token=" in result or "--token" in result

    def test_password_env_redacted(self):
        cmd = "PASSWORD=MyS3cretP@ss! ./run.sh"
        result = _redact_secrets(cmd)
        assert "MyS3cretP@ss!" not in result
        assert "***" in result

    def test_bearer_token_redacted(self):
        cmd = "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig'"
        result = _redact_secrets(cmd)
        assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in result

    def test_connection_string_redacted(self):
        cmd = "psql postgresql://admin:secret_password@db.host:5432/mydb"
        result = _redact_secrets(cmd)
        assert "secret_password" not in result

    def test_summarize_tool_input_bash_redacts(self, agentic_settings, deps):
        """summarize_tool_input applies redaction to Bash commands."""
        from src.bot.progress import summarize_tool_input

        result = summarize_tool_input(
            "Bash",
            {"command": "curl --token=mysupersecrettoken123 https://api.example.com"},
        )
        assert "mysupersecrettoken123" not in result
        assert "***" in result

    def test_summarize_tool_input_non_bash_unchanged(self, agentic_settings, deps):
        """Non-Bash tools don't go through redaction."""
        from src.bot.progress import summarize_tool_input

        result = summarize_tool_input("Read", {"file_path": "/home/user/.env"})
        assert result == ".env"


# --- Typing heartbeat tests ---


class TestTypingHeartbeat:
    """Verify typing indicator stays alive independently of stream events."""

    async def test_heartbeat_sends_typing_action(self, agentic_settings, deps):
        """Heartbeat sends typing actions at the configured interval."""
        chat = AsyncMock()
        chat.send_action = AsyncMock()

        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        # Let the heartbeat fire a few times
        await asyncio.sleep(0.2)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have been called multiple times
        assert chat.send_action.call_count >= 2
        chat.send_action.assert_called_with("typing")

    async def test_heartbeat_cancels_cleanly(self, agentic_settings, deps):
        """Cancelling the heartbeat task does not raise."""
        chat = AsyncMock()
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        heartbeat.cancel()
        # Should not raise
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        assert heartbeat.cancelled() or heartbeat.done()

    async def test_heartbeat_survives_send_action_errors(self, agentic_settings, deps):
        """Heartbeat keeps running even if send_action raises."""
        chat = AsyncMock()
        call_count = [0]

        async def flaky_send_action(action: str) -> None:
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Network error")

        chat.send_action = flaky_send_action

        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        await asyncio.sleep(0.3)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have called send_action more than 2 times (survived errors)
        assert call_count[0] >= 3

    async def test_stream_callback_independent_of_typing(self, agentic_settings, deps):
        """Stream callback no longer sends typing — that's the heartbeat's job."""
        from src.bot.progress import ProgressMessageManager, build_stream_callback

        progress_msg = AsyncMock()
        import time

        manager = ProgressMessageManager(
            initial_message=progress_msg, start_time=time.time()
        )
        callback = build_stream_callback(manager)
        assert callback is not None

        # Verify the callback signature doesn't accept a 'chat' parameter
        # (typing is no longer handled by the stream callback)
        import inspect

        sig = inspect.signature(build_stream_callback)
        assert "chat" not in sig.parameters


async def test_group_thread_mode_allows_non_configured_chat(
    group_thread_settings, deps
):
    """In group thread mode, messages from other chats are allowed through."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_threads_manager = MagicMock()
    deps["project_threads_manager"] = project_threads_manager

    called = {"value": False}

    async def dummy_handler(update, context):
        called["value"] = True

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1002222222  # different from configured -1001234567890
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    # Non-configured chats are allowed through
    assert called["value"] is True


async def test_group_thread_mode_rejects_unbound_topic(group_thread_settings, deps):
    """In the configured chat, topics not bound to a directory are rejected."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_threads_manager = MagicMock()
    project_threads_manager.resolve_directory = AsyncMock(return_value=None)
    deps["project_threads_manager"] = project_threads_manager

    called = {"value": False}

    async def dummy_handler(update, context):
        called["value"] = True

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1001234567890  # configured chat
    update.effective_chat.type = (
        "supergroup"  # must be supergroup to trigger thread routing
    )
    update.effective_message.message_thread_id = 777
    update.effective_message.direct_messages_topic = None
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {"project_threads_manager": project_threads_manager}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is False
    update.effective_message.reply_text.assert_called_once()


async def test_thread_mode_loads_directory_from_mapping(group_thread_settings, deps):
    """Thread mode resolves directory from mapping and sets current_directory."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_path = group_thread_settings.approved_directory / "project_a"

    project_threads_manager = MagicMock()
    project_threads_manager.resolve_directory = AsyncMock(
        return_value=str(project_path)
    )
    deps["project_threads_manager"] = project_threads_manager

    captured = {"directory": None}

    async def dummy_handler(update, context):
        captured["directory"] = context.user_data.get("current_directory")

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1001234567890
    update.effective_chat.type = (
        "supergroup"  # must be supergroup to trigger thread routing
    )
    update.effective_message.message_thread_id = 777
    update.effective_message.direct_messages_topic = None
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {"project_threads_manager": project_threads_manager}
    context.user_data = {}

    await wrapped(update, context)

    project_threads_manager.resolve_directory.assert_awaited_once_with(
        -1001234567890, 777
    )
    assert captured["directory"] == project_path


async def test_sync_threads_bypasses_thread_gate(group_thread_settings, deps):
    """sync_threads command bypasses strict thread routing gate."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    called = {"value": False}

    async def sync_threads(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(sync_threads)

    update = MagicMock()
    update.effective_chat.id = -1002222222
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is True


async def test_private_mode_start_bypasses_thread_gate(private_thread_settings, deps):
    """Private mode allows /start outside topics."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    called = {"value": False}

    async def start_command(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(start_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is True
    project_threads_manager.resolve_project.assert_not_called()


async def test_general_topic_sets_in_general_topic_flag(group_thread_settings, deps):
    """Messages in the general topic (no thread_id) set _in_general_topic flag."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_threads_manager = MagicMock()

    captured = {"flag": None}

    async def dummy_handler(update, context):
        captured["flag"] = context.user_data.get("_in_general_topic")

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1001234567890  # configured chat
    update.effective_chat.type = (
        "supergroup"  # must be supergroup to trigger thread routing
    )
    update.effective_message.message_thread_id = None
    update.effective_message.direct_messages_topic = None
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {"project_threads_manager": project_threads_manager}
    context.user_data = {}

    await wrapped(update, context)

    assert captured["flag"] is True


async def test_thread_topic_clears_in_general_topic_flag(group_thread_settings, deps):
    """Messages in a bound topic clear the _in_general_topic flag."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)
    project_path = group_thread_settings.approved_directory / "project_a"

    project_threads_manager = MagicMock()
    project_threads_manager.resolve_directory = AsyncMock(
        return_value=str(project_path)
    )

    captured = {"flag": "unset"}

    async def dummy_handler(update, context):
        captured["flag"] = context.user_data.get("_in_general_topic", "absent")

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1001234567890
    update.effective_chat.type = (
        "supergroup"  # must be supergroup to trigger thread routing
    )
    update.effective_message.message_thread_id = 777
    update.effective_message.direct_messages_topic = None
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {"project_threads_manager": project_threads_manager}
    context.user_data = {"_in_general_topic": True}

    await wrapped(update, context)

    assert captured["flag"] == "absent"
