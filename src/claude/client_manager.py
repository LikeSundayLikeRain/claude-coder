"""Manages persistent UserClient instances per user with lifecycle management."""

from __future__ import annotations

import asyncio
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
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._bot_session_repo = bot_session_repo
        self._options_builder = options_builder or OptionsBuilder()
        self._session_resolver = SessionResolver(history_path=history_path)
        self._idle_timeout = idle_timeout
        self._clients: dict[int, UserClient] = {}

    def _on_client_exit(self, user_id: int) -> None:
        """Called by actor when it exits (idle timeout or error)."""
        self._clients.pop(user_id, None)
        logger.info("client_manager_actor_exited", user_id=user_id)

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

        # Directory changed or force_new — stop old
        if existing is not None:
            await existing.stop()

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

        # Create and start
        client = UserClient(
            user_id=user_id,
            directory=directory,
            session_id=resolved_session_id,
            model=resolved_model,
            idle_timeout=self._idle_timeout,
            on_exit=self._on_client_exit,
        )
        options = self._options_builder.build(
            cwd=directory,
            session_id=resolved_session_id,
            model=resolved_model,
            betas=resolved_betas,
            approved_directory=approved_directory,
        )
        await client.start(options)
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
        """Stop current, connect to different session."""
        existing = self._clients.pop(user_id, None)
        if existing is not None:
            await existing.stop()

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
        """Stop and remove user's client."""
        client = self._clients.pop(user_id, None)
        if client is not None:
            await client.stop()

    async def disconnect_all(self) -> None:
        """Stop all clients. Called on bot shutdown."""
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
