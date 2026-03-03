"""Manages persistent UserClient instances per (user_id, chat_id, message_thread_id)."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

import structlog

from src.claude.options import OptionsBuilder
from src.claude.user_client import UserClient
from src.storage.repositories import ChatSessionRepository

logger = structlog.get_logger()

DEFAULT_IDLE_TIMEOUT_SECONDS = 3600  # 1 hour


class ClientManager:
    """Owns persistent UserClient instances, one per active (user_id, chat_id, message_thread_id) triple."""

    def __init__(
        self,
        chat_session_repo: ChatSessionRepository,
        options_builder: Optional[OptionsBuilder] = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        bot: Optional[Any] = None,
        lifecycle_manager: Optional[Any] = None,
    ) -> None:
        self._chat_session_repo = chat_session_repo
        self._options_builder = options_builder or OptionsBuilder()
        self._idle_timeout = idle_timeout
        self._bot = bot
        self._lifecycle_manager = lifecycle_manager
        self._clients: dict[tuple[int, int, int], UserClient] = {}

    def _make_on_exit(
        self, user_id: int, chat_id: int, message_thread_id: int
    ) -> Callable[[int], None]:
        """Return a closure that removes the triple key on actor exit."""

        def on_exit(_uid: int) -> None:
            self._clients.pop((user_id, chat_id, message_thread_id), None)
            logger.info(
                "client_manager_actor_exited",
                user_id=user_id,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
            )
            # Schedule topic close for group topics (not private DMs)
            if (
                self._bot is not None
                and self._lifecycle_manager is not None
                and chat_id != user_id  # private DM: chat_id == user_id
            ):
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(
                        self._lifecycle_manager.close_on_idle(
                            self._bot,
                            chat_id=chat_id,
                            message_thread_id=message_thread_id,
                        )
                    )
                except RuntimeError:
                    pass  # no event loop (shutdown)

        return on_exit

    async def get_or_connect(
        self,
        user_id: int,
        chat_id: int,
        message_thread_id: int,
        directory: str,
        session_id: Optional[str] = None,
        approved_directory: Optional[str] = None,
        force_new: bool = False,
    ) -> UserClient:
        """Get existing client or create+connect a new one."""
        key = (user_id, chat_id, message_thread_id)
        existing = self._clients.get(key)

        if existing is not None and existing.is_connected and not force_new:
            return existing

        if existing is not None:
            await existing.stop()

        # Resolve session_id: explicit > persisted DB
        # Skip auto-resolution when force_new
        resolved_session_id = session_id

        if resolved_session_id is None and not force_new:
            session = await self._chat_session_repo.get(chat_id, message_thread_id)
            if session is not None and session.session_id:
                resolved_session_id = session.session_id

        # Create and start
        client = UserClient(
            user_id=user_id,
            directory=directory,
            session_id=resolved_session_id,
            idle_timeout=self._idle_timeout,
            on_exit=self._make_on_exit(user_id, chat_id, message_thread_id),
        )
        options = self._options_builder.build(
            cwd=directory,
            session_id=resolved_session_id,
            approved_directory=approved_directory,
        )
        await client.start(options)
        self._clients[key] = client

        # Persist state
        await self._chat_session_repo.upsert(
            chat_id, message_thread_id, user_id, directory, client.session_id
        )

        logger.info(
            "client_manager_connected",
            user_id=user_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            directory=directory,
            session_id=client.session_id,
        )
        return client

    async def switch_session(
        self,
        user_id: int,
        chat_id: int,
        message_thread_id: int,
        session_id: str,
        directory: str,
        approved_directory: Optional[str] = None,
    ) -> UserClient:
        """Stop current client, connect to a different session."""
        key = (user_id, chat_id, message_thread_id)
        existing = self._clients.pop(key, None)
        if existing is not None:
            await existing.stop()

        return await self.get_or_connect(
            user_id=user_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            directory=directory,
            session_id=session_id,
            approved_directory=approved_directory,
        )

    async def set_next_session(
        self,
        user_id: int,
        chat_id: int,
        message_thread_id: int,
        session_id: str,
        directory: str,
    ) -> None:
        """Disconnect current client and persist session for lazy reconnect.

        Unlike switch_session(), this does NOT eagerly connect to the SDK.
        The next user message will trigger get_or_connect() which picks up
        the persisted session from the database.
        """
        await self.disconnect(user_id, chat_id, message_thread_id)
        await self._chat_session_repo.upsert(
            chat_id, message_thread_id, user_id, directory, session_id
        )
        logger.info(
            "client_manager_next_session_set",
            user_id=user_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            session_id=session_id,
            directory=directory,
        )

    async def interrupt(
        self, user_id: int, chat_id: int, message_thread_id: int
    ) -> None:
        """Interrupt active query for the given triple."""
        client = self._clients.get((user_id, chat_id, message_thread_id))
        if client is not None:
            await client.interrupt()

    async def set_model(
        self,
        user_id: int,
        chat_id: int,
        message_thread_id: int,
        model: str,
        betas: Optional[list[str]] = None,
    ) -> None:
        """Update model on the client (in-memory only)."""
        client = self._clients.get((user_id, chat_id, message_thread_id))
        if client is None:
            return
        client.model = model
        if betas is not None:
            client.betas = betas

    def get_active_client(
        self, user_id: int, chat_id: int, message_thread_id: int
    ) -> Optional[UserClient]:
        """Return the active UserClient for the triple, or None."""
        return self._clients.get((user_id, chat_id, message_thread_id))

    def get_available_commands(
        self, user_id: int, chat_id: int, message_thread_id: int
    ) -> list[dict]:
        """Return cached commands for the user's active client, or []."""
        client = self._clients.get((user_id, chat_id, message_thread_id))
        if client is None:
            return []
        return client.available_commands

    async def disconnect(
        self, user_id: int, chat_id: int, message_thread_id: int
    ) -> None:
        """Stop and remove client for the given triple."""
        client = self._clients.pop((user_id, chat_id, message_thread_id), None)
        if client is not None:
            await client.stop()

    def get_all_clients_for_user(
        self, user_id: int
    ) -> list[tuple[int, int, UserClient]]:
        """Return all active (chat_id, message_thread_id, client) tuples for a user."""
        return [
            (chat_id, thread_id, client)
            for (uid, chat_id, thread_id), client in self._clients.items()
            if uid == user_id
        ]

    async def disconnect_all_for_user(self, user_id: int) -> None:
        """Stop all clients for a user."""
        keys = [
            (uid, chat_id, thread_id)
            for uid, chat_id, thread_id in self._clients
            if uid == user_id
        ]
        for key in keys:
            client = self._clients.pop(key, None)
            if client is not None:
                await client.stop()

    async def disconnect_all(self) -> None:
        """Stop all clients. Called on bot shutdown."""
        keys = list(self._clients.keys())
        for key in keys:
            client = self._clients.pop(key, None)
            if client is not None:
                await client.stop()

    async def update_session_id(
        self,
        user_id: int,
        chat_id: int,
        message_thread_id: int,
        directory: str,
        session_id: str,
    ) -> None:
        """Update session ID after receiving a ResultMessage."""
        client = self._clients.get((user_id, chat_id, message_thread_id))
        if client is not None:
            client.session_id = session_id
        await self._chat_session_repo.upsert(
            chat_id, message_thread_id, user_id, directory, session_id
        )
