# Multimodal Attachments Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Send files and images from Telegram to Claude as native Anthropic API content blocks (images, PDFs, text documents) instead of text descriptions.

**Architecture:** Replace `FileHandler` + `ImageHandler` with a single `AttachmentProcessor` and `MediaGroupCollector`. Unify `agentic_document()` + `agentic_photo()` into one `agentic_attachment()` handler. Change `UserClient.submit()` to accept a `Query` dataclass and always use the SDK's `AsyncIterable[dict]` prompt path.

**Tech Stack:** Python 3.12+, python-telegram-bot, claude-agent-sdk (AsyncIterable prompt path), pytest-asyncio

**Design doc:** `docs/plans/2026-02-28-multimodal-attachments-design.md`

---

### Task 1: Create `Query` and `Attachment` dataclasses

**Files:**
- Create: `src/bot/attachments.py`
- Test: `tests/unit/test_attachments.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_attachments.py
"""Tests for multimodal attachment processing."""

import pytest

from src.bot.attachments import Attachment, Query, UnsupportedAttachmentError


class TestQuery:
    def test_text_only(self):
        q = Query(text="hello")
        assert q.text == "hello"
        assert q.attachments == ()

    def test_with_attachments(self):
        att = Attachment(
            content_block={"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
            filename="test.png",
            size=100,
            media_type="image/png",
        )
        q = Query(text="analyze", attachments=(att,))
        assert len(q.attachments) == 1
        assert q.attachments[0].filename == "test.png"

    def test_immutable(self):
        q = Query(text="hello")
        with pytest.raises(AttributeError):
            q.text = "changed"

    def test_default_no_text(self):
        att = Attachment(
            content_block={"type": "text", "text": "data"},
            filename="f.txt",
            size=10,
            media_type="text/plain",
        )
        q = Query(attachments=(att,))
        assert q.text is None

    def test_to_content_blocks_text_only(self):
        q = Query(text="hello")
        blocks = q.to_content_blocks()
        assert blocks == [{"type": "text", "text": "hello"}]

    def test_to_content_blocks_with_attachments(self):
        att = Attachment(
            content_block={"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
            filename="img.png",
            size=100,
            media_type="image/png",
        )
        q = Query(text="look", attachments=(att,))
        blocks = q.to_content_blocks()
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "look"}
        assert blocks[1]["type"] == "image"

    def test_to_content_blocks_attachments_only(self):
        att = Attachment(
            content_block={"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
            filename="img.png",
            size=100,
            media_type="image/png",
        )
        q = Query(attachments=(att,))
        blocks = q.to_content_blocks()
        assert len(blocks) == 1
        assert blocks[0]["type"] == "image"


class TestUnsupportedAttachmentError:
    def test_attributes(self):
        err = UnsupportedAttachmentError("file.xlsx", "application/vnd.ms-excel")
        assert err.filename == "file.xlsx"
        assert err.mime_type == "application/vnd.ms-excel"
        assert "file.xlsx" in str(err)
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_attachments.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.bot.attachments'`

**Step 3: Write minimal implementation**

```python
# src/bot/attachments.py
"""Multimodal attachment processing for Telegram -> Claude.

Converts Telegram photos, documents, and albums into Anthropic API
content blocks (image, document) for native multimodal queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Attachment:
    """A processed attachment ready for the Anthropic API."""

    content_block: dict[str, Any]
    filename: str
    size: int
    media_type: str


@dataclass(frozen=True)
class Query:
    """A query to submit to Claude, with optional attachments.

    Used by both plain text messages and multimodal messages.
    """

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
    """Raised when an attachment type cannot be converted to a content block."""

    def __init__(self, filename: str, mime_type: str | None) -> None:
        self.filename = filename
        self.mime_type = mime_type
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "unknown"
        super().__init__(
            f"Can't process .{ext} files. Try sending as PDF or pasting the content as text."
        )
```

**Step 4: Run test to verify it passes**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_attachments.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bot/attachments.py tests/unit/test_attachments.py
git commit -m "feat: add Query, Attachment, and UnsupportedAttachmentError dataclasses"
```

---

### Task 2: Implement `AttachmentProcessor`

**Files:**
- Modify: `src/bot/attachments.py`
- Modify: `tests/unit/test_attachments.py`

**Step 1: Write the failing tests**

Add to `tests/unit/test_attachments.py`:

```python
import base64
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.attachments import AttachmentProcessor


