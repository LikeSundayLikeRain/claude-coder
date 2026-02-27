"""Manages persistent UserClient instances per user with lifecycle management."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import structlog

from src.claude.options import OptionsBuilder
from src.claude.session import SessionResolver
from src.claude.user_client import UserClient
from src.storage.models import BotSessionModel
from src.storage.repositories import BotSessionRepository

logger = structlog.get_logger()

DEFAULT_IDLE_TIMEOUT_SECONDS = 3600  # 1 hour


class ClientManager:
    """Owns persistent UserClient instances, one per active user."""

    def __init__(
        self,
        bot_session_repo: BotSessionRepository,
        options_builder: Optional[OptionsBuilder] = None,
        history_path: Optional[Path] = None,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._bot_session_repo = bot_session_repo
        self._options_builder = options_builder or OptionsBuilder()
        self._session_resolver = SessionResolver(history_path=history_path)
        self._idle_timeout = idle_timeout
        self._clients: dict[int, UserClient] = {}
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    async def get_or_connect(
        self,
        user_id: int,
        directory: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
        approved_directory: Optional[str] = None,
        force_new: bool = False,
    ) -> UserClient:
        """Get existing client or create+connect a new one."""
        existing = self._clients.get(user_id)

        # Reuse if same directory, still connected, and not forcing new
        if (
            existing is not None
            and existing.is_connected
            and existing.directory == directory
            and not force_new
        ):
            return existing

        # Directory changed or force_new — disconnect old
        if existing is not None:
            await existing.disconnect()

        # Resolve session_id: explicit > persisted > history.jsonl
        # Skip auto-resolution when force_new (start a fresh session)
        resolved_session_id = session_id
        resolved_model = model
        resolved_betas = betas

        if resolved_session_id is None and not force_new:
            persisted: Optional[BotSessionModel] = (
                await self._bot_session_repo.get_by_user(user_id)
            )
            if persisted is not None and persisted.directory == directory:
                resolved_session_id = persisted.session_id
                if resolved_model is None:
                    resolved_model = persisted.model
                if resolved_betas is None:
                    resolved_betas = persisted.betas

        if resolved_session_id is None and not force_new:
            resolved_session_id = self._session_resolver.get_latest_session(directory)

        # Create and connect
        client = UserClient(
            user_id=user_id,
            directory=directory,
            session_id=resolved_session_id,
            model=resolved_model,
        )
        options = self._options_builder.build(
            cwd=directory,
            session_id=resolved_session_id,
            model=resolved_model,
            betas=resolved_betas,
            approved_directory=approved_directory,
        )
        await client.connect(options)
        self._clients[user_id] = client

        # Persist state if we have a session_id
        if client.session_id is not None:
            await self._bot_session_repo.upsert(
                user_id=user_id,
                session_id=client.session_id,
                directory=directory,
                model=resolved_model,
                betas=resolved_betas,
            )

        logger.info(
            "client_manager_connected",
            user_id=user_id,
            directory=directory,
            session_id=client.session_id,
        )
        return client

    async def switch_session(
        self,
        user_id: int,
        session_id: str,
        directory: str,
        model: Optional[str] = None,
        betas: Optional[list[str]] = None,
        approved_directory: Optional[str] = None,
    ) -> UserClient:
        """Disconnect current, connect to different session."""
        existing = self._clients.pop(user_id, None)
        if existing is not None:
            await existing.disconnect()

        return await self.get_or_connect(
            user_id=user_id,
            directory=directory,
            session_id=session_id,
            model=model,
            betas=betas,
            approved_directory=approved_directory,
        )

    async def interrupt(self, user_id: int) -> None:
        """Interrupt active query for the given user."""
        client = self._clients.get(user_id)
        if client is not None:
            await client.interrupt()

    async def set_model(
        self,
        user_id: int,
        model: str,
        betas: Optional[list[str]] = None,
    ) -> None:
        """Update model on the client and persist."""
        client = self._clients.get(user_id)
        if client is None:
            return
        client.model = model
        if betas is not None:
            client.betas = betas
        if client.session_id is not None:
            await self._bot_session_repo.upsert(
                user_id=user_id,
                session_id=client.session_id,
                directory=client.directory,
                model=model,
                betas=client.betas,
            )

    def get_active_client(self, user_id: int) -> Optional["UserClient"]:
        """Return the active UserClient for a user, or None."""
        return self._clients.get(user_id)

    async def disconnect(self, user_id: int) -> None:
        """Disconnect and remove user's client."""
        client = self._clients.pop(user_id, None)
        if client is not None:
            await client.disconnect()

    async def disconnect_all(self) -> None:
        """Disconnect all clients. Called on bot shutdown."""
        user_ids = list(self._clients.keys())
        for user_id in user_ids:
            await self.disconnect(user_id)

    async def update_session_id(self, user_id: int, session_id: str) -> None:
        """Update session ID after receiving a ResultMessage."""
        client = self._clients.get(user_id)
        if client is None:
            return
        client.session_id = session_id
        await self._bot_session_repo.upsert(
            user_id=user_id,
            session_id=session_id,
            directory=client.directory,
            model=client.model,
        )

    def get_latest_session(self, directory: str) -> Optional[str]:
        """Return the most recent session ID for a directory, or None."""
        return self._session_resolver.get_latest_session(directory)

    def list_sessions(
        self,
        directory: str,
        limit: int = 10,
    ) -> list:
        """Delegate to session_resolver."""
        return self._session_resolver.list_sessions(directory=directory, limit=limit)

    def start_cleanup_loop(self) -> None:
        """Start background idle-cleanup task."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("client_manager_cleanup_loop_started")

    def stop_cleanup_loop(self) -> None:
        """Stop background cleanup."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """Periodically disconnect idle clients."""
        check_interval = max(60, self._idle_timeout // 10)
        while True:
            await asyncio.sleep(check_interval)
            await self._cleanup_idle()

    async def _cleanup_idle(self) -> None:
        """Disconnect clients that have been idle longer than idle_timeout."""
        now = datetime.now(UTC)
        to_disconnect = []
        for user_id, client in self._clients.items():
            if client.is_querying:
                continue
            idle_seconds = (now - client.last_active).total_seconds()
            if idle_seconds > self._idle_timeout:
                to_disconnect.append(user_id)

        for user_id in to_disconnect:
            logger.info("client_manager_idle_disconnect", user_id=user_id)
            await self.disconnect(user_id)
