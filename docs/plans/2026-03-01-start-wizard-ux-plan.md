# /start Wizard UX Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `/add` with a unified `/start` wizard that lets users pick a directory then a session, creating a 1:1 topic-session binding. Auto-name topics via Haiku after ~3 exchanges.

**Architecture:** The wizard reuses the existing directory browser (inline keyboards) with new `start_nav:`/`start_sel:` callback prefixes. After dir selection, a session picker shows existing sessions + "New". Topic creation eagerly connects the session. A background Haiku call renames topics after 3 user messages.

**Tech Stack:** python-telegram-bot (forum topic APIs), anthropic (Haiku for naming), existing `ClientManager`, `ProjectThreadManager`, `TopicLifecycleManager`.

---

### Task 1: TopicLifecycleManager — add rename_topic method

**Files:**
- Modify: `src/projects/lifecycle.py`
- Test: `tests/unit/test_topic_lifecycle.py`

**Step 1: Write the failing test**

Append to `tests/unit/test_topic_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_rename_topic_calls_edit():
    lifecycle = TopicLifecycleManager()
    bot = AsyncMock()
    await lifecycle.rename_topic(bot, chat_id=-1001234, message_thread_id=42, name="Fix auth bug")
    bot.edit_forum_topic.assert_called_once_with(
        chat_id=-1001234, message_thread_id=42, name="Fix auth bug"
    )


@pytest.mark.asyncio
async def test_rename_topic_swallows_error():
    lifecycle = TopicLifecycleManager()
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = TelegramError("forbidden")
    # Should not raise
    await lifecycle.rename_topic(bot, chat_id=-1001234, message_thread_id=42, name="test")
```

Add `from telegram.error import TelegramError` to the test imports if not present.

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_topic_lifecycle.py -v -k "rename"`
Expected: FAIL — `AttributeError: 'TopicLifecycleManager' object has no attribute 'rename_topic'`

**Step 3: Write minimal implementation**

Add to `src/projects/lifecycle.py` in `TopicLifecycleManager`:

```python
    async def rename_topic(
        self, bot: Bot, chat_id: int, message_thread_id: int, name: str
    ) -> None:
        """Rename a topic. Silently ignores errors (e.g. no permission)."""
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id, message_thread_id=message_thread_id, name=name
            )
        except TelegramError as e:
            logger.debug("rename_topic_failed", error=str(e))
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_topic_lifecycle.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/projects/lifecycle.py tests/unit/test_topic_lifecycle.py
git commit -m "feat: add rename_topic to TopicLifecycleManager"
```

---

### Task 2: TopicNameGenerator — Haiku-powered topic naming

**Files:**
- Create: `src/projects/topic_namer.py`
- Test: `tests/unit/test_topic_namer.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_topic_namer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.projects.topic_namer'`

**Step 3: Write minimal implementation**

```python
# src/projects/topic_namer.py
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
) -> Optional[str]:
    """Generate a concise topic name from conversation snippets.

    Returns None on failure (caller should keep existing name).
    """
    snippet = "\n".join(msg[:200] for msg in messages[:6])
    prompt = (
        f"Generate a concise topic title (3-6 words, no quotes) for this "
        f"coding session in {dir_name}/:\n\n{snippet}"
    )

    try:
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=30,
            messages=[{"role": "user", "content": prompt}],
        )
        name = response.content[0].text.strip().strip('"').strip("'")
        return name[:MAX_NAME_LENGTH] if name else None
    except Exception as e:
        logger.debug("topic_name_generation_failed", error=str(e))
        return None
```

**Step 4: Update `src/projects/__init__.py`**

Add the import:
```python
from .topic_namer import generate_topic_name
```

And update `__all__`:
```python
__all__ = ["ProjectThreadManager", "TopicLifecycleManager", "generate_topic_name"]
```