def _make_telegram_file(content: bytes) -> AsyncMock:
    """Create a mock Telegram File that returns given bytes."""
    file = AsyncMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(content))
    return file


def _make_photo_message(image_bytes: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100) -> MagicMock:
    """Create a mock Telegram Message with a photo."""
    photo_size = MagicMock()
    photo_size.file_size = len(image_bytes)
    photo_size.file_id = "photo_123"
    photo_size.get_file = AsyncMock(return_value=_make_telegram_file(image_bytes))

    message = MagicMock()
    message.photo = [MagicMock(), photo_size]  # Telegram sends multiple sizes, we pick last
    message.document = None
    message.caption = None
    return message


def _make_document_message(
    content: bytes,
    filename: str,
    mime_type: str | None = None,
) -> MagicMock:
    """Create a mock Telegram Message with a document."""
    doc = MagicMock()
    doc.file_name = filename
    doc.file_size = len(content)
    doc.mime_type = mime_type
    doc.get_file = AsyncMock(return_value=_make_telegram_file(content))

    message = MagicMock()
    message.photo = None
    message.document = doc
    message.caption = None
    return message


class TestAttachmentProcessorPhoto:
    @pytest.mark.asyncio
    async def test_photo_creates_image_block(self):
        processor = AttachmentProcessor()
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        message = _make_photo_message(image_bytes)

        att = await processor.process(message)

        assert att.content_block["type"] == "image"
        assert att.content_block["source"]["type"] == "base64"
        assert att.content_block["source"]["media_type"] == "image/png"
        expected_b64 = base64.b64encode(image_bytes).decode("utf-8")
        assert att.content_block["source"]["data"] == expected_b64
        assert att.media_type == "image/png"

    @pytest.mark.asyncio
    async def test_photo_jpeg_detection(self):
        processor = AttachmentProcessor()
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        message = _make_photo_message(jpeg_bytes)

        att = await processor.process(message)

        assert att.content_block["source"]["media_type"] == "image/jpeg"


class TestAttachmentProcessorDocument:
    @pytest.mark.asyncio
    async def test_image_document_creates_image_block(self):
        processor = AttachmentProcessor()
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        message = _make_document_message(png_bytes, "screenshot.png", "image/png")

        att = await processor.process(message)

        assert att.content_block["type"] == "image"
        assert att.filename == "screenshot.png"

    @pytest.mark.asyncio
    async def test_pdf_creates_document_block(self):
        processor = AttachmentProcessor()
        pdf_bytes = b"%PDF-1.4" + b"\x00" * 100
        message = _make_document_message(pdf_bytes, "report.pdf", "application/pdf")

        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "base64"
        assert att.content_block["source"]["media_type"] == "application/pdf"
        assert att.content_block.get("title") == "report.pdf"
        assert att.media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_text_file_creates_text_document_block(self):
        processor = AttachmentProcessor()
        content = b"import os\nprint('hello')\n"
        message = _make_document_message(content, "main.py", "text/x-python")

        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"
        assert att.content_block["source"]["media_type"] == "text/plain"
        assert att.content_block["source"]["data"] == content.decode("utf-8")
        assert att.content_block.get("title") == "main.py"

    @pytest.mark.asyncio
    async def test_csv_detected_as_text(self):
        processor = AttachmentProcessor()
        content = b"name,age\nAlice,30\nBob,25\n"
        message = _make_document_message(content, "data.csv", "text/csv")

        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"

    @pytest.mark.asyncio
    async def test_unknown_text_extension_detected(self):
        processor = AttachmentProcessor()
        content = b"some config value = true\n"
        message = _make_document_message(content, "config.ini", None)

        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"

    @pytest.mark.asyncio
    async def test_unknown_binary_detected_via_utf8_attempt(self):
        processor = AttachmentProcessor()
        # Valid UTF-8 content with no MIME type and unknown extension
        content = b"valid utf8 content here"
        message = _make_document_message(content, "data.weird", None)

        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"

    @pytest.mark.asyncio
    async def test_binary_file_rejected(self):
        processor = AttachmentProcessor()
        binary_bytes = bytes(range(256)) * 10  # non-UTF-8 binary
        message = _make_document_message(binary_bytes, "file.xlsx", "application/vnd.ms-excel")

        with pytest.raises(UnsupportedAttachmentError) as exc_info:
            await processor.process(message)

        assert "xlsx" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_no_photo_no_document_raises(self):
        processor = AttachmentProcessor()
        message = MagicMock()
        message.photo = None
        message.document = None

        with pytest.raises(ValueError, match="no photo or document"):
            await processor.process(message)
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_attachments.py::TestAttachmentProcessorPhoto -v`
Expected: FAIL — `ImportError: cannot import name 'AttachmentProcessor'`

**Step 3: Implement `AttachmentProcessor`**

Add to `src/bot/attachments.py`:

```python
import base64

