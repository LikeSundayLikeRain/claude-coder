"""Claude Code integration layer."""

from .client_manager import ClientManager
from .exceptions import (
    ClaudeError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from .facade import ClaudeIntegration
from .options import OptionsBuilder
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import SessionResolver
from .stream_handler import StreamEvent, StreamHandler
from .user_client import UserClient

__all__ = [
    # New persistent client layer
    "ClientManager",
    "OptionsBuilder",
    "SessionResolver",
    "StreamEvent",
    "StreamHandler",
    "UserClient",
    # Exceptions
    "ClaudeError",
    "ClaudeParsingError",
    "ClaudeProcessError",
    "ClaudeSessionError",
    "ClaudeTimeoutError",
    # Legacy (classic mode + event handlers)
    "ClaudeIntegration",
    "ClaudeSDKManager",
    "ClaudeResponse",
    "StreamUpdate",
]
