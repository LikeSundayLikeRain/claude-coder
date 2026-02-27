"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

from ..claude.client_manager import ClientManager
from ..claude.history import (
    append_history_entry,
    check_history_format_health,
    filter_by_directory,
    read_claude_history,
    read_session_transcript,
)
from ..claude.sdk_integration import StreamUpdate
from ..claude.stream_handler import StreamHandler
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from ..skills.loader import discover_skills, load_skill_body, resolve_skill_prompt
from .utils.html_format import escape_html

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

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
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("stop", self.handle_stop),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("compact", self.agentic_compact),
            ("model", self.handle_model),
            ("repo", self.agentic_repo),
            ("sessions", self.agentic_sessions),
            ("commands", self.agentic_commands),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Unrecognized /commands -> skill lookup + Claude fallback.
        # Registered AFTER CommandHandlers in group 0 so it only runs
        # when no CommandHandler matched (within a group, first match wins).
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
        )

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Callbacks for cd:, session:, skill:, and model: patterns
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^(cd:|session:|skill:|model:)",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("stop", "Interrupt running query"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("compact", "Compress context, keep continuity"),
                BotCommand("model", "Switch Claude model"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("sessions", "Choose a session to resume"),
                BotCommand("commands", "Browse available skills"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Interrupt the currently running Claude query."""
        if not update.effective_user or not update.message:
            return
        user_id = update.effective_user.id
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            client = client_manager.get_active_client(user_id)
            if client and client.is_querying:
                await client_manager.interrupt(user_id)
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

        Note: query.answer() is already called by _agentic_callback.
        """
        query = update.callback_query
        if not query or not query.data:
            return
        data = query.data  # e.g. "model:sonnet" or "model:opus:1m"
        parts = data.split(":")
        # parts[0] = "model", parts[1] = model name, parts[2] = optional "1m"
        if len(parts) < 2:
            return
        model = parts[1]
        is_1m = len(parts) > 2 and parts[2] == "1m"
        betas = ["context-1m-2025-08-07"] if is_1m else None

        # Build display label
        label_map = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
        label = label_map.get(model, model)
        if is_1m:
            label += " 1M"

        user_id = query.from_user.id
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            await client_manager.set_model(user_id, model, betas)
            await query.edit_message_text(f"Model set to: {label}")
        else:
            await query.edit_message_text("Model switching is not available.")

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
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
            f"/stop — Interrupt running query\n"
            f"/status — Current session info\n"
            f"/model — Switch Claude model\n"
            f"/sessions — Pick a session to resume\n"
            f"/commands — Browse available skills\n"
            f"/compact — Compress context\n"
            f"/repo — Switch workspace\n"
            f"/verbose — Set output level (0/1/2)"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        user_id = update.effective_user.id
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        # Disconnect persistent client so next message starts fresh
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            await client_manager.disconnect(user_id)

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show current session status with workspace and session info."""
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
        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager:
            active_client = client_manager.get_active_client(update.effective_user.id)
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

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f"<b>Cost:</b> ${current_cost:.2f}\n"
            except Exception:
                pass

        await update.message.reply_text(
            f"{workspace_line}"
            f"<b>Directory:</b> <code>{dir_display}</code>\n"
            f"{session_line}"
            f"{client_line}"
            f"{cost_str}",
            parse_mode="HTML",
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    async def agentic_compact(
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

            summary_response = await self._run_claude_query(
                prompt=summary_prompt,
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
                prompt=reseed_prompt,
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

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    async def _run_claude_query(
        self,
        prompt: str,
        user_id: int,
        current_dir: Any,
        session_id: Optional[str],
        force_new: bool,
        on_stream: Optional[Callable[[StreamUpdate], Any]],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> "ClaudeResponse":
        """Run a query via ClientManager, streaming events through on_stream.

        Returns a ClaudeResponse for compatibility with existing formatting code.
        Falls back to ClaudeIntegration if ClientManager is not available.
        """
        from ..claude.sdk_integration import ClaudeResponse

        client_manager: Optional[ClientManager] = context.bot_data.get("client_manager")
        if client_manager is None:
            # Safety fallback / classic-mode compatibility: in agentic mode
            # client_manager is always set up in main.py, so this branch is
            # only reachable if running in classic mode or during tests that
            # don't provide a ClientManager.
            claude_integration = context.bot_data.get("claude_integration")
            if not claude_integration:
                raise RuntimeError(
                    "Neither client_manager nor claude_integration available"
                )
            return await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

        directory = str(current_dir)
        approved_dir = str(self.settings.approved_directory)
        stream_handler = StreamHandler()

        start_ms = int(time.time() * 1000)

        client = await client_manager.get_or_connect(
            user_id=user_id,
            directory=directory,
            session_id=None if force_new else session_id,
            approved_directory=approved_dir,
            force_new=force_new,
        )

        response_text = ""
        result_session_id: Optional[str] = None
        cost = 0.0
        num_turns = 0

        async for message in client.query(prompt):
            event = stream_handler.extract_content(message)
            # Partial SDK StreamEvents are for UX progress only;
            # complete AssistantMessages drive turn counting.
            is_partial = message.__class__.__name__ == "StreamEvent"

            if event.type == "result":
                response_text = event.content or ""
                result_session_id = event.session_id
                cost = event.cost or 0.0
                if result_session_id:
                    await client_manager.update_session_id(user_id, result_session_id)
            elif event.type == "text" and event.content:
                if on_stream:
                    await on_stream(
                        StreamUpdate(type="assistant", content=event.content)
                    )
            elif event.type == "tool_use":
                if not is_partial:
                    num_turns += 1
                if on_stream:
                    await on_stream(
                        StreamUpdate(
                            type="assistant",
                            tool_calls=[
                                {
                                    "name": event.tool_name or "",
                                    "input": event.tool_input or {},
                                }
                            ],
                        )
                    )
            elif event.type == "thinking" and event.content:
                if on_stream:
                    await on_stream(
                        StreamUpdate(
                            type="assistant", content=f"\U0001f4ad {event.content}"
                        )
                    )

        duration_ms = int(time.time() * 1000) - start_ms

        return ClaudeResponse(
            content=response_text,
            session_id=result_session_id or "",
            cost=cost,
            duration_ms=duration_ms,
            num_turns=num_turns,
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

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        Returns None when verbose_level is 0 (nothing to display).
        Typing indicators are handled by a separate heartbeat task.
        """
        if verbose_level == 0:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    tool_log.append({"kind": "tool", "name": name, "detail": detail})

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text and verbose_level >= 1:
                    # Collapse to first meaningful line, cap length
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        tool_log.append({"kind": "text", "detail": first_line[:120]})

            # Throttle progress message edits to avoid Telegram rate limits
            now = time.time()
            if (now - last_edit_time[0]) >= 2.0 and tool_log:
                last_edit_time[0] = now
                new_text = self._format_verbose_progress(
                    tool_log, verbose_level, start_time
                )
                try:
                    await progress_msg.edit_text(new_text)
                except Exception:
                    pass

        return _on_stream

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Check if this is a skill invocation (e.g., "/skillname args")
        # Skip if it's a registered bot command
        if message_text.startswith("/"):
            parts = message_text[1:].split(None, 1)
            if parts:
                potential_skill_name = parts[0]
                skill_args = parts[1] if len(parts) > 1 else ""

                # List of registered bot commands to skip
                registered_commands = {
                    "start",
                    "new",
                    "stop",
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
                    # Try to find this skill
                    current_dir = context.user_data.get(
                        "current_directory", self.settings.approved_directories[0]
                    )
                    skills = discover_skills(project_dir=Path(current_dir))
                    # Match exact name, then unprefixed exact, then prefix match.
                    # e.g., "brainstorm" matches "superpowers:brainstorming"
                    skill = next(
                        (s for s in skills if s.name == potential_skill_name),
                        None,
                    )
                    if skill is None:
                        # Try unprefixed exact match
                        skill = next(
                            (
                                s
                                for s in skills
                                if ":" in s.name
                                and s.name.split(":", 1)[1] == potential_skill_name
                            ),
                            None,
                        )
                    if skill is None:
                        # Try prefix match on unprefixed part
                        skill = next(
                            (
                                s
                                for s in skills
                                if ":" in s.name
                                and s.name.split(":", 1)[1].startswith(
                                    potential_skill_name
                                )
                            ),
                            None,
                        )

                    if skill:
                        # This is a skill invocation — load and resolve it
                        body = load_skill_body(skill)
                        if body:
                            session_id = context.user_data.get("claude_session_id", "")
                            resolved = resolve_skill_prompt(
                                body, skill_args, session_id
                            )
                            # Frame the skill body as instructions, not a
                            # user question — mirrors how the CLI's Skill
                            # tool presents skills to Claude.
                            args_line = (
                                f"\nUser arguments: {skill_args}"
                                if skill_args
                                else ""
                            )
                            message_text = (
                                f"<skill-invocation>\n"
                                f"The user has invoked the /{skill.name} "
                                f"skill. Follow the instructions in the "
                                f"skill body below exactly as written."
                                f"{args_line}\n"
                                f"</skill-invocation>\n\n"
                                f"<skill-body>\n{resolved}\n</skill-body>"
                            )
                            logger.info(
                                "Skill invocation detected",
                                skill_name=skill.name,
                                user_id=user_id,
                            )

        # Restore persisted directory if not already set
        storage = context.bot_data.get("storage")
        if not context.user_data.get("current_directory") and storage:
            persisted_dir = await storage.load_user_directory(user_id)
            if persisted_dir:
                persisted_path = Path(persisted_dir)
                # Validate that persisted path is still in approved directories
                if persisted_path.is_dir() and any(
                    persisted_path == r or persisted_path.is_relative_to(r)
                    for r in self.settings.approved_directories
                ):
                    context.user_data["current_directory"] = persisted_path
                    logger.debug(
                        "Restored user directory from database",
                        user_id=user_id,
                        directory=str(persisted_path),
                    )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)
        progress_msg = await update.message.reply_text("Working...")

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directories[0]
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        on_stream = self._make_stream_callback(
            verbose_level, progress_msg, tool_log, start_time
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await self._run_claude_query(
                prompt=message_text,
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
                display_preview = message_text[:80] if message_text else ""
                append_history_entry(
                    session_id=claude_response.session_id,
                    display=display_preview,
                    project=str(current_dir),
                )

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()

        await progress_msg.delete()

        for i, message in enumerate(formatted_messages):
            if not message.text or not message.text.strip():
                continue
            try:
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,  # No keyboards in agentic mode
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
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
                    await update.message.reply_text(
                        message.text,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                except Exception as plain_err:
                    await update.message.reply_text(
                        f"Failed to deliver response "
                        f"(Telegram error: {str(plain_err)[:150]}). "
                        f"Please try again.",
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level, progress_msg, tool_log, time.time()
        )

        heartbeat = self._start_typing_heartbeat(chat)
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

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            await progress_msg.delete()

            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )

            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

            # Check if /new was used — skip auto-resume for this first message.
            # Flag is only cleared after a successful run so retries keep the intent.
            force_new = bool(context.user_data.get("force_new_session"))

            verbose_level = self._get_verbose_level(context)
            tool_log: List[Dict[str, Any]] = []
            on_stream = self._make_stream_callback(
                verbose_level, progress_msg, tool_log, time.time()
            )

            heartbeat = self._start_typing_heartbeat(chat)
            try:
                claude_response = await self._run_claude_query(
                    prompt=processed_image.prompt,
                    user_id=user_id,
                    current_dir=current_dir,
                    session_id=session_id,
                    force_new=force_new,
                    on_stream=on_stream,
                    context=context,
                )
            finally:
                heartbeat.cancel()

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            await progress_msg.delete()

            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list all workspace roots and their subdirectories
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        roots = self.settings.approved_directories
        storage = context.bot_data.get("storage")

        # Get current directory
        current_dir = context.user_data.get("current_directory")
        if not current_dir and storage:
            # Try to restore from database
            persisted = await storage.load_user_directory(update.effective_user.id)
            if persisted:
                current_dir = Path(persisted)
                context.user_data["current_directory"] = current_dir

        if args:
            # Switch to named repo - search across all roots
            target_name = args[0]
            target_path = None

            # Try each root
            for root in roots:
                candidate = root / target_name
                if candidate.is_dir():
                    target_path = candidate
                    break
                # Also check if target_name is an absolute path within roots
                candidate_abs = Path(target_name).resolve()
                if candidate_abs.is_dir() and any(
                    candidate_abs == r or candidate_abs.is_relative_to(r) for r in roots
                ):
                    target_path = candidate_abs
                    break

            if not target_path:
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Persist to database
            if storage:
                await storage.save_user_directory(
                    update.effective_user.id, str(target_path)
                )

            # Try to find a resumable session from history.jsonl
            session_id = None
            client_manager_repo: Optional[ClientManager] = context.bot_data.get(
                "client_manager"
            )
            if client_manager_repo:
                session_id = client_manager_repo.get_latest_session(str(target_path))
            else:
                # Legacy fallback: only reachable in classic mode or tests
                # without a ClientManager. Agentic mode always has one.
                claude_integration = context.bot_data.get("claude_integration")
                if claude_integration:
                    session_id = claude_integration._find_resumable_session_id(
                        target_path
                    )
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_path.name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list all workspace roots and their subdirectories
        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]

        # First, add workspace root buttons if multiple roots
        if len(roots) > 1:
            lines.append("<b>📁 Workspaces</b>")
            root_row = []
            for root in roots:
                root_name = root.name
                marker = " \u25c0" if current_dir == root else ""
                lines.append(f"  <code>{escape_html(root_name)}/</code>{marker}")
                root_row.append(
                    InlineKeyboardButton(root_name, callback_data=f"cd:{str(root)}")
                )
                if len(root_row) == 2:
                    keyboard_rows.append(root_row)
                    root_row = []
            if root_row:
                keyboard_rows.append(root_row)
            lines.append("")  # Blank line separator

        # List subdirectories for each root
        for root in roots:
            try:
                entries = sorted(
                    [
                        d
                        for d in root.iterdir()
                        if d.is_dir() and not d.name.startswith(".")
                    ],
                    key=lambda d: d.name,
                )
            except OSError as e:
                logger.warning(
                    "Error reading workspace root", root=str(root), error=str(e)
                )
                continue

            if not entries:
                continue

            # Section header for this root (only if multiple roots)
            if len(roots) > 1:
                lines.append(f"<b>📂 {escape_html(root.name)}/</b>")

            for d in entries:
                is_git = (d / ".git").is_dir()
                icon = "\U0001f4e6" if is_git else "\U0001f4c1"
                marker = " \u25c0" if d == current_dir else ""
                rel_name = d.name if len(roots) == 1 else f"{root.name}/{d.name}"
                lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

            # Build inline keyboard (2 per row)
            for i in range(0, len(entries), 2):
                row = []
                for j in range(2):
                    if i + j < len(entries):
                        d = entries[i + j]
                        name = d.name
                        row.append(
                            InlineKeyboardButton(name, callback_data=f"cd:{str(d)}")
                        )
                keyboard_rows.append(row)

            if len(roots) > 1:
                lines.append("")  # Blank line between roots

        if not lines:
            workspaces_list = "\n".join(
                f"  • <code>{escape_html(str(r))}</code>" for r in roots
            )
            await update.message.reply_text(
                f"No repos in workspaces:\n{workspaces_list}\n\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        reply_markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

        header = "<b>Repos</b>\n\n" if len(roots) == 1 else ""
        await update.message.reply_text(
            header + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def agentic_commands(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show available skills as inline keyboard buttons."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directories[0]
        )

        # Discover skills
        skills = discover_skills(project_dir=Path(current_dir))

        if not skills:
            await update.message.reply_text(
                "📝 <b>No Skills Found</b>\n\n"
                "Create skills in:\n"
                "  • <code>.claude/skills/&lt;name&gt;/SKILL.md</code> (project)\n"
                "  • <code>~/.claude/skills/&lt;name&gt;/SKILL.md</code> (personal)\n\n"
                "Or install plugins via Claude Code CLI.\n\n"
                "Legacy commands also supported:\n"
                "  • <code>.claude/commands/&lt;name&gt;.md</code>\n"
                "  • <code>~/.claude/commands/&lt;name&gt;.md</code>",
                parse_mode="HTML",
            )
            return

        # Group by source
        project_skills = [
            s for s in skills if s.source in ("project", "legacy_project")
        ]
        personal_skills = [
            s for s in skills if s.source in ("personal", "legacy_personal")
        ]
        plugin_skills = [s for s in skills if s.source == "plugin"]

        # Helper to build a button for a skill
        def _skill_button(skill: Any) -> InlineKeyboardButton:
            if skill.argument_hint:
                return InlineKeyboardButton(
                    f"{skill.name} ...",
                    switch_inline_query_current_chat=f"/{skill.name} ",
                )
            return InlineKeyboardButton(skill.name, callback_data=f"skill:{skill.name}")

        # Build inline keyboard — project/personal get one per row,
        # plugin skills get two per row for compactness
        keyboard_rows: List[list] = []  # type: ignore[type-arg]

        for group in (project_skills, personal_skills):
            for skill in group:
                keyboard_rows.append([_skill_button(skill)])

        # Plugin skills: 2 per row
        plugin_row: list = []  # type: ignore[type-arg]
        for skill in plugin_skills:
            plugin_row.append(_skill_button(skill))
            if len(plugin_row) == 2:
                keyboard_rows.append(plugin_row)
                plugin_row = []
        if plugin_row:
            keyboard_rows.append(plugin_row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        # Build message text — project/personal get descriptions,
        # plugin skills are name-only grouped by plugin for compactness
        lines: List[str] = ["<b>Available Skills</b>\n"]

        for label, group in [
            ("\U0001f4c2 Project", project_skills),
            ("\U0001f464 Personal", personal_skills),
        ]:
            if not group:
                continue
            lines.append(f"<b>{label}</b>")
            for skill in group:
                desc_line = f"  \u2022 <code>{skill.name}</code>"
                if skill.description:
                    desc_line += f" \u2014 {escape_html(skill.description[:80])}"
                if skill.argument_hint:
                    desc_line += f" <i>({escape_html(skill.argument_hint)})</i>"
                lines.append(desc_line)
            lines.append("")

        # Plugin skills: group by plugin name, names only
        if plugin_skills:
            # Group by plugin prefix (e.g., "superpowers" from "superpowers:brainstorming")
            plugin_groups: Dict[str, List[str]] = {}
            for skill in plugin_skills:
                if ":" in skill.name:
                    prefix, short_name = skill.name.split(":", 1)
                else:
                    prefix, short_name = "other", skill.name
                plugin_groups.setdefault(prefix, []).append(short_name)

            lines.append("\U0001f4e6 <b>Plugin Skills</b>")
            for plugin_name, skill_names in sorted(plugin_groups.items()):
                names_str = ", ".join(f"<code>{n}</code>" for n in sorted(skill_names))
                lines.append(f"  <b>{escape_html(plugin_name)}</b>: {names_str}")

        # Telegram message limit is 4096 chars — truncate if needed
        message = "\n".join(lines)
        if len(message) > 4000:
            message = message[:3950] + "\n\n<i>... truncated</i>"

        await update.message.reply_text(
            message,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def agentic_sessions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show session picker for current directory."""
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
            from datetime import UTC, datetime

            # Cap at 10 sessions
            for entry in sorted_entries[:10]:
                # Format date from millisecond timestamp
                ts_dt = datetime.fromtimestamp(entry.timestamp / 1000.0, tz=UTC)
                date_str = ts_dt.strftime("%m/%d")
                # Truncate display name to 45 chars
                display_name = (entry.display or entry.session_id[:12])[:45]
                button_label = f"{date_str} — {display_name}"

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
            message = f"*Sessions in `{dir_name}/`*\n\nSelect a session to resume or start a new one:"
        else:
            message = f"*No sessions found in `{dir_name}/`*\n\nStart a new session:"

        await update.message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def _agentic_callback(
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

        # Handle skill callbacks
        if prefix == "skill":
            skill_name = value
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directories[0]
            )

            # Discover and find the skill
            skills = discover_skills(project_dir=Path(current_dir))
            skill = next((s for s in skills if s.name == skill_name), None)

            if not skill:
                await query.edit_message_text(
                    f"❌ Skill not found: <code>{escape_html(skill_name)}</code>",
                    parse_mode="HTML",
                )
                return

            # Load and resolve skill body
            body = load_skill_body(skill)
            if not body:
                await query.edit_message_text(
                    f"❌ Failed to load skill: <code>{escape_html(skill_name)}</code>",
                    parse_mode="HTML",
                )
                return

            session_id = context.user_data.get("claude_session_id", "")
            prompt = resolve_skill_prompt(body, "", session_id)

            # Show running message
            await query.edit_message_text(
                f"⚙️ Running skill: <b>{escape_html(skill_name)}</b>...",
                parse_mode="HTML",
            )

            # Execute via Claude
            user_id = query.from_user.id
            force_new = bool(context.user_data.get("force_new_session"))

            verbose_level = self._get_verbose_level(context)
            tool_log: List[Dict[str, Any]] = []
            start_time = time.time()

            # Create a progress message for stream updates
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

            await progress_msg.delete()

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

        # Handle session callbacks
        if prefix == "session":
            if value == "new":
                context.user_data["force_new_session"] = True
                await query.edit_message_text(
                    "✨ Starting new session",
                    parse_mode="HTML",
                )
            else:
                # Resume session by ID
                context.user_data["claude_session_id"] = value

                # Load recent messages from session transcript
                current_dir = context.user_data.get(
                    "current_directory",
                    self.settings.approved_directories[0],
                )
                recent_lines: List[str] = ["📂 <b>Resumed session</b>\n"]

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
                                preview += "…"
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

        # Persist to database
        if storage:
            await storage.save_user_directory(query.from_user.id, str(new_path))

        # Look for a resumable session instead of always clearing
        session_id = None
        client_manager_cd: Optional[ClientManager] = context.bot_data.get(
            "client_manager"
        )
        if client_manager_cd:
            session_id = client_manager_cd.get_latest_session(str(new_path))
        else:
            # Legacy fallback: only reachable in classic mode or tests
            # without a ClientManager. Agentic mode always has one.
            claude_integration = context.bot_data.get("claude_integration")
            if claude_integration:
                session_id = claude_integration._find_resumable_session_id(new_path)
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(new_path.name)}/</code>"
            f"{git_badge}{session_badge}",
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
