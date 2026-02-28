# Native Skill Pass-Through Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace manual skill discovery/body injection with SDK native handling — pass `/skill_name args` verbatim to the CLI and use `get_server_info()` for the `/commands` menu.

**Architecture:** `UserClient` caches the commands list from `get_server_info()` after connect. The orchestrator checks unrecognized `/commands` against this cache and passes them verbatim to `query()`. The `/commands` handler builds its inline keyboard from the cached list. `src/skills/loader.py` functions are removed.

**Tech Stack:** Python 3.12, claude-agent-sdk (`ClaudeSDKClient.get_server_info()`), python-telegram-bot

---

### Task 1: Add `get_server_info()` caching to UserClient

**Files:**
- Modify: `src/claude/user_client.py`
- Test: `tests/unit/test_claude/test_user_client.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_claude/test_user_client.py`:

```python
class TestUserClientSkillsCache:
    """Test get_server_info() caching after connect."""

    @pytest.mark.asyncio
    async def test_available_commands_populated_after_start(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.get_server_info = AsyncMock(return_value={
            "commands": [
                {"name": "brainstorm", "description": "Brainstorm ideas", "argumentHint": "<topic>"},
                {"name": "commit", "description": "Commit changes", "argumentHint": ""},
            ]
        })
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert len(client.available_commands) == 2
            assert client.available_commands[0]["name"] == "brainstorm"
            await client.stop()

    @pytest.mark.asyncio
    async def test_available_commands_empty_on_server_info_failure(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.get_server_info = AsyncMock(return_value=None)
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert client.available_commands == []
            await client.stop()

    @pytest.mark.asyncio
    async def test_available_commands_cleared_after_stop(self) -> None:
        mock_sdk = AsyncMock()
        mock_sdk.get_server_info = AsyncMock(return_value={
            "commands": [{"name": "test", "description": "", "argumentHint": ""}]
        })
        mock_options = MagicMock()

        with patch("src.claude.user_client.ClaudeSDKClient", return_value=mock_sdk):
            client = UserClient(user_id=1, directory="/dir")
            await client.start(mock_options)
            assert len(client.available_commands) == 1
            await client.stop()
            assert client.available_commands == []

    def test_has_command_checks_cache(self) -> None:
        client = UserClient(user_id=1, directory="/dir")
        client._available_commands = [
            {"name": "brainstorm", "description": "Ideas", "argumentHint": ""},
            {"name": "commit", "description": "Commit", "argumentHint": ""},
        ]
        assert client.has_command("brainstorm") is True
        assert client.has_command("nonexistent") is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py::TestUserClientSkillsCache -v`
Expected: FAIL — `available_commands` attribute doesn't exist

**Step 3: Write minimal implementation**

In `src/claude/user_client.py`, add to `__init__`:

```python
self._available_commands: list[dict[str, Any]] = []
```

Add property:

```python
@property
def available_commands(self) -> list[dict[str, Any]]:
    """Cached commands from get_server_info()."""
    return list(self._available_commands)
```

Add method:

```python
def has_command(self, name: str) -> bool:
    """Check if a command name exists in the cached list."""
    return any(cmd["name"] == name for cmd in self._available_commands)
```

In `_worker()`, after `await self._sdk_client.connect()` (line 137), add:

```python
# Cache available commands from CLI
try:
    server_info = await self._sdk_client.get_server_info()
    if server_info and "commands" in server_info:
        self._available_commands = server_info["commands"]
        logger.info(
            "cached_available_commands",
            user_id=self.user_id,
            count=len(self._available_commands),
        )
except Exception as e:
    logger.warning("failed_to_get_server_info", error=str(e))
```

In `_worker()` finally block, before the `self._running = False` line, add:

```python
self._available_commands = []
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_user_client.py::TestUserClientSkillsCache -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/user_client.py tests/unit/test_claude/test_user_client.py
git commit -m "feat: cache available commands from get_server_info() in UserClient"
```

