"""Message orchestrator — single entry point for all Telegram updates.

Provides a conversational interface with commands, text/file handling,
and inline keyboards.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import structlog
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from ..claude.sdk_integration import ClaudeResponse
    from .attachments import Query

from ..claude.client_manager import ClientManager
from ..claude.history import (
    append_history_entry,
    check_history_format_health,
    filter_by_directory,
    read_claude_history,
    read_first_message,
    read_session_transcript,
)
from ..claude.user_client import QueryInterruptedError
from ..config.settings import Settings
from ..projects.lifecycle import TopicLifecycleManager
from .progress import ProgressMessageManager, build_stream_callback
from .utils.html_format import escape_html
from .utils.repo_browser import (
    build_browse_header,
    build_browser_keyboard,
    is_branch_dir,
    list_visible_children,
    resolve_browse_path,
)
from .utils.time_format import relative_time

logger = structlog.get_logger()


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        from .attachments import MediaGroupCollector

        self._media_collector = MediaGroupCollector()

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_management_bypass = handler.__name__ in {"sync_threads", "handle_remove"}
            is_start_bypass = handler.__name__ in {"start_command", "handle_start"}
            is_general_allowed = handler.__name__ in {
                "start_command",
                "handle_start",
                "handle_status",
                "_handle_callback",
            }
            message_thread_id = self._extract_message_thread_id(update)

            chat = update.effective_chat
            is_supergroup = chat is not None and chat.type == "supergroup"

            if is_supergroup:
                # Supergroup with topics — enforce thread routing
                in_general = not message_thread_id

                # Block commands not allowed in General topic
                if in_general and not is_management_bypass and not is_general_allowed:
                    if update.effective_message:
                        await update.effective_message.reply_text(
                            "Use this command inside a project topic."
                        )
                    return

                should_enforce = not is_management_bypass and not (
                    is_start_bypass and in_general
                )
                if should_enforce:
                    allowed = await self._apply_thread_routing_context(update, context)
                    if not allowed:
                        return
                elif in_general:
                    # Bypassed handlers still need the General flag
                    context.user_data["_in_general_topic"] = True
                elif message_thread_id:
                    # Bypassed handler in a project topic — set context
                    # without enforcement so /remove etc. can read it
                    directory = ""
                    manager = context.bot_data.get("project_threads_manager")
                    if manager:
                        directory = (
                            await manager.resolve_directory(chat.id, message_thread_id)
                            or ""
                        )
                    context.user_data["_thread_context"] = {
                        "chat_id": chat.id,
                        "message_thread_id": message_thread_id,
                        "directory": directory,
                    }
            else:
                # Private DM — set thread context for private chat
                user_id = update.effective_user.id if update.effective_user else 0
                current_dir = context.user_data.get(
                    "current_directory", self.settings.approved_directories[0]
                )
                context.user_data["_thread_context"] = {
                    "chat_id": user_id,
                    "message_thread_id": 0,
                    "directory": str(current_dir),
                }

            await handler(update, context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce project-thread routing and load thread-local directory state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            return True  # No manager = no thread routing

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        message_thread_id = self._extract_message_thread_id(update)

        # General topic (no thread_id) — allow /add, /start, /status through
        if not message_thread_id:
            context.user_data["_in_general_topic"] = True
            return True

        directory = await manager.resolve_directory(chat.id, message_thread_id)
        if not directory:
            await self._reject_for_thread_mode(
                update, "This topic is not bound to a project. Use /add in General."
            )
            return False

        context.user_data["current_directory"] = Path(directory)
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "directory": directory,
        }
        context.user_data.pop("_in_general_topic", None)
        return True

    @staticmethod
    async def _post_to_topic(
        update: Update,
        text: str,
        message_thread_id: int = 0,
        parse_mode: Optional[str] = None,
        reply_markup: Any = None,
    ) -> Any:
        """Send a message: post to topic in supergroups, reply in DMs."""
        chat = update.effective_chat
        if message_thread_id and chat and chat.type == "supergroup":
            return await chat.send_message(
                text,
                message_thread_id=message_thread_id,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        return await update.message.reply_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        return None

    def _resolve_chat_key(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> tuple[int, int]:
        """Return (chat_id, message_thread_id) for the current context."""
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            return thread_ctx["chat_id"], thread_ctx["message_thread_id"]
        # Private DM fallback
        return update.effective_user.id, 0

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register all handlers."""
        self._register_handlers(app)

    def _register_handlers(self, app: Application) -> None:
        """Register handlers: commands + text/file/photo."""
        # Commands
        handlers = [
            ("start", self.handle_start),
            ("new", self.handle_new),
            ("interrupt", self.handle_interrupt),
            ("status", self.handle_status),
            ("compact", self.handle_compact),
            ("model", self.handle_model),
            ("repo", self.handle_repo),
            ("resume", self.handle_resume),
            ("commands", self.handle_commands),
            ("remove", self.handle_remove),
            ("history", self.handle_history),
            ("restart", self.handle_restart),
        ]

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Unrecognized /commands -> skill lookup + Claude fallback.
        # Registered AFTER CommandHandlers in group 0 so it only runs
        # when no CommandHandler matched (within a group, first match wins).
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self.handle_text),
            ),
        )

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.handle_text),
            ),
            group=10,
        )

        # File and photo uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.ALL,
                self._inject_deps(self.handle_attachment),
            ),
            group=10,
        )

        # Callbacks for cd:, session:, skill:, and model: patterns
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_callback),
                pattern=r"^(cd:|nav:|sel:|start_nav:|start_sel:|start_ses:|session:|skill:|model:|remove_confirm:|remove_cancel)",
            )
        )

        logger.info("Handlers registered")

    async def get_bot_commands(self) -> Any:
        """Return bot commands. Dict of scope->commands for private and group contexts."""
        private_commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("new", "Start a fresh session"),
            BotCommand("interrupt", "Interrupt running query"),
            BotCommand("status", "Show session status"),
            BotCommand("compact", "Compress context"),
            BotCommand("model", "Switch Claude model"),
            BotCommand("repo", "List repos / switch workspace"),
            BotCommand("resume", "Choose a session to resume"),
            BotCommand("commands", "Browse available skills"),
            BotCommand("history", "Show session transcript"),
        ]

        group_commands = [
            BotCommand("start", "Create a project topic"),
            BotCommand("new", "New topic for same project"),
            BotCommand("interrupt", "Interrupt running query"),
            BotCommand("status", "Show active sessions"),
            BotCommand("compact", "Compress context"),
            BotCommand("model", "Switch Claude model"),
            BotCommand("commands", "Browse available skills"),
            BotCommand("history", "Show session transcript"),
            BotCommand("remove", "Delete this topic"),
        ]

        return {"private": private_commands, "group": group_commands}

    # --- Handlers ---

    async def handle_interrupt(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Interrupt the currently running Claude query."""
        if not update.effective_user or not update.message:
            return
        user_id = update.effective_user.id
        chat_id, message_thread_id = self._resolve_chat_key(update, context)
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            client = client_manager.get_active_client(
                user_id, chat_id, message_thread_id
            )
            if client and client.is_connected:
                await client_manager.interrupt(user_id, chat_id, message_thread_id)
                await update.message.reply_text("Interrupting current query...")
                return
        await update.message.reply_text("No active query to interrupt.")

    async def handle_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show model selection keyboard."""
        if not update.message:
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Sonnet", callback_data="model:sonnet"),
                    InlineKeyboardButton("Opus", callback_data="model:opus"),
                    InlineKeyboardButton("Haiku", callback_data="model:haiku"),
                ],
                [
                    InlineKeyboardButton("Sonnet 1M", callback_data="model:sonnet:1m"),
                    InlineKeyboardButton("Opus 1M", callback_data="model:opus:1m"),
                ],
            ]
        )
        await update.message.reply_text("Select a model:", reply_markup=keyboard)

    async def handle_model_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle model selection callback.

        Note: query.answer() is already called by _handle_callback.
        """
        query = update.callback_query
        if not query or not query.data:
            return
        data = query.data  # e.g. "model:sonnet" or "model:opus:1m"
        parts = data.split(":")
        # parts[0] = "model", parts[1] = model name, parts[2] = optional "1m"
        if len(parts) < 2:
            return
        base_model = parts[1]
        is_1m = len(parts) > 2 and parts[2] == "1m"
        # Encode 1M in the model name (e.g. "opus[1m]") so the CLI
        # handles extended context natively — works for both Bedrock
        # and direct API.  The separate --betas flag is API-key-only.
        model = f"{base_model}[1m]" if is_1m else base_model

        # Build display label
        label_map = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
        label = label_map.get(base_model, base_model)
        if is_1m:
            label += " 1M"

        user_id = query.from_user.id
        chat_id, message_thread_id = self._resolve_chat_key(update, context)
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            await client_manager.set_model(
                user_id, chat_id, message_thread_id, model
            )
            await query.edit_message_text(
                f"Model switched to {label}. Active on your next message."
            )
        else:
            await query.edit_message_text("Model switching is not available.")

    async def handle_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Start: welcome in DM/topic, wizard in supergroup General."""
        user = update.effective_user
        chat = update.effective_chat

        # Supergroup General topic — show directory browser (wizard step 1)
        is_supergroup = chat is not None and chat.type == "supergroup"
        message_thread_id = self._extract_message_thread_id(update)
        in_general = is_supergroup and not message_thread_id

        if in_general:
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
        keyboard_rows = build_browser_keyboard(
            browse_dir, browse_dir, multi_root=multi_root
        )

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
        # Dedupe by session_id — keep most recent entry per session
        seen_ids: set[str] = set()
        unique_entries: list = []
        for entry in sorted_entries:
            if entry.session_id not in seen_ids:
                seen_ids.add(entry.session_id)
                unique_entries.append(entry)
        sorted_entries = unique_entries

        keyboard_rows: List[list] = []

        if sorted_entries:
            for entry in sorted_entries[:8]:
                time_str = relative_time(entry.timestamp)
                first_msg = read_first_message(
                    session_id=entry.session_id,
                    project_dir=entry.project,
                )
                display_name = (first_msg or entry.display or entry.session_id[:12])[
                    :40
                ]
                button_label = f"{time_str} — {display_name}"
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            button_label,
                            callback_data=f"start_ses:{entry.session_id}",
                        )
                    ]
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

    async def handle_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Start a fresh SDK session in the current directory."""
        user_id = update.effective_user.id
        chat_id, message_thread_id = self._resolve_chat_key(update, context)

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
                topic_name = manager.generate_topic_name(
                    str(current_dir), existing_names
                )

                try:
                    mapping = await manager.create_topic(
                        context.bot, chat.id, user_id, str(current_dir), topic_name
                    )
                    new_thread_id = mapping.message_thread_id

                    # Eagerly connect new session
                    client_manager_new: Optional[ClientManager] = context.bot_data.get(
                        "client_manager"
                    )
                    if client_manager_new:
                        client = await client_manager_new.get_or_connect(
                            user_id=user_id,
                            chat_id=chat.id,
                            message_thread_id=new_thread_id,
                            directory=str(current_dir),
                            session_id=None,
                            force_new=True,
                            approved_directory=str(
                                self.settings.approved_directories[0]
                            ),
                        )
                        # Rename with session snippet
                        if client.session_id:
                            new_name = f"{dir_name} — {client.session_id[:8]}"
                            lifecycle_new: Optional[TopicLifecycleManager] = (
                                context.bot_data.get("lifecycle_manager")
                            )
                            if lifecycle_new:
                                await lifecycle_new.rename_topic(
                                    context.bot, chat.id, new_thread_id, new_name
                                )

                    await update.message.reply_text(
                        f"New session started in topic <b>{escape_html(topic_name)}</b>.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error("new_topic_creation_failed", error=str(e))
                    await update.message.reply_text(
                        f"Failed to create new topic: {str(e)[:200]}"
                    )
                return

        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directories[0]
        )

        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            await client_manager.disconnect(user_id, chat_id, message_thread_id)

        # Clear persisted session so cold-start won't restore it
        storage = context.bot_data.get("storage")
        if storage:
            await storage.clear_session(chat_id, message_thread_id)

        if client_manager:
            try:
                client = await client_manager.get_or_connect(
                    user_id=user_id,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    directory=str(current_dir),
                    session_id=None,
                    force_new=True,
                    approved_directory=str(self.settings.approved_directories[0]),
                )
                context.user_data["claude_session_id"] = client.session_id
                context.user_data["force_new_session"] = False

                dir_name = (
                    current_dir.name
                    if hasattr(current_dir, "name")
                    else str(current_dir).rsplit("/", 1)[-1]
                )
                await update.message.reply_text(
                    f"New session in <code>{escape_html(dir_name)}/</code>. Ready.",
                    parse_mode="HTML",
                )
                return
            except Exception:
                logger.debug("new_session_eager_connect_failed", user_id=user_id)

        # Fallback: lazy connect on next message
        await update.message.reply_text(
            "Session reset. Will connect on your next message.",
        )

    async def handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show current session status with workspace and session info."""
        if not update.effective_user or not update.message:
            return
        user_id = update.effective_user.id

        # General topic dashboard: show all active sessions across projects
        if context.user_data.get("_in_general_topic"):
            client_manager: Optional[ClientManager] = context.bot_data.get(
                "client_manager"
            )
            if client_manager:
                clients = client_manager.get_all_clients_for_user(user_id)
                if clients:
                    lines = ["<b>Active Sessions</b>\n"]
                    for _chat_id, _thread_id, client in clients:
                        name = escape_html(Path(client.directory).name)
                        if client.is_querying:
                            state = "querying"
                        elif client.is_connected:
                            state = "idle"
                        else:
                            state = "disconnected"
                        lines.append(f"<b>{name}</b> — {state}")
                    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
                    return
            await update.message.reply_text("No active sessions.")
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))

        # Determine which workspace root contains current_dir
        workspace_root = None
        approved_dirs = self.settings.approved_directories
        for root in approved_dirs:
            try:
                current_dir.relative_to(root)
                workspace_root = root
                break
            except ValueError:
                continue

        # Build workspace display
        if workspace_root and len(approved_dirs) > 1:
            # Multi-root: show which root we're in
            workspace_name = workspace_root.name
            workspace_line = f"<b>Workspace:</b> {workspace_name}\n"
        else:
            workspace_line = ""

        dir_display = escape_html(str(current_dir))

        # Session info
        session_id = context.user_data.get("claude_session_id")
        if session_id:
            # Try to get display name from history.jsonl
            display_name = ""
            try:
                history_entries = read_claude_history()
                for entry in history_entries:
                    if entry.session_id == session_id:
                        display_name = entry.display
                        break
            except Exception:
                pass

            if display_name:
                session_line = f"<b>Session:</b> {escape_html(display_name[:50])}\n"
            else:
                session_line = f"<b>Session:</b> {session_id[:12]}...\n"

            # Count available sessions for this directory
            try:
                dir_entries = filter_by_directory(read_claude_history(), current_dir)
                session_count = len(dir_entries)
            except Exception:
                session_count = 0

            if session_count > 1:
                session_line += f"({session_count} sessions available)\n"
        else:
            session_line = "<b>Session:</b> none (send a message to start)\n"

        # ClientManager info (model, connection state)
        client_line = ""
        chat_id, message_thread_id = self._resolve_chat_key(update, context)
        client_manager = context.bot_data.get("client_manager")
        if client_manager:
            active_client = client_manager.get_active_client(
                update.effective_user.id, chat_id, message_thread_id
            )
            if active_client:
                model_name = active_client.model or "default"
                if active_client.is_querying:
                    state = "querying"
                elif active_client.is_connected:
                    state = "connected"
                else:
                    state = "disconnected"
                client_line = (
                    f"<b>Model:</b> {escape_html(model_name)}\n"
                    f"<b>State:</b> {state}\n"
                )
                # Use active client's session_id if context doesn't have one
                if not session_id and active_client.session_id:
                    session_id = active_client.session_id
                    session_line = f"<b>Session:</b> {session_id[:12]}...\n"

        await update.message.reply_text(
            f"{workspace_line}"
            f"<b>Directory:</b> <code>{dir_display}</code>\n"
            f"{session_line}"
            f"{client_line}",
            parse_mode="HTML",
        )

    async def handle_compact(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compress conversation context while keeping session continuity."""
        user_id = update.effective_user.id
        session_id = context.user_data.get("claude_session_id")

        # Check for active session
        if not session_id:
            await update.message.reply_text(
                "No active session to compact. Start a conversation first."
            )
            return

        logger.info(
            "Compacting session context",
            user_id=user_id,
            session_id=session_id,
        )

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directories[0]
        )

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Compacting context...")

        # Start typing heartbeat
        heartbeat = self._start_typing_heartbeat(chat)

        try:
            # Step 1: Ask Claude to summarize the conversation
            summary_prompt = (
                "Summarize our conversation so far concisely. Include: "
                "key decisions, current state of work, pending tasks, "
                "and important context. Format as bullet points."
            )

            logger.info("Requesting conversation summary", user_id=user_id)

            from .attachments import Query

            summary_response = await self._run_claude_query(
                query=Query(text=summary_prompt),
                user_id=user_id,
                current_dir=current_dir,
                session_id=session_id,
                force_new=False,
                on_stream=None,
                context=context,
            )

            summary_text = summary_response.content.strip()

            # Step 2: Start new session seeded with the summary
            reseed_prompt = (
                f"This is a compacted session. Here is the context from our "
                f"previous conversation:\n\n{summary_text}\n\n"
                f"Please acknowledge briefly. We're continuing our work."
            )

            logger.info("Creating new session with summary", user_id=user_id)

            new_response = await self._run_claude_query(
                query=Query(text=reseed_prompt),
                user_id=user_id,
                current_dir=current_dir,
                session_id=None,
                force_new=True,
                on_stream=None,
                context=context,
            )

            # Update session ID to the new one
            context.user_data["claude_session_id"] = new_response.session_id

            logger.info(
                "Session compacted successfully",
                user_id=user_id,
                old_session_id=session_id,
                new_session_id=new_response.session_id,
            )

            await progress_msg.delete()
            await update.message.reply_text(
                "Context compacted. Session continues with summary.",
                reply_to_message_id=update.message.message_id,
            )

        except Exception as e:
            logger.error(
                "Failed to compact session",
                error=str(e),
                user_id=user_id,
                session_id=session_id,
            )
            await progress_msg.delete()
            await update.message.reply_text(
                f"Failed to compact context: {str(e)[:200]}",
                reply_to_message_id=update.message.message_id,
            )
        finally:
            heartbeat.cancel()

    async def _run_claude_query(
        self,
        query: "Query",
        user_id: int,
        current_dir: Any,
        session_id: Optional[str],
        force_new: bool,
        on_stream: Optional[Callable[..., Any]],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> "ClaudeResponse":
        """Run a query via ClientManager, streaming events through on_stream.

        Returns a ClaudeResponse for compatibility with existing formatting code.
        """
        from ..claude.sdk_integration import ClaudeResponse  # noqa: F811

        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager is None:
            raise RuntimeError("client_manager not available")

        directory = str(current_dir)
        approved_dir = str(self.settings.approved_directory)

        # Resolve chat key from context — _run_claude_query needs the context
        # parameter that callers already pass through.
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            chat_id = thread_ctx["chat_id"]
            message_thread_id = thread_ctx["message_thread_id"]
        else:
            chat_id = user_id
            message_thread_id = 0

        client = await client_manager.get_or_connect(
            user_id=user_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            directory=directory,
            session_id=None if force_new else session_id,
            approved_directory=approved_dir,
            force_new=force_new,
        )

        result = await client.submit(query, on_stream=on_stream)

        if result.session_id:
            await client_manager.update_session_id(
                user_id, chat_id, message_thread_id, directory, result.session_id
            )

        return ClaudeResponse(
            content=result.response_text,
            session_id=result.session_id or "",
            cost=result.cost,
            duration_ms=result.duration_ms,
            num_turns=result.num_turns,
        )

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    async def _execute_query(
        self,
        query: "Query",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        """Shared query execution: progress, session, Claude call, response.

        Handles steps common to both handle_text and handle_attachment:
        client sync, directory restore, progress message, heartbeat,
        _run_claude_query, session/history update, response formatting,
        error handling, and reply delivery.

        Returns True on success, False on error.
        """
        from .handlers.message import (
            _format_error_message,
            _update_working_directory_from_claude_response,
        )
        from .utils.formatting import FormattedMessage, ResponseFormatter

        user_id = update.effective_user.id
        chat_id, message_thread_id = self._resolve_chat_key(update, context)

        # Resolve session for THIS topic.  context.user_data is per-user
        # (not per-topic), so we must always resolve from the active client
        # or the DB — never rely on a stale value left by a different topic.
        _cm: Optional[ClientManager] = context.bot_data.get("client_manager")
        _active = (
            _cm.get_active_client(user_id, chat_id, message_thread_id)
            if _cm
            else None
        )
        if _active and _active.is_connected:
            context.user_data["current_directory"] = Path(_active.directory)
            context.user_data["claude_session_id"] = _active.session_id
        else:
            # No active client for this topic — load from DB
            storage = context.bot_data.get("storage")
            if storage:
                session = await storage.load_session(chat_id, message_thread_id)
                if session and session.session_id:
                    context.user_data["claude_session_id"] = session.session_id
                else:
                    context.user_data["claude_session_id"] = None

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directories[0]
        )
        session_id = context.user_data.get("claude_session_id")

        # Reopen topic if it was closed (idle timeout)
        if message_thread_id != 0:
            lifecycle: Optional[TopicLifecycleManager] = context.bot_data.get(
                "lifecycle_manager"
            )
            if lifecycle:
                await lifecycle.reopen(context.bot, chat_id, message_thread_id)

        chat = update.message.chat
        await chat.send_action("typing")

        progress_msg = await self._post_to_topic(
            update, "Working...", message_thread_id=message_thread_id
        )
        start_time = time.time()
        progress_manager = ProgressMessageManager(
            initial_message=progress_msg, start_time=start_time
        )
        on_stream = build_stream_callback(progress_manager)

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await self._run_claude_query(
                query=query,
                user_id=user_id,
                current_dir=current_dir,
                session_id=session_id,
                force_new=force_new,
                on_stream=on_stream,
                context=context,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            previous_session_id = context.user_data.get("claude_session_id")
            context.user_data["claude_session_id"] = claude_response.session_id

            # Write to CLI history.jsonl so CLI /resume can discover bot sessions
            if claude_response.session_id != previous_session_id:
                current_dir = context.user_data.get(
                    "current_directory",
                    self.settings.approved_directories[0],
                )
                display_preview = (query.text or "")[:80]
                append_history_entry(
                    session_id=claude_response.session_id,
                    display=display_preview,
                    project=str(current_dir),
                )

            # Track directory changes
            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Auto-naming disabled — topics use dir_name — session_id[:8] format

            # Format response (no reply_markup — strip keyboards)
            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except QueryInterruptedError:
            success = True  # Not a failure — user-initiated cancellation
            logger.info("query_interrupted_by_user", user_id=user_id)
            formatted_messages = [
                FormattedMessage("Query interrupted.", parse_mode=None)
            ]
        except Exception as e:
            success = False
            logger.error("Claude query failed", error=str(e), user_id=user_id)
            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()

        await progress_manager.finalize()

        for i, message in enumerate(formatted_messages):
            if not message.text or not message.text.strip():
                continue
            try:
                await self._post_to_topic(
                    update,
                    message.text,
                    message_thread_id=message_thread_id,
                    parse_mode=message.parse_mode,
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)
            except Exception as send_err:
                logger.warning(
                    "Failed to send HTML response, retrying as plain text",
                    error=str(send_err),
                    message_index=i,
                )
                try:
                    await self._post_to_topic(
                        update,
                        message.text,
                        message_thread_id=message_thread_id,
                    )
                except Exception as plain_err:
                    await self._post_to_topic(
                        update,
                        f"Failed to deliver response "
                        f"(Telegram error: {str(plain_err)[:150]}). "
                        f"Please try again.",
                        message_thread_id=message_thread_id,
                    )

        return success

    async def handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        from .attachments import Query

        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Clear pending /remove confirmation on any non-command message
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            tid = thread_ctx.get("message_thread_id", 0)
            context.chat_data.pop(f"_pending_remove_{tid}", None)

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
                    "compact",
                    "model",
                    "repo",
                    "resume",
                    "commands",
                    "remove",
                    "history",
                    "restart",
                }

                if potential_skill_name not in registered_commands:
                    # Check cached commands from SDK — if found, log and
                    # pass verbatim. The CLI handles body loading, placeholder
                    # resolution, and prompt injection natively.
                    _cm_check: Optional[ClientManager] = context.bot_data.get(
                        "client_manager"
                    )
                    _chat_id_check, _thread_id_check = self._resolve_chat_key(
                        update, context
                    )
                    _active = (
                        _cm_check.get_active_client(
                            user_id, _chat_id_check, _thread_id_check
                        )
                        if _cm_check
                        else None
                    )
                    if _active and _active.has_command(potential_skill_name):
                        logger.info(
                            "skill_passthrough",
                            skill_name=potential_skill_name,
                            user_id=user_id,
                        )
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

        success = await self._execute_query(Query(text=message_text), update, context)

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def handle_attachment(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo or document upload -> Claude via Query pipeline."""
        from .attachments import AttachmentProcessor, Query, UnsupportedAttachmentError

        user_id = update.effective_user.id

        # Use MediaGroupCollector to buffer album items
        updates = await self._media_collector.add(update)
        if updates is None:
            # Still buffering — more items may arrive
            return

        logger.info(
            "handle_attachment",
            user_id=user_id,
            num_items=len(updates),
        )

        # Process each message into an Attachment
        processor = AttachmentProcessor()
        attachments = []
        caption: Optional[str] = None
        for u in updates:
            msg = u.message
            if msg is None:
                continue
            if caption is None:
                caption = msg.caption
            try:
                att = await processor.process(msg)
                attachments.append(att)
            except UnsupportedAttachmentError as exc:
                await update.message.reply_text(str(exc))
                return

        if not attachments:
            await update.message.reply_text("No supported attachments found.")
            return

        query = Query(
            text=caption or "Analyze this.",
            attachments=tuple(attachments),
        )

        success = await self._execute_query(query, update, context)

        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="attachment",
                args=[f"{len(attachments)} file(s)"],
                success=success,
            )

    async def handle_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Navigable directory browser.

        /repo          — browse current directory (or workspace root)
        /repo <path>   — navigate to path (multi-level supported)
        """
        args = update.message.text.split()[1:] if update.message.text else []
        roots = self.settings.approved_directories
        storage = context.bot_data.get("storage")

        # Determine current browse location
        browse_root = context.user_data.get("repo_browse_root")
        browse_rel = context.user_data.get("repo_browse_rel", "")

        if not browse_root or browse_root not in roots:
            browse_root = roots[0]
            browse_rel = ""

        if args:
            # /repo <path> — resolve multi-level path
            target_name = " ".join(args)
            target_path = resolve_browse_path(target_name, roots)

            if not target_path:
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            # Find which root this path is under
            target_root = next(
                (r for r in roots if target_path == r or target_path.is_relative_to(r)),
                None,
            )
            if not target_root:
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            if is_branch_dir(target_path):
                # Navigate into it — show browser
                context.user_data["repo_browse_root"] = target_root
                context.user_data["repo_browse_rel"] = (
                    str(target_path.relative_to(target_root))
                    if target_path != target_root
                    else ""
                )
                await self._send_repo_browser(
                    update.message, target_path, target_root, roots, context
                )
            else:
                # Leaf — select it
                _sel_chat_id, _sel_thread_id = self._resolve_chat_key(update, context)
                await self._select_directory(
                    update.message,
                    target_path,
                    storage,
                    context,
                    user_id=update.effective_user.id,
                    chat_id=_sel_chat_id,
                    message_thread_id=_sel_thread_id,
                )
            return

        # No args — show browser at current browse location
        browse_dir = browse_root / browse_rel if browse_rel else browse_root
        if not browse_dir.is_dir():
            browse_dir = browse_root
            browse_rel = ""
            context.user_data["repo_browse_rel"] = ""

        await self._send_repo_browser(
            update.message, browse_dir, browse_root, roots, context
        )

    async def _send_repo_browser(
        self,
        message: Any,
        browse_dir: Path,
        workspace_root: Path,
        roots: list,
        context: ContextTypes.DEFAULT_TYPE,
        edit: bool = False,
    ) -> None:
        """Render the directory browser for browse_dir."""
        header = build_browse_header(browse_dir, workspace_root)
        children = list_visible_children(browse_dir)

        # Build file listing text
        lines = [header, ""]
        for child in children:
            is_git = (child / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            branch_marker = " \u25b6" if is_branch_dir(child) else ""
            lines.append(
                f"{icon} <code>{escape_html(child.name)}/</code>{branch_marker}"
            )

        if not children:
            lines.append("<i>No subdirectories</i>")

        keyboard = build_browser_keyboard(
            browse_dir=browse_dir,
            workspace_root=workspace_root,
            multi_root=len(roots) > 1,
        )
        markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        text = "\n".join(lines)

        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _select_directory(
        self,
        message: Any,
        target_path: Path,
        storage: Any,
        context: ContextTypes.DEFAULT_TYPE,
        edit: bool = False,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
    ) -> None:
        """Select a directory: set as working dir, disconnect active session."""
        context.user_data["current_directory"] = target_path

        # Clear session — user must /new or /resume explicitly.
        context.user_data["claude_session_id"] = None
        context.user_data["force_new_session"] = False

        # Disconnect active SDK session for the current chat key
        client_manager = context.bot_data.get("client_manager")
        if (
            client_manager
            and user_id
            and chat_id is not None
            and message_thread_id is not None
        ):
            await client_manager.disconnect(user_id, chat_id, message_thread_id)

        is_git = (target_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""

        text = (
            f"Switched to <code>{escape_html(target_path.name)}/</code>" f"{git_badge}"
        )

        if edit:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.reply_text(text, parse_mode="HTML")

    async def handle_commands(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show available skills as inline keyboard buttons."""
        user_id = update.effective_user.id
        chat_id, message_thread_id = self._resolve_chat_key(update, context)
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")

        commands: list[dict] = []
        if client_manager:
            commands = client_manager.get_available_commands(
                user_id, chat_id, message_thread_id
            )

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

        # Truncate to fit Telegram limits
        if len(keyboard_rows) > 100:
            keyboard_rows = keyboard_rows[:100]

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        # Build message text
        lines: List[str] = ["<b>Available Skills</b>\n"]
        for cmd in commands:
            desc = cmd.get("description", "")
            line = f"  \u2022 <code>{escape_html(cmd['name'])}</code>"
            if desc:
                line += f" \u2014 {escape_html(desc[:80])}"
            lines.append(line)

        message = "\n".join(lines)
        if len(message) > 4000:
            message = message[:3950] + "\n\n<i>... truncated</i>"

        await update.message.reply_text(
            message,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def handle_remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Remove this topic — double-confirm then delete permanently."""
        if not update.message or not update.effective_user:
            return
        thread_ctx = context.user_data.get("_thread_context")
        if not thread_ctx:
            await update.message.reply_text("Use /remove inside a project topic.")
            return

        user_id = update.effective_user.id
        chat_id = thread_ctx["chat_id"]
        message_thread_id = thread_ctx["message_thread_id"]

        # Show confirmation with inline button
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirm",
                        callback_data=f"remove_confirm:{message_thread_id}",
                    ),
                    InlineKeyboardButton("Cancel", callback_data="remove_cancel"),
                ]
            ]
        )
        await update.message.reply_text(
            "\u26a0\ufe0f This will <b>permanently delete</b> this topic and all messages.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    async def _handle_remove_confirmed(
        self,
        user_id: int,
        chat_id: int,
        message_thread_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Execute topic removal after confirmation."""

        # Disconnect client
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            await client_manager.disconnect(user_id, chat_id, message_thread_id)

        # Deactivate DB row
        manager = context.bot_data.get("project_threads_manager")
        if manager:
            await manager.repository.deactivate(chat_id, message_thread_id)

        # Delete topic (falls back to close)
        lifecycle: Optional[TopicLifecycleManager] = context.bot_data.get(
            "lifecycle_manager"
        )
        if lifecycle:
            await lifecycle.delete_confirmed(context.bot, chat_id, message_thread_id)
        else:
            # Fallback: just close
            try:
                await context.bot.close_forum_topic(
                    chat_id=chat_id, message_thread_id=message_thread_id
                )
            except Exception:
                pass

        logger.info(
            "topic_deleted", chat_id=chat_id, message_thread_id=message_thread_id
        )

    async def handle_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show session transcript history in this topic."""
        if not update.message or not update.effective_user:
            return

        chat_id, message_thread_id = self._resolve_chat_key(update, context)
        storage = context.bot_data.get("storage")

        # Parse optional limit argument
        args = update.message.text.split()[1:] if update.message.text else []
        last_n = None
        if args:
            try:
                last_n = int(args[0])
            except ValueError:
                await update.message.reply_text("Usage: /history [N]")
                return

        # Resolve session
        session = (
            await storage.load_session(chat_id, message_thread_id) if storage else None
        )
        if not session or not session.session_id:
            await update.message.reply_text("No active session in this chat.")
            return

        from src.claude.transcript import format_condensed, read_full_transcript

        entries = read_full_transcript(session.session_id, session.directory)
        if not entries:
            await update.message.reply_text(
                f"No transcript available for session <code>{session.session_id[:8]}...</code>",
                parse_mode="HTML",
            )
            return

        messages = format_condensed(entries, last_n=last_n)
        for msg in messages:
            await update.message.reply_text(msg)

    async def handle_restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Restart the bot process (for picking up code changes)."""
        if not update.message:
            return
        await update.message.reply_text("Restarting bot...")
        logger.info("restart_requested", user_id=update.effective_user.id)
        # SIGINT triggers graceful shutdown; systemd/wrapper restarts the process
        os.kill(os.getpid(), signal.SIGINT)

    async def handle_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show session picker for current directory."""
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

        # Get current directory
        current_directory = context.user_data.get("current_directory")
        if not current_directory:
            # Fall back to first approved directory
            roots = self.settings.approved_directories
            if roots:
                current_directory = roots[0]
            else:
                await update.message.reply_text(
                    "No approved directories configured.",
                    parse_mode="Markdown",
                )
                return

        # Read Claude history and filter by current directory
        history_entries = read_claude_history()
        filtered_entries = filter_by_directory(history_entries, current_directory)

        # Check history format health
        from ..claude.history import DEFAULT_HISTORY_PATH

        health_warning = check_history_format_health(DEFAULT_HISTORY_PATH)
        if health_warning:
            await update.message.reply_text(
                f"⚠️ {health_warning}",
                parse_mode="Markdown",
            )

        # Sort by timestamp descending (newest first)
        sorted_entries = sorted(
            filtered_entries, key=lambda e: e.timestamp, reverse=True
        )

        # Build inline keyboard
        keyboard_rows: List[list] = []  # type: ignore[type-arg]

        if sorted_entries:
            # Cap at 10 sessions
            for entry in sorted_entries[:10]:
                time_str = relative_time(entry.timestamp)
                first_msg = read_first_message(
                    session_id=entry.session_id,
                    project_dir=entry.project,
                )
                display_name = (first_msg or entry.display or entry.session_id[:12])[
                    :45
                ]
                button_label = f"{time_str} — {display_name}"

                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            button_label, callback_data=f"session:{entry.session_id}"
                        )
                    ]
                )

        # Always add "New Session" button at the end
        keyboard_rows.append(
            [InlineKeyboardButton("+ New Session", callback_data="session:new")]
        )

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        # Build message
        dir_name = (
            current_directory.name
            if hasattr(current_directory, "name")
            else str(current_directory)
        )
        if sorted_entries:
            message = (
                f"*Sessions in `{dir_name}/`*\n\n"
                "Select a session to resume or start a new one:"
            )
        else:
            message = f"*No sessions found in `{dir_name}/`*\n\nStart a new session:"

        await update.message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd:, session:, skill:, and model: callbacks."""
        query = update.callback_query
        await query.answer()

        data = query.data
        prefix, value = data.split(":", 1)

        # Handle model callbacks
        if prefix == "model":
            await self.handle_model_callback(update, context)
            return

        # Handle remove confirmation/cancel
        if prefix == "remove_confirm":
            thread_id = int(value)
            user_id = query.from_user.id
            chat_id = query.message.chat.id if query.message else 0
            await query.edit_message_text("Deleting topic...")
            await self._handle_remove_confirmed(user_id, chat_id, thread_id, context)
            return

        if prefix == "remove_cancel":
            await query.edit_message_text("Cancelled.")
            return

        # Handle skill callbacks
        if prefix == "skill":
            skill_name = value
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directories[0]
            )

            skill_prompt = f"/{skill_name}"
            session_id = context.user_data.get("claude_session_id")

            # Show running message
            await query.edit_message_text(
                f"⚙️ Running skill: <b>{escape_html(skill_name)}</b>...",
                parse_mode="HTML",
            )

            # Execute via Claude
            from .attachments import Query

            user_id = query.from_user.id
            force_new = bool(context.user_data.get("force_new_session"))

            skill_start_time = time.time()
            # Create a progress message for stream updates
            progress_msg = await query.message.reply_text("Working...")
            skill_progress_manager = ProgressMessageManager(
                initial_message=progress_msg, start_time=skill_start_time
            )
            on_stream = build_stream_callback(skill_progress_manager)

            chat = query.message.chat
            heartbeat = self._start_typing_heartbeat(chat)

            success = True
            try:
                claude_response = await self._run_claude_query(
                    query=Query(text=skill_prompt),
                    user_id=user_id,
                    current_dir=current_dir,
                    session_id=session_id,
                    force_new=force_new,
                    on_stream=on_stream,
                    context=context,
                )

                if force_new:
                    context.user_data["force_new_session"] = False

                context.user_data["claude_session_id"] = claude_response.session_id

                # Track directory changes
                from .handlers.message import (
                    _update_working_directory_from_claude_response,
                )

                _update_working_directory_from_claude_response(
                    claude_response, context, self.settings, user_id
                )

                # Format response
                from .utils.formatting import ResponseFormatter

                formatter = ResponseFormatter(self.settings)
                formatted_messages = formatter.format_claude_response(
                    claude_response.content
                )

            except Exception as e:
                success = False
                logger.error(
                    "Claude skill execution failed", error=str(e), user_id=user_id
                )
                from .handlers.message import _format_error_message
                from .utils.formatting import FormattedMessage

                formatted_messages = [
                    FormattedMessage(_format_error_message(e), parse_mode="HTML")
                ]
            finally:
                heartbeat.cancel()

            await skill_progress_manager.finalize()

            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await query.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await query.message.reply_text(
                            message.text,
                            reply_markup=None,
                        )
                    except Exception as plain_err:
                        await query.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again."
                        )

            # Audit log
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="skill",
                    args=[skill_name],
                    success=success,
                )
            return

        # Handle nav: callbacks (browse into directory)
        if prefix == "nav":
            roots = self.settings.approved_directories
            browse_root = context.user_data.get("repo_browse_root", roots[0])
            if browse_root not in roots:
                browse_root = roots[0]
                context.user_data["repo_browse_root"] = browse_root
            browse_rel = context.user_data.get("repo_browse_rel", "")

            if value == "..":
                # Go up one level
                if browse_rel:
                    parent_rel = str(Path(browse_rel).parent)
                    new_rel = "" if parent_rel == "." else parent_rel
                else:
                    # At root — stay at root
                    new_rel = ""

                context.user_data["repo_browse_rel"] = new_rel
            else:
                # Navigate into directory
                browse_dir = (browse_root / value).resolve()
                if not browse_dir.is_dir() or not browse_dir.is_relative_to(
                    browse_root
                ):
                    await query.edit_message_text(
                        f"Directory not found: <code>{escape_html(value)}</code>",
                        parse_mode="HTML",
                    )
                    return
                context.user_data["repo_browse_rel"] = str(
                    browse_dir.relative_to(browse_root)
                )
                context.user_data["repo_browse_root"] = browse_root

            browse_rel = context.user_data["repo_browse_rel"]
            browse_dir = browse_root / browse_rel if browse_rel else browse_root
            await self._send_repo_browser(
                query.message, browse_dir, browse_root, roots, context, edit=True
            )
            return

        # Handle sel: callbacks (select directory)
        if prefix == "sel":
            roots = self.settings.approved_directories
            browse_root = context.user_data.get("repo_browse_root", roots[0])
            if browse_root not in roots:
                browse_root = roots[0]
                context.user_data["repo_browse_root"] = browse_root
            browse_rel = context.user_data.get("repo_browse_rel", "")
            storage = context.bot_data.get("storage")

            if value == ".":
                target_path = browse_root / browse_rel if browse_rel else browse_root
            else:
                target_path = (browse_root / value).resolve()

            if not target_path.is_dir():
                await query.edit_message_text(
                    f"Directory not found: <code>{escape_html(value)}</code>",
                    parse_mode="HTML",
                )
                return

            # Validate security boundary
            if not any(
                target_path == r or target_path.is_relative_to(r) for r in roots
            ):
                await query.edit_message_text("Access denied.", parse_mode="HTML")
                return

            _sel_chat_id, _sel_thread_id = self._resolve_chat_key(update, context)
            await self._select_directory(
                query.message,
                target_path,
                storage,
                context,
                edit=True,
                user_id=query.from_user.id,
                chat_id=_sel_chat_id,
                message_thread_id=_sel_thread_id,
            )

            # Audit log
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=query.from_user.id,
                    command="repo",
                    args=[str(target_path)],
                    success=True,
                )
            return

        # Handle start_nav: callbacks (navigate in /start wizard browser)
        if prefix == "start_nav":
            roots = self.settings.approved_directories
            add_browse_root = context.user_data.get("add_browse_root", roots[0])
            if add_browse_root not in roots:
                add_browse_root = roots[0]
                context.user_data["add_browse_root"] = add_browse_root
            add_browse_rel = context.user_data.get("add_browse_rel", "")

            if value == "..":
                if add_browse_rel:
                    parent_rel = str(Path(add_browse_rel).parent)
                    new_rel = "" if parent_rel == "." else parent_rel
                else:
                    new_rel = ""
                context.user_data["add_browse_rel"] = new_rel
            else:
                browse_dir = (add_browse_root / value).resolve()
                if not browse_dir.is_dir() or not browse_dir.is_relative_to(
                    add_browse_root
                ):
                    await query.edit_message_text(
                        f"Directory not found: <code>{escape_html(value)}</code>",
                        parse_mode="HTML",
                    )
                    return
                context.user_data["add_browse_rel"] = str(
                    browse_dir.relative_to(add_browse_root)
                )
                context.user_data["add_browse_root"] = add_browse_root

            add_browse_rel = context.user_data["add_browse_rel"]
            browse_dir = (
                add_browse_root / add_browse_rel if add_browse_rel else add_browse_root
            )

            # Rebuild keyboard with start_ prefixes
            keyboard_rows = build_browser_keyboard(
                browse_dir, add_browse_root, multi_root=len(roots) > 1
            )
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

            await query.edit_message_text(
                build_browse_header(browse_dir, add_browse_root),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(remapped_rows),
            )
            return

        # Handle start_sel: callbacks (select directory in /start wizard)
        if prefix == "start_sel":
            roots = self.settings.approved_directories
            add_browse_root = context.user_data.get("add_browse_root", roots[0])
            if add_browse_root not in roots:
                add_browse_root = roots[0]
            add_browse_rel = context.user_data.get("add_browse_rel", "")

            if value == ".":
                target_path = (
                    add_browse_root / add_browse_rel
                    if add_browse_rel
                    else add_browse_root
                )
            else:
                target_path = (add_browse_root / value).resolve()

            if not target_path.is_dir():
                await query.edit_message_text(
                    f"Directory not found: <code>{escape_html(value)}</code>",
                    parse_mode="HTML",
                )
                return

            if not any(
                target_path == r or target_path.is_relative_to(r) for r in roots
            ):
                await query.edit_message_text("Access denied.", parse_mode="HTML")
                return

            await self._start_wizard_session_picker(
                query.message, target_path, context, edit=True
            )
            return

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
            session_id: Optional[str] = value if value != "new" else None

            manager = context.bot_data.get("project_threads_manager")
            if not manager:
                await query.edit_message_text("Project threads not configured.")
                return

            # Check if a topic already exists for this session
            if session_id and manager.repository:
                existing = await manager.repository.find_by_session_id(
                    chat_id, session_id
                )
                if existing:
                    topic_link = (
                        f"https://t.me/c/{str(chat_id).removeprefix('-100')}"
                        f"/{existing.message_thread_id}"
                    )
                    topic_label = existing.topic_name or dir_name
                    await query.edit_message_text(
                        f"Session already has a topic: "
                        f'<a href="{topic_link}">{escape_html(topic_label)}</a>',
                        parse_mode="HTML",
                    )
                    # Cleanup wizard state
                    context.user_data.pop("start_wizard_dir", None)
                    context.user_data.pop("start_browse_root", None)
                    context.user_data.pop("start_browse_rel", None)
                    return

            # Generate topic name
            if session_id:
                topic_name = f"{dir_name} — {session_id[:8]}"
            else:
                topic_name = dir_name

            # Avoid collision with existing topic names
            existing = await manager.list_topics(chat_id)
            existing_names = [t.topic_name for t in existing]
            final_name = manager.generate_topic_name(
                directory, existing_names, override_name=topic_name
            )

            await query.edit_message_text(
                f"Creating topic <b>{escape_html(final_name)}</b>...",
                parse_mode="HTML",
            )

            try:
                mapping = await manager.create_topic(
                    context.bot,
                    chat_id,
                    user_id,
                    directory,
                    final_name,
                    session_id=session_id,
                )
                thread_id = mapping.message_thread_id

                # Eagerly connect
                client_manager: Optional[ClientManager] = context.bot_data.get(
                    "client_manager"
                )
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
                        lifecycle_wiz: Optional[TopicLifecycleManager] = (
                            context.bot_data.get("lifecycle_manager")
                        )
                        if lifecycle_wiz:
                            await lifecycle_wiz.rename_topic(
                                context.bot, chat_id, thread_id, new_name
                            )
                        await manager.repository.upsert(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                            user_id=user_id,
                            directory=directory,
                            topic_name=new_name,
                            session_id=client.session_id,
                        )

                await query.edit_message_text(
                    f"Topic <b>{escape_html(final_name)}</b> created "
                    f"→ <code>{escape_html(directory)}</code>",
                    parse_mode="HTML",
                )

                # Send brief transcript preview for existing sessions
                if session_id:
                    try:
                        from src.claude.transcript import (
                            format_condensed,
                            read_full_transcript,
                        )

                        entries = read_full_transcript(session_id, directory)
                        if entries:
                            # Single message with as much recent history as fits
                            history_messages = format_condensed(entries, last_n=20)
                            if history_messages:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=history_messages[-1],
                                    message_thread_id=thread_id,
                                )
                            # Mark as replayed so _execute_query skips it
                            context.chat_data[f"_history_replayed_{thread_id}"] = True
                    except Exception as e:
                        logger.warning(
                            "wizard_transcript_send_failed",
                            error=str(e),
                            session_id=session_id,
                            thread_id=thread_id,
                        )

            except Exception as e:
                logger.error("start_wizard_create_failed", error=str(e))
                await query.message.reply_text(
                    f"Failed to create topic: {str(e)[:200]}"
                )

            # Cleanup wizard state
            context.user_data.pop("start_wizard_dir", None)
            context.user_data.pop("start_browse_root", None)
            context.user_data.pop("start_browse_rel", None)
            return

        # Handle session callbacks
        if prefix == "session":
            _ses_chat_id, _ses_thread_id = self._resolve_chat_key(update, context)
            if value == "new":
                context.user_data["force_new_session"] = True
                # Eager connect like /new
                client_manager = context.bot_data.get("client_manager")
                current_dir = context.user_data.get(
                    "current_directory",
                    self.settings.approved_directories[0],
                )
                if client_manager:
                    try:
                        client = await client_manager.get_or_connect(
                            user_id=query.from_user.id,
                            chat_id=_ses_chat_id,
                            message_thread_id=_ses_thread_id,
                            directory=str(current_dir),
                            session_id=None,
                            force_new=True,
                            approved_directory=str(
                                self.settings.approved_directories[0]
                            ),
                        )
                        context.user_data["claude_session_id"] = client.session_id
                        context.user_data["force_new_session"] = False
                    except Exception:
                        pass  # Will lazy-connect on next message
                await query.edit_message_text(
                    "New session started. Ready.",
                    parse_mode="HTML",
                )
            else:
                # Resume session by ID — eagerly connect
                context.user_data["claude_session_id"] = value
                current_dir = context.user_data.get(
                    "current_directory",
                    self.settings.approved_directories[0],
                )
                client_manager = context.bot_data.get("client_manager")
                if client_manager:
                    try:
                        await client_manager.switch_session(
                            user_id=query.from_user.id,
                            chat_id=_ses_chat_id,
                            message_thread_id=_ses_thread_id,
                            session_id=value,
                            directory=str(current_dir),
                            approved_directory=str(
                                self.settings.approved_directories[0]
                            ),
                        )
                    except Exception:
                        logger.debug(
                            "session_callback_eager_connect_failed", session_id=value
                        )

                # Show transcript preview (last 3 messages)
                recent_lines: List[str] = [
                    "\U0001f4c2 <b>Session resumed. Ready.</b>\n"
                ]
                try:
                    transcript = read_session_transcript(
                        session_id=value,
                        project_dir=str(current_dir),
                        limit=3,
                    )
                    if transcript:
                        recent_lines.append("<b>Recent:</b>")
                        for msg in transcript:
                            preview = msg.text[:120]
                            if len(msg.text) > 120:
                                preview += "\u2026"
                            label = "You" if msg.role == "user" else "Claude"
                            recent_lines.append(
                                f"  <b>{label}:</b> {escape_html(preview)}"
                            )
                except Exception:
                    pass

                await query.edit_message_text(
                    "\n".join(recent_lines),
                    parse_mode="HTML",
                )

            # Audit log
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=query.from_user.id,
                    command="session",
                    args=[value],
                    success=True,
                )
            return

        # Handle cd callbacks (existing logic)
        roots = self.settings.approved_directories
        storage = context.bot_data.get("storage")
        path_str = value

        # Parse the path - could be relative name or absolute path
        new_path = None
        if Path(path_str).is_absolute():
            # Absolute path from callback
            candidate = Path(path_str)
            if candidate.is_dir() and any(
                candidate == r or candidate.is_relative_to(r) for r in roots
            ):
                new_path = candidate
        else:
            # Relative name - search across all roots
            for root in roots:
                candidate = root / path_str
                if candidate.is_dir():
                    new_path = candidate
                    break

        if not new_path:
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(path_str)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Clear session — user must /new or /resume explicitly
        context.user_data["claude_session_id"] = None
        context.user_data["force_new_session"] = False

        # Disconnect active SDK session for the current chat key
        _cd_chat_id, _cd_thread_id = self._resolve_chat_key(update, context)
        client_manager_cd: Optional[ClientManager] = context.bot_data.get(
            "client_manager"
        )
        if client_manager_cd:
            await client_manager_cd.disconnect(
                query.from_user.id, _cd_chat_id, _cd_thread_id
            )

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(new_path.name)}/</code>" f"{git_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[str(new_path)],
                success=True,
            )