**Step 5: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/unit/test_topic_namer.py -v`
Expected: All 3 pass

**Step 6: Commit**

```bash
git add src/projects/topic_namer.py src/projects/__init__.py tests/unit/test_topic_namer.py
git commit -m "feat: Haiku-powered topic name generation"
```

---

### Task 3: Update create_topic to accept session_id

**Files:**
- Modify: `src/projects/thread_manager.py`
- Test: `tests/unit/test_thread_manager.py` (check if exists, otherwise update existing tests)

**Step 1: Update `create_topic` signature**

In `src/projects/thread_manager.py`, update `create_topic` to accept an optional `session_id` parameter and pass it to `repository.upsert`:

```python
    async def create_topic(
        self,
        bot: Bot,
        chat_id: int,
        user_id: int,
        directory: str,
        topic_name: str,
        session_id: Optional[str] = None,
    ) -> ChatSessionModel:
        """Create a forum topic and store the session binding."""
        topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
        await self.repository.upsert(
            chat_id=chat_id,
            message_thread_id=topic.message_thread_id,
            user_id=user_id,
            directory=directory,
            topic_name=topic_name,
            session_id=session_id,
        )
```

The rest of the method body stays the same.

**Step 2: Run existing tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass (new param is optional with default=None, backward compatible)

**Step 3: Commit**

```bash
git add src/projects/thread_manager.py
git commit -m "feat: thread_manager.create_topic accepts session_id"
```

---

### Task 4: Replace /add with /start wizard — directory browser in General

**Files:**
- Modify: `src/bot/orchestrator.py`

**Context:** Replace `agentic_add` with `/start` wizard behavior in supergroup General topics. The wizard shows a directory browser. In DMs or inside topics, `/start` keeps its current behavior.

**Step 1: Update `agentic_start` method**

Replace the current `agentic_start` method (around line 442) with:

```python
    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Start: welcome in DM/topic, wizard in supergroup General."""
        user = update.effective_user
        chat = update.effective_chat

        # Supergroup General topic — show directory browser (wizard step 1)
        is_supergroup = chat is not None and chat.type == "supergroup"
        in_general = context.user_data.get("_in_general_topic")

        if is_supergroup and in_general:
            await self._start_wizard_dir_browser(update, context)
            return

        # DM or inside a topic — show welcome
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n\n"
            f"<b>Commands:</b>\n"
            f"/new — Start fresh session\n"
            f"/interrupt — Interrupt running query\n"
            f"/status — Current session info\n"
            f"/model — Switch Claude model\n"
            f"/commands — Browse available skills\n"
            f"/compact — Compress context\n"
            f"/repo — Switch workspace",
            parse_mode="HTML",
        )

    async def _start_wizard_dir_browser(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Wizard step 1: show directory browser with start_ prefixed callbacks."""
        browse_dir = self.settings.approved_directories[0]
        multi_root = len(self.settings.approved_directories) > 1
        keyboard_rows = build_browser_keyboard(browse_dir, browse_dir, multi_root=multi_root)

        # Remap callbacks: sel: -> start_sel:, nav: -> start_nav:
        remapped_rows = []
        for row in keyboard_rows:
            new_row = []
            for btn in row:
                data = btn.callback_data or ""
                if data.startswith("sel:"):
                    new_row.append(
                        InlineKeyboardButton(
                            btn.text, callback_data=f"start_sel:{data[4:]}"
                        )
                    )
                elif data.startswith("nav:"):
                    new_row.append(
                        InlineKeyboardButton(
                            btn.text, callback_data=f"start_nav:{data[4:]}"
                        )
                    )
                else:
                    new_row.append(btn)
            remapped_rows.append(new_row)

        await update.message.reply_text(
            build_browse_header(browse_dir, browse_dir),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(remapped_rows),
        )