---

### Task 2: Expose skills listing through ClientManager

**Files:**
- Modify: `src/claude/client_manager.py`
- Test: `tests/unit/test_claude/test_client_manager.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_claude/test_client_manager.py`:

```python
class TestGetAvailableCommands:
    """Test get_available_commands() delegates to active client."""

    def test_returns_commands_from_active_client(self) -> None:
        repo = _make_mock_bot_session_repo()
        manager = ClientManager(bot_session_repo=repo)
        mock_client = _make_mock_user_client()
        mock_client.available_commands = [
            {"name": "brainstorm", "description": "Ideas", "argumentHint": ""},
        ]
        manager._clients[1] = mock_client
        result = manager.get_available_commands(user_id=1)
        assert len(result) == 1
        assert result[0]["name"] == "brainstorm"

    def test_returns_empty_for_unknown_user(self) -> None:
        repo = _make_mock_bot_session_repo()
        manager = ClientManager(bot_session_repo=repo)
        result = manager.get_available_commands(user_id=999)
        assert result == []
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_claude/test_client_manager.py::TestGetAvailableCommands -v`
Expected: FAIL — `get_available_commands` method doesn't exist

**Step 3: Write minimal implementation**

In `src/claude/client_manager.py`, add method:

```python
def get_available_commands(self, user_id: int) -> list[dict]:
    """Return cached commands for the user's active client, or []."""
    client = self._clients.get(user_id)
    if client is None:
        return []
    return client.available_commands
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_claude/test_client_manager.py::TestGetAvailableCommands -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/claude/client_manager.py tests/unit/test_claude/test_client_manager.py
git commit -m "feat: expose get_available_commands() on ClientManager"
```

---

### Task 3: Simplify skill invocation in `agentic_text` to pass-through

**Files:**
- Modify: `src/bot/orchestrator.py` (lines 1066-1155)
- Test: `tests/unit/test_bot/test_commands_command.py`

**Step 1: Write the failing test**

Replace `test_skill_text_invocation` in `tests/unit/test_bot/test_commands_command.py`:

```python
@pytest.mark.asyncio
async def test_skill_text_passthrough(orchestrator, mock_update, mock_context):
    """Test that /skillname args passes verbatim to Claude without body injection."""
    mock_update.message.text = "/deploy production"

    mock_client_manager = MagicMock()
    mock_client = MagicMock()
    mock_client.has_command.return_value = True
    mock_client_manager.get_active_client.return_value = mock_client

    mock_claude_integration = MagicMock()
    mock_claude_integration.run_command = AsyncMock()
    mock_claude_response = MagicMock()
    mock_claude_response.session_id = "test-session"
    mock_claude_response.content = "Deployed to production"
    mock_claude_integration.run_command.return_value = mock_claude_response

    mock_context.bot_data = {
        "client_manager": mock_client_manager,
        "claude_integration": mock_claude_integration,
    }
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "old-session",
    }

    with patch("src.bot.utils.formatting.ResponseFormatter") as mock_formatter_class:
        mock_formatter = MagicMock()
        mock_formatted_msg = MagicMock()
        mock_formatted_msg.text = "Deployed to production"
        mock_formatted_msg.parse_mode = "HTML"
        mock_formatter.format_claude_response.return_value = [mock_formatted_msg]
        mock_formatter_class.return_value = mock_formatter

        with patch.object(orchestrator, "_make_stream_callback", return_value=AsyncMock()):
            with patch.object(orchestrator, "_start_typing_heartbeat") as mock_heartbeat:
                mock_heartbeat.return_value = MagicMock()
                with patch.object(orchestrator, "_get_verbose_level", return_value=1):
                    mock_update.message.reply_text.return_value = MagicMock(
                        delete=AsyncMock()
                    )
                    with patch.object(orchestrator, "_run_claude_query") as mock_query:
                        mock_query.return_value = mock_claude_response
                        await orchestrator.agentic_text(mock_update, mock_context)

    # Verify prompt is passed verbatim — no <skill-invocation> wrapping
    mock_query.assert_called_once()
    call_kwargs = mock_query.call_args
    prompt = call_kwargs.kwargs.get("prompt") or call_kwargs.args[0]
    assert prompt == "/deploy production"
    assert "<skill-invocation>" not in prompt
    assert "<skill-body>" not in prompt
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py::test_skill_text_passthrough -v`
Expected: FAIL — prompt still contains `<skill-invocation>` wrapping

