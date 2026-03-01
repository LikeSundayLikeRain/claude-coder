from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any

import structlog
from telegram import Message, Update

logger = structlog.get_logger(__name__)

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        "py",
        "js",
        "ts",
        "jsx",
        "tsx",
        "java",
        "cpp",
        "c",
        "h",
        "hpp",
        "cs",
        "go",
        "rs",
        "rb",
        "php",
        "swift",
        "kt",
        "scala",
        "r",
        "jl",
        "lua",
        "pl",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "cmd",
        "md",
        "txt",
        "rst",
        "adoc",
        "json",
        "yml",
        "yaml",
        "toml",
        "xml",
        "ini",
        "cfg",
        "conf",
        "env",
        "html",
        "css",
        "scss",
        "sass",
        "less",
        "vue",
        "svelte",
        "csv",
        "tsv",
        "log",
        "sql",
        "dockerfile",
        "makefile",
        "cmake",
        "lock",
        "gitignore",
        "gitattributes",
        "editorconfig",
    }
)


def _detect_image_media_type(data: bytes) -> str | None:
    """Check magic bytes to detect image media type. Returns None if not an image."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    return None


def _file_extension(filename: str) -> str:
    """Extract lowercase extension without leading dot. Returns empty string if none."""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


@dataclass(frozen=True)
class Attachment:
    content_block: dict[str, Any]  # Anthropic API content block
    filename: str
    size: int
    media_type: str


@dataclass(frozen=True)
class Query:
    text: str | None = None
    attachments: tuple[Attachment, ...] = ()

    def to_content_blocks(self) -> list[dict[str, Any]]:
        """Build Anthropic API content blocks from this query."""
        blocks: list[dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for att in self.attachments:
            blocks.append(att.content_block)
        return blocks


class UnsupportedAttachmentError(Exception):
    def __init__(self, filename: str, mime_type: str | None) -> None:
        self.filename = filename
        self.mime_type = mime_type
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "unknown"
        super().__init__(
            f"Can't process .{ext} files. Try sending as PDF or pasting the content as text."
        )


class AttachmentProcessor:
    """Convert Telegram photos and documents to Anthropic API content blocks."""

    async def process(self, message: Message) -> Attachment:
        """Process a Telegram message's photo or document into an Attachment."""
        if message.photo:
            return await self._process_photo(message)
        if message.document:
            return await self._process_document(message)
        raise ValueError("Message contains no photo or document")

    async def _process_photo(self, message: Message) -> Attachment:
        """Process a Telegram photo (picks the largest size)."""
        # photo is a tuple of PhotoSize from smallest to largest
        photo_size = message.photo[-1]
        tg_file = await photo_size.get_file()
        data = bytes(await tg_file.download_as_bytearray())

        media_type = _detect_image_media_type(data) or "image/jpeg"
        encoded = base64.standard_b64encode(data).decode()
        filename = f"photo.{media_type.split('/')[-1]}"

        logger.debug(
            "processed_photo",
            filename=filename,
            media_type=media_type,
            size=len(data),
        )

        content_block: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            },
        }
        return Attachment(
            content_block=content_block,
            filename=filename,
            size=len(data),
            media_type=media_type,
        )

    async def _process_document(self, message: Message) -> Attachment:
        """Process a Telegram document into an image or text/PDF document block."""
        assert message.document is not None  # caller guarantees this
        doc = message.document
        filename: str = doc.file_name or "document"
        mime_type: str | None = doc.mime_type

        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())

        # 1. Check magic bytes for images first (overrides MIME)
        detected_image_type = _detect_image_media_type(data)
        if detected_image_type:
            encoded = base64.standard_b64encode(data).decode()
            content_block: dict[str, Any] = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": detected_image_type,
                    "data": encoded,
                },
            }
            logger.debug(
                "processed_image_document",
                filename=filename,
                media_type=detected_image_type,
                size=len(data),
            )
            return Attachment(
                content_block=content_block,
                filename=filename,
                size=len(data),
                media_type=detected_image_type,
            )

        # 2. MIME says image
        if mime_type and mime_type.startswith("image/"):
            encoded = base64.standard_b64encode(data).decode()
            content_block = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": encoded,
                },
            }
            logger.debug(
                "processed_image_document_by_mime",
                filename=filename,
                media_type=mime_type,
                size=len(data),
            )
            return Attachment(
                content_block=content_block,
                filename=filename,
                size=len(data),
                media_type=mime_type,
            )

        # 3. PDF: magic bytes or MIME
        if (mime_type == "application/pdf") or data.startswith(b"%PDF-"):
            encoded = base64.standard_b64encode(data).decode()
            content_block = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": encoded,
                },
                "title": filename,
            }
            logger.debug(
                "processed_pdf_document",
                filename=filename,
                size=len(data),
            )
            return Attachment(
                content_block=content_block,
                filename=filename,
                size=len(data),
                media_type="application/pdf",
            )

        # 4. Text MIME or known text extension
        ext = _file_extension(filename)
        is_text_mime = mime_type is not None and (
            mime_type.startswith("text/") or mime_type == "application/json"
        )
        is_text_ext = ext in _TEXT_EXTENSIONS

        if is_text_mime or is_text_ext:
            text_content = data.decode("utf-8")
            content_block = {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": text_content,
                },
                "title": filename,
            }
            logger.debug(
                "processed_text_document",
                filename=filename,
                mime_type=mime_type,
                ext=ext,
                size=len(data),
            )
            return Attachment(
                content_block=content_block,
                filename=filename,
                size=len(data),
                media_type=mime_type or "text/plain",
            )

        # 5. Last resort: try UTF-8 decode
        try:
            text_content = data.decode("utf-8")
            content_block = {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": text_content,
                },
                "title": filename,
            }
            logger.debug(
                "processed_unknown_as_text",
                filename=filename,
                mime_type=mime_type,
                size=len(data),
            )
            return Attachment(
                content_block=content_block,
                filename=filename,
                size=len(data),
                media_type="text/plain",
            )
        except UnicodeDecodeError:
            logger.warning(
                "unsupported_binary_attachment",
                filename=filename,
                mime_type=mime_type,
            )
            raise UnsupportedAttachmentError(filename, mime_type)


class MediaGroupCollector:
    """Buffer Telegram album items and group them by media_group_id."""

    def __init__(self, timeout: float = 1.0) -> None:
        self._timeout = timeout
        self._pending: dict[str, list[Update]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, list[Update]] = {}

    async def add(self, update: Update) -> list[Update] | None:
        """Add an update. Returns immediately for non-album messages.

        For album messages, returns None while buffering. Use pop_ready()
        after the timeout to retrieve the complete group.
        """
        group_id = update.message.media_group_id if update.message else None
        if group_id is None:
            return [update]

        if group_id not in self._pending:
            self._pending[group_id] = []

        self._pending[group_id].append(update)

        # Cancel existing timer and start a fresh one (sliding window)
        existing = self._timers.get(group_id)
        if existing is not None and not existing.done():
            existing.cancel()

        self._timers[group_id] = asyncio.create_task(self._fire_timeout(group_id))
        return None

    async def _fire_timeout(self, group_id: str) -> None:
        """Wait for timeout, then move pending group to ready."""
        await asyncio.sleep(self._timeout)
        if group_id in self._pending:
            self._ready[group_id] = self._pending.pop(group_id)
            self._timers.pop(group_id, None)

    def pop_ready(self, group_id: str) -> list[Update] | None:
        """Pop a completed group if ready, else None."""
        return self._ready.pop(group_id, None)