```

**Step 2: Remove `agentic_add` method and `_create_project_topic` method**

Delete the `agentic_add` method (around line 1386-1447) and `_create_project_topic` method (around line 1449-1483). These are replaced by the wizard.

**Step 3: Update handler registration**

In `_register_agentic_handlers`, remove `("add", self.agentic_add)` from the handlers list. The `/start` handler already exists. `/add` is no longer a command.

**Step 4: Update `_inject_deps` management bypass list**

Change:
```python
is_management_bypass = handler.__name__ in {"sync_threads", "agentic_add", "agentic_remove"}
```
To:
```python
is_management_bypass = handler.__name__ in {"sync_threads", "agentic_remove"}
```

(`agentic_start` is already in `is_start_bypass`, which allows it through in General.)

**Step 5: Update callback pattern in handler registration**

Change the callback pattern from:
```python
pattern=r"^(cd:|nav:|sel:|add_nav:|add_sel:|session:|skill:|model:)"
```
To:
```python
pattern=r"^(cd:|nav:|sel:|start_nav:|start_sel:|start_ses:|session:|skill:|model:)"
```

**Step 6: Update `get_bot_commands()`**

In `group_commands`, replace `BotCommand("add", "Add a project topic")` with nothing (remove it). The `/start` command already exists in both command lists.

Update `group_commands` to include `/start`:
```python
BotCommand("start", "Start a new project topic"),
```

Remove `/resume` from `group_commands` (will be done in Task 6).

**Step 7: Update `registered_commands` set in `agentic_text`**

Remove `"add"` from the `registered_commands` set. Add `"start"` if not already present (it already is).

**Step 8: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass (some test counts may change if tests reference `/add`)

**Step 9: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: replace /add with /start wizard — directory browser in General"
```

---

### Task 5: Session picker + topic creation via start_ses: callback

**Files:**
- Modify: `src/bot/orchestrator.py`

**Context:** After the user picks a directory in the wizard, show a session picker. When they pick a session, create the topic and eagerly connect.

**Step 1: Add start_nav: and start_sel: callback handlers**

In `_agentic_callback`, replace the existing `add_nav:` and `add_sel:` handlers with `start_nav:` and `start_sel:`. Model them after the existing code but:

- `start_nav:` — same as `add_nav:` but using `start_browse_root` / `start_browse_rel` user_data keys and `start_nav:`/`start_sel:` callback prefixes
- `start_sel:` — resolves the path, stores it in `context.user_data["start_wizard_dir"]`, then shows the session picker

For the session picker in `start_sel:`, add a helper method:

```python
    async def _start_wizard_session_picker(
        self,
        message: Any,
        directory: Path,
        context: ContextTypes.DEFAULT_TYPE,
        edit: bool = False,
    ) -> None:
        """Wizard step 2: show session picker for the selected directory."""
        context.user_data["start_wizard_dir"] = str(directory)

        history_entries = read_claude_history()
        filtered_entries = filter_by_directory(history_entries, directory)
        sorted_entries = sorted(
            filtered_entries, key=lambda e: e.timestamp, reverse=True
        )

        keyboard_rows: List[list] = []

        if sorted_entries:
            for entry in sorted_entries[:8]:
                time_str = relative_time(entry.timestamp)
                first_msg = read_first_message(
                    session_id=entry.session_id,
                    project_dir=entry.project,
                )
                display_name = (first_msg or entry.display or entry.session_id[:12])[:40]
                button_label = f"{time_str} — {display_name}"
                keyboard_rows.append(
                    [InlineKeyboardButton(
                        button_label,
                        callback_data=f"start_ses:{entry.session_id}",
                    )]
                )

        keyboard_rows.append(
            [InlineKeyboardButton("+ New Session", callback_data="start_ses:new")]
        )

        reply_markup = InlineKeyboardMarkup(keyboard_rows)
        dir_name = directory.name

        if sorted_entries:
            text = (
                f"<b>Sessions in <code>{escape_html(dir_name)}/</code></b>\n\n"
                f"Select a session to resume or start a new one:"
            )
        else:
            text = (
                f"<b>No sessions in <code>{escape_html(dir_name)}/</code></b>\n\n"
                f"Start a new session:"
            )

        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
```