import structlog
from telegram import Message

logger = structlog.get_logger()

# Known text file extensions (no leading dot)
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    "py", "js", "ts", "jsx", "tsx", "java", "cpp", "c", "h", "hpp", "cs",
    "go", "rs", "rb", "php", "swift", "kt", "scala", "r", "jl", "lua",
    "pl", "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd",
    "md", "txt", "rst", "adoc",
    "json", "yml", "yaml", "toml", "xml", "ini", "cfg", "conf", "env",
    "html", "css", "scss", "sass", "less", "vue", "svelte",
    "csv", "tsv", "log", "sql",
    "dockerfile", "makefile", "cmake",
    "lock", "gitignore", "gitattributes", "editorconfig",
})

# Image magic byte prefixes
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # WebP starts with RIFF....WEBP
)


def _detect_image_media_type(data: bytes) -> str | None:
    """Detect image media type from magic bytes."""
    for sig, media_type in _IMAGE_SIGNATURES:
        if data[:len(sig)] == sig:
            return media_type
    return None


def _file_extension(filename: str) -> str:
    """Extract lowercase extension without dot."""
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return filename.lower()  # e.g. "Makefile" -> "makefile"


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
        """Process a Telegram photo (always an image)."""
        photo = message.photo[-1]  # largest resolution
        file = await photo.get_file()
        data = bytes(await file.download_as_bytearray())

        media_type = _detect_image_media_type(data) or "image/jpeg"
        b64 = base64.b64encode(data).decode("utf-8")

        return Attachment(
            content_block={
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            },
            filename=f"photo_{photo.file_id}.jpg",
            size=len(data),
            media_type=media_type,
        )

    async def _process_document(self, message: Message) -> Attachment:
        """Process a Telegram document (image, PDF, text, or reject)."""
        doc = message.document
        file = await doc.get_file()
        data = bytes(await file.download_as_bytearray())
        filename = doc.file_name or "unnamed"
        mime = doc.mime_type or ""

        # 1. Image document
        if mime.startswith("image/") or _detect_image_media_type(data):
            media_type = _detect_image_media_type(data) or mime
            b64 = base64.b64encode(data).decode("utf-8")
            return Attachment(
                content_block={
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                filename=filename,
                size=len(data),
                media_type=media_type,
            )

        # 2. PDF document
        if mime == "application/pdf" or data[:5] == b"%PDF-":
            b64 = base64.b64encode(data).decode("utf-8")
            return Attachment(
                content_block={
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                    "title": filename,
                },
                filename=filename,
                size=len(data),
                media_type="application/pdf",
            )

        # 3. Text document — by MIME, by extension, or by UTF-8 decode attempt
        ext = _file_extension(filename)
        if mime.startswith("text/") or ext in _TEXT_EXTENSIONS:
            text_content = data.decode("utf-8", errors="replace")
            return Attachment(
                content_block={
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": text_content},
                    "title": filename,
                },
                filename=filename,
                size=len(data),
                media_type="text/plain",
            )

        # 4. Unknown — try UTF-8 decode as last resort
        try:
            text_content = data.decode("utf-8")
            return Attachment(
                content_block={
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": text_content},
                    "title": filename,
                },
                filename=filename,
                size=len(data),
                media_type="text/plain",
            )
        except UnicodeDecodeError:
            pass

        # 5. Unsupported binary
        raise UnsupportedAttachmentError(filename, mime or None)
```

**Step 4: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_attachments.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/bot/attachments.py tests/unit/test_attachments.py
git commit -m "feat: implement AttachmentProcessor with image/PDF/text detection"
```

---

### Task 3: Implement `MediaGroupCollector`

**Files:**
- Modify: `src/bot/attachments.py`
- Modify: `tests/unit/test_attachments.py`

**Step 1: Write the failing tests**

Add to `tests/unit/test_attachments.py`:

```python
import asyncio

from src.bot.attachments import MediaGroupCollector


def _make_update(media_group_id: str | None = None, message_id: int = 1) -> MagicMock:
    """Create a mock Telegram Update."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.media_group_id = media_group_id
    update.message.message_id = message_id
    update.message.caption = None
    return update


class TestMediaGroupCollector:
    @pytest.mark.asyncio
    async def test_single_message_returns_immediately(self):
        collector = MediaGroupCollector()
        update = _make_update(media_group_id=None)

        result = await collector.add(update)

        assert result is not None
        assert len(result) == 1
        assert result[0] is update

    @pytest.mark.asyncio
    async def test_album_buffers_and_returns(self):
        collector = MediaGroupCollector(timeout=0.1)
        u1 = _make_update(media_group_id="group_1", message_id=1)
        u2 = _make_update(media_group_id="group_1", message_id=2)
        u3 = _make_update(media_group_id="group_1", message_id=3)

        r1 = await collector.add(u1)
        r2 = await collector.add(u2)
        r3 = await collector.add(u3)

        assert r1 is None
        assert r2 is None
        assert r3 is None

        # Wait for timeout to fire
        await asyncio.sleep(0.2)
        # Collector should have produced a result via callback
        result = collector.pop_ready("group_1")
        assert result is not None
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_different_groups_independent(self):
        collector = MediaGroupCollector(timeout=0.1)
        u1 = _make_update(media_group_id="group_a", message_id=1)
        u2 = _make_update(media_group_id="group_b", message_id=2)

        r1 = await collector.add(u1)
        r2 = await collector.add(u2)

        assert r1 is None
        assert r2 is None

        await asyncio.sleep(0.2)

        result_a = collector.pop_ready("group_a")
        result_b = collector.pop_ready("group_b")
        assert result_a is not None and len(result_a) == 1
        assert result_b is not None and len(result_b) == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_attachments.py::TestMediaGroupCollector -v`
Expected: FAIL — `ImportError: cannot import name 'MediaGroupCollector'`

**Step 3: Implement `MediaGroupCollector`**

Add to `src/bot/attachments.py`:

```python
from telegram import Update


class MediaGroupCollector:
    """Buffer Telegram album items and group them by media_group_id.

    Non-album messages (no media_group_id) are returned immediately
    as a single-item list. Album items are buffered until no new items
    arrive for `timeout` seconds, then marked as ready.
    """

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

        # Cancel existing timer and start a new one
        if group_id in self._timers:
            self._timers[group_id].cancel()

        self._timers[group_id] = asyncio.create_task(
            self._fire_timeout(group_id)
        )

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
```

**Step 4: Run tests to verify they pass**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_attachments.py::TestMediaGroupCollector -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/bot/attachments.py tests/unit/test_attachments.py
git commit -m "feat: implement MediaGroupCollector for Telegram album buffering"
```

---

### Task 4: Update `UserClient` to accept `Query`

**Files:**
- Modify: `src/claude/user_client.py`
- Modify: `tests/unit/test_user_client.py` (if exists, else create)

**Step 1: Write the failing test**

Add a test that verifies `UserClient.submit()` accepts a `Query` object and calls the SDK with the async iterable path. This requires mocking the SDK client.

```python
# In tests/unit/test_user_client.py — add or find existing test class

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.bot.attachments import Attachment, Query
from src.claude.user_client import UserClient, WorkItem


class TestUserClientMultimodal:
    """Test that UserClient handles Query objects correctly."""

    def test_work_item_accepts_query(self):
        """WorkItem should store a Query instead of a string prompt."""
        q = Query(text="hello")
        import asyncio
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        item = WorkItem(query=q, future=future)
        assert item.query.text == "hello"
        loop.close()
```

**Step 2: Run test to verify it fails**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_user_client.py::TestUserClientMultimodal -v`
Expected: FAIL — `WorkItem` still expects `prompt: str`

**Step 3: Modify `UserClient`**

Changes to `src/claude/user_client.py`:

1. Replace `WorkItem.prompt: str` with `WorkItem.query: Query`
2. Change `submit()` signature from `prompt: str` to `query: Query`
3. In `_process_item()`, build content blocks from `query.to_content_blocks()` and create an async generator to pass to `self._sdk_client.query()`

Key changes:

```python
# Import at top
from src.bot.attachments import Query

# WorkItem dataclass
@dataclass
class WorkItem:
    query: Query
    future: asyncio.Future[Any]
    on_stream: Optional[Callable[..., Any]] = None

# submit method
async def submit(
    self,
    query: Query,
    on_stream: Optional[Callable[..., Any]] = None,
) -> QueryResult:
    if not self._running:
        raise RuntimeError("UserClient is not running. Call start() first.")
    loop = asyncio.get_running_loop()
    future: asyncio.Future[QueryResult] = loop.create_future()
    await self._queue.put(
        WorkItem(query=query, on_stream=on_stream, future=future)
    )
    return await future

# _process_item — replace line 224:
#   await self._sdk_client.query(item.prompt)
# with:
    content_blocks = item.query.to_content_blocks()

    async def _prompt_iter():
        yield {
            "type": "user",
            "message": {"role": "user", "content": content_blocks},
            "parent_tool_use_id": None,
        }

    await self._sdk_client.query(_prompt_iter())
```

**Step 4: Run tests**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/unit/test_user_client.py -v`
Expected: PASS

**Step 5: Run full test suite to check for regressions**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/ -x -q`
Expected: Many tests will fail because callers still pass `prompt: str`. That's expected — we fix callers in Task 5.

**Step 6: Commit**

```bash
git add src/claude/user_client.py tests/unit/test_user_client.py
git commit -m "feat: UserClient.submit() accepts Query with content blocks"
```

---

### Task 5: Update orchestrator — unified `agentic_attachment()` handler

**Files:**
- Modify: `src/bot/orchestrator.py`

**Step 1: Update `_run_claude_query()` to accept `Query`**

Change `_run_claude_query()` signature from `prompt: str` to `query: Query`. Update the line that calls `client.submit(prompt, on_stream=on_stream)` to `client.submit(query, on_stream=on_stream)`. Update the classic-mode fallback to extract `query.text` for `claude_integration.run_command()`.

**Step 2: Update `agentic_text()` to wrap message in `Query`**

Change line ~960 from:
```python
claude_response = await self._run_claude_query(
    prompt=message_text,
    ...
)
```
to:
```python
from .attachments import Query
claude_response = await self._run_claude_query(
    query=Query(text=message_text),
    ...
)
```

Also update the two calls in `agentic_compact()` (~lines 696, 717).

**Step 3: Replace `agentic_document()` and `agentic_photo()` with `agentic_attachment()`**

New method in `MessageOrchestrator`:

```python
async def agentic_attachment(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Unified handler for photos, documents, and albums."""
    from .attachments import (
        AttachmentProcessor,
        MediaGroupCollector,
        Query,
        UnsupportedAttachmentError,
    )

    user_id = update.effective_user.id

    # Buffer albums; single items return immediately as [update]
    group = await self._media_collector.add(update)
    if group is None:
        return  # still buffering album items

    processor = AttachmentProcessor()
    attachments = []
    caption = None
    errors = []

    for item in group:
        if not caption and item.message.caption:
            caption = item.message.caption
        try:
            att = await processor.process(item.message)
            attachments.append(att)
        except UnsupportedAttachmentError as e:
            errors.append(str(e))
        except Exception as e:
            logger.error("attachment_processing_failed", error=str(e), user_id=user_id)
            errors.append(f"Failed to process attachment: {e}")

    # Report any unsupported files
    if errors:
        error_text = "\n".join(errors)
        await update.message.reply_text(error_text)

    if not attachments:
        return  # all items were unsupported

    query = Query(text=caption or "Analyze this.", attachments=tuple(attachments))

    logger.info(
        "agentic_attachment",
        user_id=user_id,
        num_attachments=len(attachments),
        types=[a.media_type for a in attachments],
    )

    # From here, same flow as agentic_text() — session resolution, progress, query
    # ... (extract shared logic or inline)
```

**Step 4: Update handler registration in `_register_agentic_handlers()`**

Replace the separate photo and document handlers:

```python
# Old:
# app.add_handler(MessageHandler(filters.Document.ALL, ...agentic_document))
# app.add_handler(MessageHandler(filters.PHOTO, ...agentic_photo))

# New:
app.add_handler(
    MessageHandler(
        filters.PHOTO | filters.Document.ALL,
        self._inject_deps(self.agentic_attachment),
    ),
    group=10,
)
```

**Step 5: Initialize `MediaGroupCollector` on orchestrator**

In `MessageOrchestrator.__init__()`, add:
```python
from .attachments import MediaGroupCollector
self._media_collector = MediaGroupCollector()
```

**Step 6: Run full test suite**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/ -x -q`
Expected: PASS (or known failures from unrelated tests)

**Step 7: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "feat: unified agentic_attachment() handler with Query pipeline"
```

---

### Task 6: Clean up old handlers and registry

**Files:**
- Delete: `src/bot/features/file_handler.py`
- Delete: `src/bot/features/image_handler.py`
- Modify: `src/bot/features/registry.py`
- Modify: `src/config/features.py`
- Modify: `src/config/settings.py` (if `enable_file_uploads` is there)

**Step 1: Remove `FileHandler` and `ImageHandler` from `FeatureRegistry`**

In `src/bot/features/registry.py`:
- Remove imports of `FileHandler` and `ImageHandler`
- Remove their initialization blocks in `_initialize_features()`
- Remove `get_file_handler()` and `get_image_handler()` methods

**Step 2: Delete old handler files**

```bash
rm src/bot/features/file_handler.py
rm src/bot/features/image_handler.py
```

**Step 3: Update feature flags**

In `src/config/features.py`:
- Rename `file_uploads_enabled` to `attachments_enabled` (or remove if no longer gating anything)

In `src/config/settings.py`:
- Rename `enable_file_uploads` to `enable_attachments` if it exists, or just remove it

**Step 4: Remove stale references**

Search for any remaining references to `file_handler`, `image_handler`, `FileHandler`, `ImageHandler`, `get_file_handler`, `get_image_handler`, `agentic_document`, `agentic_photo` in the codebase and clean them up.

Run: `grep -rn "file_handler\|image_handler\|FileHandler\|ImageHandler\|agentic_document\|agentic_photo" src/`

**Step 5: Remove or update old tests**

Remove tests that test the old `FileHandler` and `ImageHandler` classes. Check:
```bash
grep -rn "FileHandler\|ImageHandler\|file_handler\|image_handler" tests/
```

**Step 6: Run full test suite**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/ -x -q`
Expected: PASS

**Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove FileHandler and ImageHandler, simplify feature registry"
```

---

### Task 7: Refactor orchestrator to reduce duplication

**Files:**
- Modify: `src/bot/orchestrator.py`

**Step 1: Extract shared query flow**

Both `agentic_text()` and `agentic_attachment()` share the same post-query logic: session resolution, directory restoration, progress manager setup, `_run_claude_query()` call, response formatting, error handling. Extract into a shared `_execute_query()` helper.

```python
async def _execute_query(
    self,
    query: Query,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Shared query execution: progress, session, Claude call, response."""
    # ... session resolution, progress setup, _run_claude_query, format, reply
```

Then `agentic_text()` becomes:
```python
async def agentic_text(self, update, context):
    # ... skill detection, directory sync ...
    query = Query(text=message_text)
    await self._execute_query(query, update, context)
```

And `agentic_attachment()` becomes:
```python
async def agentic_attachment(self, update, context):
    # ... album buffering, processing ...
    query = Query(text=caption, attachments=tuple(attachments))
    await self._execute_query(query, update, context)
```

**Step 2: Run full test suite**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/ -x -q`
Expected: PASS

**Step 3: Commit**

```bash
git add src/bot/orchestrator.py
git commit -m "refactor: extract shared _execute_query() to reduce duplication"
```

---

### Task 8: Lint, type-check, and final verification

**Step 1: Run formatter**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run black src tests && uv run isort src tests`

**Step 2: Run linter**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run flake8 src`

**Step 3: Run type checker**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run mypy src`

Fix any type errors.

**Step 4: Run full test suite with coverage**

Run: `cd /local/home/moxu/claude-coder/.worktrees/multimodal-attachments && uv run pytest tests/ -v --tb=short`

**Step 5: Commit any fixes**

```bash
git add -A
git commit -m "chore: lint, type-check, and formatting fixes"
```

---

## Execution Order Summary

| Task | What | Depends On |
|------|------|-----------|
| 1 | `Query`, `Attachment`, `UnsupportedAttachmentError` dataclasses | — |
| 2 | `AttachmentProcessor` implementation | Task 1 |
| 3 | `MediaGroupCollector` implementation | Task 1 |
| 4 | `UserClient.submit()` accepts `Query` | Task 1 |
| 5 | Unified `agentic_attachment()` handler | Tasks 2, 3, 4 |
| 6 | Remove old `FileHandler`/`ImageHandler` | Task 5 |
| 7 | Extract shared `_execute_query()` | Task 5 |
| 8 | Lint, type-check, final verification | Tasks 6, 7 |

Tasks 2, 3, and 4 are independent of each other and can be implemented in parallel.