**Step 3: Write minimal implementation**

In `src/bot/orchestrator.py`, replace lines 1066-1155 (the skill invocation block in `agentic_text`) with:

```python
        # Check if this is a skill invocation (e.g., "/skillname args")
        # Skip if it's a registered bot command — pass verbatim to CLI
        if message_text.startswith("/"):
            parts = message_text[1:].split(None, 1)
            if parts:
                potential_skill_name = parts[0]

                # List of registered bot commands to skip
                registered_commands = {
                    "start",
                    "new",
                    "interrupt",
                    "status",
                    "verbose",
                    "compact",
                    "model",
                    "repo",
                    "sessions",
                    "commands",
                    "sync_threads",
                }

                if potential_skill_name not in registered_commands:
                    # Check cached commands from SDK — if found, pass
                    # verbatim. The CLI handles body loading, placeholder
                    # resolution, and prompt injection natively.
                    _cm: Optional[ClientManager] = context.bot_data.get(
                        "client_manager"
                    )
                    _active = _cm.get_active_client(user_id) if _cm else None
                    if _active and _active.has_command(potential_skill_name):
                        logger.info(
                            "skill_passthrough",
                            skill_name=potential_skill_name,
                            user_id=user_id,
                        )
                    # message_text stays as-is — sent verbatim to query()
```