**Step 2: Add start_ses: callback handler**

In `_agentic_callback`, add a handler for `start_ses:`:

```python
        # Handle start_ses: callbacks (wizard step 3 — create topic + connect)
        if prefix == "start_ses":
            wizard_dir = context.user_data.get("start_wizard_dir")
            if not wizard_dir:
                await query.edit_message_text("Session expired. Use /start again.")
                return

            chat_id = query.message.chat.id if query.message else 0
            user_id = query.from_user.id
            directory = wizard_dir
            dir_name = Path(directory).name
            session_id = value if value != "new" else None

            manager = context.bot_data.get("project_threads_manager")
            if not manager:
                await query.edit_message_text("Project threads not configured.")
                return

            # Generate topic name
            if session_id:
                # Existing session — try Haiku name from transcript
                from src.projects.topic_namer import generate_topic_name as gen_name
                from src.claude.history import read_session_transcript

                transcript = read_session_transcript(session_id, directory, limit=3)
                messages = [m.text for m in transcript if m.text]
                haiku_name = await gen_name(messages, dir_name) if messages else None
                topic_name = haiku_name or f"{dir_name} — {session_id[:8]}"
                topic_named = bool(haiku_name)
            else:
                topic_name = dir_name
                topic_named = False

            # Avoid collision with existing topic names
            existing = await manager.list_topics(chat_id)
            existing_names = [t.topic_name for t in existing]
            if topic_name in existing_names:
                topic_name = manager.generate_topic_name(directory, existing_names, override_name=None)
                if topic_name in existing_names:
                    # Fallback: append session snippet or counter
                    n = 2
                    base = topic_name
                    while topic_name in existing_names:
                        topic_name = f"{base} ({n})"
                        n += 1

            await query.edit_message_text(
                f"Creating topic <b>{escape_html(topic_name)}</b>...",
                parse_mode="HTML",
            )

            try:
                mapping = await manager.create_topic(
                    context.bot, chat_id, user_id, directory, topic_name,
                    session_id=session_id,
                )
                thread_id = mapping.message_thread_id

                # Eagerly connect
                client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
                if client_manager:
                    client = await client_manager.get_or_connect(
                        user_id=user_id,
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        directory=directory,
                        session_id=session_id,
                        force_new=(session_id is None),
                        approved_directory=str(self.settings.approved_directories[0]),
                    )
                    # For new sessions, rename topic with session snippet
                    if session_id is None and client.session_id:
                        new_name = f"{dir_name} — {client.session_id[:8]}"
                        lifecycle: Optional[TopicLifecycleManager] = context.bot_data.get("lifecycle_manager")
                        if lifecycle:
                            await lifecycle.rename_topic(context.bot, chat_id, thread_id, new_name)
                        # Update DB
                        await manager.repository.upsert(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                            user_id=user_id,
                            directory=directory,
                            topic_name=new_name,
                            session_id=client.session_id,
                        )

                # Set auto-naming flag for this topic's chat_data
                # (chat_data is per-chat in PTB, but for topics we'd need per-thread state)
                # We'll track naming state in the DB topic_name field or user_data

                await query.message.reply_text(
                    f"Topic <b>{escape_html(topic_name)}</b> created "
                    f"→ <code>{escape_html(directory)}</code>",
                    parse_mode="HTML",
                )

            except Exception as e:
                logger.error("start_wizard_create_failed", error=str(e))
                await query.message.reply_text(f"Failed to create topic: {str(e)[:200]}")

            # Cleanup wizard state
            context.user_data.pop("start_wizard_dir", None)
            context.user_data.pop("start_browse_root", None)
            context.user_data.pop("start_browse_rel", None)
            return
```

**Step 3: Remove old add_nav: and add_sel: callback handlers**