Also remove the import of `discover_skills, load_skill_body, resolve_skill_prompt` from line 36 (will be cleaned up in Task 6).

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py::test_skill_text_passthrough -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_bot/test_commands_command.py
git commit -m "feat: pass /skill_name verbatim to CLI instead of injecting body"
```

---

### Task 4: Simplify `skill:` callback handler to pass-through

**Files:**
- Modify: `src/bot/orchestrator.py` (lines 1999-2138)
- Test: `tests/unit/test_bot/test_commands_command.py`

**Step 1: Write the failing test**

Replace `test_skill_callback_executes` in `tests/unit/test_bot/test_commands_command.py`:

```python
@pytest.mark.asyncio
async def test_skill_callback_passthrough(orchestrator, mock_context):
    """Test that skill: callback passes /skill_name verbatim to Claude."""
    mock_query = MagicMock()
    mock_query.data = "skill:deploy"
    mock_query.from_user.id = 12345
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()
    mock_query.message.reply_text = AsyncMock()
    mock_query.message.chat.send_action = AsyncMock()

    mock_update = MagicMock(spec=Update)
    mock_update.callback_query = mock_query

    mock_claude_response = MagicMock()
    mock_claude_response.session_id = "test-session"
    mock_claude_response.content = "Deployment complete"

    mock_context.bot_data = {}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "old-session",
    }

    with patch("src.bot.utils.formatting.ResponseFormatter") as mock_formatter_class:
        mock_formatter = MagicMock()
        mock_formatted_msg = MagicMock()
        mock_formatted_msg.text = "Deployment complete"
        mock_formatted_msg.parse_mode = "HTML"
        mock_formatter.format_claude_response.return_value = [mock_formatted_msg]
        mock_formatter_class.return_value = mock_formatter

        with patch.object(orchestrator, "_make_stream_callback", return_value=AsyncMock()):
            with patch.object(orchestrator, "_start_typing_heartbeat") as mock_heartbeat:
                mock_heartbeat.return_value = MagicMock()
                with patch.object(orchestrator, "_get_verbose_level", return_value=1):
                    mock_query.message.reply_text.return_value = MagicMock(
                        delete=AsyncMock()
                    )
                    with patch.object(orchestrator, "_run_claude_query") as mock_run:
                        mock_run.return_value = mock_claude_response
                        await orchestrator._agentic_callback(mock_update, mock_context)

    # Verify prompt passed verbatim as /skill_name
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args
    prompt = call_kwargs.kwargs.get("prompt") or call_kwargs.args[0]
    assert prompt == "/deploy"
    assert "<skill-invocation>" not in prompt
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py::test_skill_callback_passthrough -v`
Expected: FAIL — still uses old `discover_skills` + body injection flow

**Step 3: Write minimal implementation**

In `src/bot/orchestrator.py`, replace the `if prefix == "skill":` block (lines 1999-2138) with:

```python
        # Handle skill callbacks — pass /skill_name verbatim to CLI
        if prefix == "skill":
            skill_name = value
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directories[0]
            )

            # Show running message
            await query.edit_message_text(
                f"⚙️ Running skill: <b>{escape_html(skill_name)}</b>...",
                parse_mode="HTML",
            )

            # Execute via Claude — pass /skill_name verbatim
            prompt = f"/{skill_name}"
            user_id = query.from_user.id
            force_new = bool(context.user_data.get("force_new_session"))
            session_id = context.user_data.get("claude_session_id")

            verbose_level = self._get_verbose_level(context)
            tool_log: List[Dict[str, Any]] = []
            start_time = time.time()

            progress_msg = await query.message.reply_text("Working...")
            on_stream = self._make_stream_callback(
                verbose_level, progress_msg, tool_log, start_time
            )

            chat = query.message.chat
            heartbeat = self._start_typing_heartbeat(chat)

            success = True
            try:
                claude_response = await self._run_claude_query(
                    prompt=prompt,
                    user_id=user_id,
                    current_dir=current_dir,
                    session_id=session_id,
                    force_new=force_new,
                    on_stream=on_stream,
                    context=context,
                )
                # ... rest of response handling stays the same
```

Keep the existing response formatting, error handling, and audit logging code below the `_run_claude_query` call unchanged.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py::test_skill_callback_passthrough -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_bot/test_commands_command.py
git commit -m "feat: simplify skill: callback to pass /name verbatim to CLI"
```

---

### Task 5: Rewrite `/commands` handler to use cached commands

**Files:**
- Modify: `src/bot/orchestrator.py` (`agentic_commands` method, lines 1689-1797)
- Test: `tests/unit/test_bot/test_commands_command.py`

**Step 1: Write the failing test**

Replace `test_commands_shows_project_and_personal_skills` and `test_commands_no_skills_shows_message` in `tests/unit/test_bot/test_commands_command.py`:

```python
@pytest.mark.asyncio
async def test_commands_shows_cached_commands(orchestrator, mock_update, mock_context):
    """Test /commands uses cached commands from get_server_info()."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = [
        {"name": "brainstorm", "description": "Brainstorm ideas", "argumentHint": "<topic>"},
        {"name": "commit", "description": "Commit changes", "argumentHint": ""},
        {"name": "deploy", "description": "Deploy app", "argumentHint": ""},
    ]
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args[0][0]
    assert "Available Skills" in message_text
    assert "brainstorm" in message_text
    assert "commit" in message_text
    assert "deploy" in message_text

    # Check inline keyboard exists
    reply_markup = call_args[1]["reply_markup"]
    assert reply_markup is not None


@pytest.mark.asyncio
async def test_commands_no_active_session(orchestrator, mock_update, mock_context):
    """Test /commands shows message when no active session."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = []
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    message_text = mock_update.message.reply_text.call_args[0][0]
    # Should tell user to start a session or show empty state
    assert "No" in message_text or "session" in message_text.lower()


@pytest.mark.asyncio
async def test_commands_arg_hint_uses_switch_inline(orchestrator, mock_update, mock_context):
    """Test skills with argumentHint use switch_inline_query_current_chat."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = [
        {"name": "search", "description": "Search code", "argumentHint": "<query>"},
    ]
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    reply_markup = mock_update.message.reply_text.call_args[1]["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]
    assert button.switch_inline_query_current_chat == "/search "


@pytest.mark.asyncio
async def test_commands_no_arg_hint_uses_callback(orchestrator, mock_update, mock_context):
    """Test skills without argumentHint use callback_data."""
    mock_client_manager = MagicMock()
    mock_client_manager.get_available_commands.return_value = [
        {"name": "deploy", "description": "Deploy app", "argumentHint": ""},
    ]
    mock_context.bot_data = {"client_manager": mock_client_manager}

    await orchestrator.agentic_commands(mock_update, mock_context)

    reply_markup = mock_update.message.reply_text.call_args[1]["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]
    assert button.callback_data == "skill:deploy"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py -k "test_commands_" -v`
Expected: FAIL — still uses `discover_skills`

**Step 3: Write minimal implementation**

Replace the `agentic_commands` method in `src/bot/orchestrator.py`:

```python
async def agentic_commands(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show available skills as inline keyboard buttons."""
    user_id = update.effective_user.id
    client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")

    commands: list[dict] = []
    if client_manager:
        commands = client_manager.get_available_commands(user_id)

    if not commands:
        await update.message.reply_text(
            "📝 <b>No Skills Available</b>\n\n"
            "Start a session first (send any message), "
            "then use /commands to see available skills.\n\n"
            "Skills are loaded from:\n"
            "  • <code>.claude/skills/&lt;name&gt;/SKILL.md</code> (project)\n"
            "  • <code>~/.claude/skills/&lt;name&gt;/SKILL.md</code> (personal)\n"
            "  • Installed plugins",
            parse_mode="HTML",
        )
        return

    # Build inline keyboard
    def _cmd_button(cmd: dict) -> InlineKeyboardButton:
        name = cmd["name"]
        hint = cmd.get("argumentHint", "")
        if hint:
            return InlineKeyboardButton(
                f"{name} ...",
                switch_inline_query_current_chat=f"/{name} ",
            )
        return InlineKeyboardButton(name, callback_data=f"skill:{name}")

    keyboard_rows = [[_cmd_button(cmd)] for cmd in commands]

    # Truncate to fit Telegram limits (max ~100 buttons)
    if len(keyboard_rows) > 100:
        keyboard_rows = keyboard_rows[:100]

    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    # Build message text
    lines: list[str] = ["<b>Available Skills</b>\n"]
    for cmd in commands:
        desc = cmd.get("description", "")
        line = f"  • <code>{escape_html(cmd['name'])}</code>"
        if desc:
            line += f" — {escape_html(desc[:80])}"
        lines.append(line)

    message = "\n".join(lines)
    if len(message) > 4000:
        message = message[:3950] + "\n\n<i>... truncated</i>"

    await update.message.reply_text(
        message,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_bot/test_commands_command.py
git commit -m "feat: rewrite /commands to use cached get_server_info() data"
```

---

### Task 6: Remove `src/skills/loader.py` and clean up imports

**Files:**
- Delete: `src/skills/loader.py`
- Modify: `src/bot/orchestrator.py` (remove import on line 36)
- Modify: `tests/unit/test_bot/test_commands_command.py` (remove `SkillMetadata` import)

**Step 1: Check no other files import from skills.loader**

Run: `grep -rn "from.*skills.*loader\|from.*skills import\|import.*skills" src/ tests/ --include="*.py"`

If any other imports found, update those too.

**Step 2: Remove the import from orchestrator**

In `src/bot/orchestrator.py`, delete line 36:

```python
from ..skills.loader import discover_skills, load_skill_body, resolve_skill_prompt
```

**Step 3: Remove SkillMetadata import from test file**

In `tests/unit/test_bot/test_commands_command.py`, remove:

```python
from src.skills.loader import SkillMetadata
```

**Step 4: Delete the loader module**

```bash
rm src/skills/loader.py
```

Check if `src/skills/__init__.py` exists and if it re-exports anything from loader. If `src/skills/` has no other modules, consider removing the directory.

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS, no import errors

**Step 6: Commit**

```bash
git add -A
git commit -m "refactor: remove skills/loader.py — replaced by SDK native handling"
```

---

### Task 7: Handle "Unknown skill" error from CLI

**Files:**
- Modify: `src/bot/orchestrator.py` (in `agentic_text` and/or `_agentic_callback`)
- Test: `tests/unit/test_bot/test_commands_command.py`

**Step 1: Write the failing test**

Add to `tests/unit/test_bot/test_commands_command.py`:

```python
@pytest.mark.asyncio
async def test_unknown_skill_shows_error(orchestrator, mock_update, mock_context):
    """Test that 'Unknown skill: X' from CLI is surfaced to user."""
    mock_update.message.text = "/nonexistent"

    mock_client_manager = MagicMock()
    mock_client = MagicMock()
    mock_client.has_command.return_value = False
    mock_client_manager.get_active_client.return_value = mock_client
    mock_client_manager.get_available_commands.return_value = [
        {"name": "brainstorm", "description": "Ideas", "argumentHint": ""},
    ]

    mock_context.bot_data = {"client_manager": mock_client_manager}
    mock_context.user_data = {
        "current_directory": Path("/test/workspace"),
        "claude_session_id": "sess",
    }

    await orchestrator.agentic_text(mock_update, mock_context)

    # Should show a "not found" error with suggestions
    mock_update.message.reply_text.assert_called()
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args[0][0]
    assert "not found" in message_text.lower() or "unknown" in message_text.lower()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py::test_unknown_skill_shows_error -v`
Expected: FAIL

**Step 3: Write minimal implementation**

In the skill detection block of `agentic_text` (from Task 3), add handling when the command is not in the cache:

```python
                if potential_skill_name not in registered_commands:
                    _cm: Optional[ClientManager] = context.bot_data.get(
                        "client_manager"
                    )
                    _active = _cm.get_active_client(user_id) if _cm else None
                    if _active and _active.has_command(potential_skill_name):
                        logger.info(
                            "skill_passthrough",
                            skill_name=potential_skill_name,
                            user_id=user_id,
                        )
                        # message_text stays as-is
                    elif _active:
                        # Command not found in cache — show error
                        await update.message.reply_text(
                            f"❌ Skill <code>{escape_html(potential_skill_name)}</code> "
                            f"not found. Use /commands to see available skills.",
                            parse_mode="HTML",
                        )
                        return
                    # If no active client, fall through to normal text handling
                    # (will trigger a connect, and CLI will handle the /command)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot/test_commands_command.py::test_unknown_skill_shows_error -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_bot/test_commands_command.py
git commit -m "feat: show friendly error for unknown skill commands"
```

---

### Task 8: Final verification

**Step 1: Run full lint + type check + tests**

```bash
uv run make lint
uv run pytest tests/ -v --tb=short
```

Expected: All pass, no import errors, no type errors.

**Step 2: Verify the old skill loading code is fully removed**

```bash
grep -rn "discover_skills\|load_skill_body\|resolve_skill_prompt\|skill-invocation\|skill-body\|SkillMetadata" src/ tests/ --include="*.py"
```

Expected: No matches (zero references to old skill loading).

**Step 3: Commit any final cleanup**

If any stray references found, clean them up and commit.

```bash
git add -A
git commit -m "chore: final cleanup of native skill pass-through migration"
```