Delete the `add_nav:` handler block (around `if prefix == "add_nav":`) and the `add_sel:` handler block (around `if prefix == "add_sel":`). These are fully replaced by `start_nav:` and `start_sel:`.

**Step 4: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: /start wizard session picker + topic creation"
```

---

### Task 6: Update /new and /resume for 1:1 topic-session model

**Files:**
- Modify: `src/bot/orchestrator.py`

**Context:** `/new` inside a supergroup topic should create a new topic (not reset in-place). `/resume` should be removed from topics (session is fixed per topic).

**Step 1: Update `agentic_new`**

In the `agentic_new` method, add supergroup-topic detection at the start. If in a supergroup topic (not General, not DM), create a new topic for the same directory instead of resetting the session:

After `chat_id, message_thread_id = self._resolve_chat_key(update, context)`, add:

```python
        # In supergroup topic: create new topic for same dir (preserves 1:1 model)
        chat = update.effective_chat
        if chat and chat.type == "supergroup" and message_thread_id != 0:
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directories[0]
            )
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                dir_name = Path(str(current_dir)).name
                existing = await manager.list_topics(chat.id)
                existing_names = [t.topic_name for t in existing]
                topic_name = manager.generate_topic_name(str(current_dir), existing_names)

                try:
                    mapping = await manager.create_topic(
                        context.bot, chat.id, user_id, str(current_dir), topic_name
                    )
                    new_thread_id = mapping.message_thread_id

                    # Eagerly connect new session
                    client_manager_new: Optional[ClientManager] = context.bot_data.get("client_manager")
                    if client_manager_new:
                        client = await client_manager_new.get_or_connect(
                            user_id=user_id,
                            chat_id=chat.id,
                            message_thread_id=new_thread_id,
                            directory=str(current_dir),
                            session_id=None,
                            force_new=True,
                            approved_directory=str(self.settings.approved_directories[0]),
                        )
                        # Rename with session snippet
                        if client.session_id:
                            new_name = f"{dir_name} — {client.session_id[:8]}"
                            lifecycle_new: Optional[TopicLifecycleManager] = context.bot_data.get("lifecycle_manager")
                            if lifecycle_new:
                                await lifecycle_new.rename_topic(context.bot, chat.id, new_thread_id, new_name)

                    await update.message.reply_text(
                        f"New session started in topic <b>{escape_html(topic_name)}</b>.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error("new_topic_creation_failed", error=str(e))
                    await update.message.reply_text(f"Failed to create new topic: {str(e)[:200]}")
                return
```

**Step 2: Update `agentic_resume` to reject in topics**

At the start of `agentic_resume`, add:

```python
        # In supergroup topic: session is fixed (1:1 model)
        chat = update.effective_chat
        if chat and chat.type == "supergroup":
            thread_ctx = context.user_data.get("_thread_context")
            if thread_ctx and thread_ctx.get("message_thread_id", 0) != 0:
                await update.message.reply_text(
                    "Session is fixed per topic. Use /new to start a new topic, "
                    "or /start in General to resume a different session.",
                    parse_mode="HTML",
                )
                return
```

**Step 3: Update `get_bot_commands()` group commands**

Remove `/resume` and `/add` from `group_commands`. Update `/start` description:

```python
        group_commands = [
            BotCommand("start", "New project topic (pick dir + session)"),
            BotCommand("new", "Start a fresh session (new topic)"),
            BotCommand("interrupt", "Interrupt running query"),
            BotCommand("status", "Show all sessions / topic status"),
            BotCommand("compact", "Compress context"),
            BotCommand("model", "Switch Claude model"),
            BotCommand("commands", "Browse available skills"),
            BotCommand("remove", "Remove this project topic"),
            BotCommand("history", "Show session transcript"),
        ]
```

**Step 4: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass (update test counts if tests reference old command lists)

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: /new creates topic in supergroups, /resume rejected in topics"
```

---

### Task 7: Auto-naming after 3 messages

**Files:**
- Modify: `src/bot/orchestrator.py`

**Context:** After a successful Claude response, if the topic hasn't been named and 3+ user messages have been exchanged, call Haiku to generate a name and rename the topic.

**Step 1: Add auto-naming logic in `_execute_query`**

After the successful response block (after `context.user_data["claude_session_id"] = claude_response.session_id` around line 957), add:

```python
            # Auto-naming: rename topic after 3 messages if not yet named
            if message_thread_id != 0 and not context.chat_data.get("_topic_named"):
                msg_count = context.chat_data.get("_msg_count", 0) + 1
                context.chat_data["_msg_count"] = msg_count

                if msg_count >= 3:
                    context.chat_data["_topic_named"] = True  # prevent retries
                    try:
                        from src.projects.topic_namer import generate_topic_name as gen_name

                        # Collect recent messages from this conversation
                        recent = context.chat_data.get("_recent_messages", [])
                        recent.append((query.text or "")[:200])
                        if claude_response.content:
                            recent.append(claude_response.content[:200])

                        dir_name = Path(str(current_dir)).name
                        haiku_name = await gen_name(recent, dir_name)

                        if haiku_name:
                            lifecycle_auto: Optional[TopicLifecycleManager] = context.bot_data.get("lifecycle_manager")
                            if lifecycle_auto:
                                await lifecycle_auto.rename_topic(
                                    context.bot, chat_id, message_thread_id, haiku_name
                                )
                                # Update DB
                                manager_auto = context.bot_data.get("project_threads_manager")
                                if manager_auto:
                                    await manager_auto.repository.upsert(
                                        chat_id=chat_id,
                                        message_thread_id=message_thread_id,
                                        user_id=user_id,
                                        directory=str(current_dir),
                                        topic_name=haiku_name,
                                    )
                                logger.info(
                                    "topic_auto_named",
                                    chat_id=chat_id,
                                    message_thread_id=message_thread_id,
                                    name=haiku_name,
                                )
                    except Exception as e:
                        logger.debug("auto_naming_failed", error=str(e))
                else:
                    # Track messages for naming context
                    recent = context.chat_data.get("_recent_messages", [])
                    recent.append((query.text or "")[:200])
                    if claude_response.content:
                        recent.append(claude_response.content[:200])
                    context.chat_data["_recent_messages"] = recent[-12:]  # keep last 12
```

**Step 2: Run tests**

Run: `cd /local/home/moxu/claude-coder/.claude/worktrees/multi-project && uv run pytest tests/ -x --tb=short -q`
Expected: All pass

**Step 3: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: auto-name topics via Haiku after 3 messages"
```

---

### Task 8: Update documentation and clean up

**Files:**
- Modify: `CLAUDE.md`
- Modify: `src/bot/orchestrator.py` (minor: remove `/resume` from `registered_commands` set for topics)

**Step 1: Update CLAUDE.md**

In the "Agentic mode commands" section, update:
- Remove `/add`
- Update `/start` description: "In DM: welcome. In supergroup General: wizard (pick dir → session → creates topic)."
- Update `/new` description: "In DM: fresh session. In topic: creates new topic."
- Note `/resume` only available in DM and General topic
- Add `/history` if not already listed
- Update architecture section to mention `topic_namer.py`

In the "Multi-project concurrent sessions" section, update:
- "Use `/start` in the General topic to create a project topic (wizard: pick dir → pick session → topic created)"
- "Each topic is bound to exactly one session (1:1). Same directory can have multiple topics for different sessions."
- "Topics are auto-named by Haiku after ~3 exchanges"

**Step 2: Commit**

```bash
git add CLAUDE.md src/bot/orchestrator.py
git commit -m "docs: update CLAUDE.md for /start wizard, topic=session 1:1, auto-naming"
```
