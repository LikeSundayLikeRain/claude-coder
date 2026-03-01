from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.attachments import (
    Attachment,
    AttachmentProcessor,
    MediaGroupCollector,
    Query,
    UnsupportedAttachmentError,
)


def make_attachment(filename: str = "photo.jpg") -> Attachment:
    return Attachment(
        content_block={
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc123"},
        },
        filename=filename,
        size=1024,
        media_type="image/jpeg",
    )


class TestQuery:
    def test_text_only(self) -> None:
        q = Query(text="hello")
        assert q.text == "hello"
        assert q.attachments == ()

    def test_with_attachments(self) -> None:
        att = make_attachment()
        q = Query(text="look at this", attachments=(att,))
        assert q.text == "look at this"
        assert len(q.attachments) == 1
        assert q.attachments[0] is att

    def test_immutable(self) -> None:
        q = Query(text="hello")
        with pytest.raises(AttributeError):
            q.text = "changed"  # type: ignore[misc]

    def test_default_no_text(self) -> None:
        att = make_attachment()
        q = Query(attachments=(att,))
        assert q.text is None
        assert len(q.attachments) == 1

    def test_to_content_blocks_text_only(self) -> None:
        q = Query(text="just text")
        blocks = q.to_content_blocks()
        assert blocks == [{"type": "text", "text": "just text"}]

    def test_to_content_blocks_with_attachments(self) -> None:
        att = make_attachment()
        q = Query(text="see image", attachments=(att,))
        blocks = q.to_content_blocks()
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "see image"}
        assert blocks[1] is att.content_block

    def test_to_content_blocks_attachments_only(self) -> None:
        att = make_attachment()
        q = Query(attachments=(att,))
        blocks = q.to_content_blocks()
        assert len(blocks) == 1
        assert blocks[0] is att.content_block

    def test_to_content_blocks_empty(self) -> None:
        q = Query()
        blocks = q.to_content_blocks()
        assert blocks == []


class TestUnsupportedAttachmentError:
    def test_attributes(self) -> None:
        err = UnsupportedAttachmentError("file.docx", "application/vnd.openxmlformats")
        assert err.filename == "file.docx"
        assert err.mime_type == "application/vnd.openxmlformats"

    def test_message(self) -> None:
        err = UnsupportedAttachmentError("report.xlsx", "application/vnd.ms-excel")
        assert ".xlsx" in str(err)
        assert "PDF" in str(err)

    def test_message_no_extension(self) -> None:
        err = UnsupportedAttachmentError("Makefile", None)
        assert "unknown" in str(err)
        assert err.mime_type is None

    def test_is_exception(self) -> None:
        err = UnsupportedAttachmentError("bad.zip", "application/zip")
        assert isinstance(err, Exception)


def _make_update(media_group_id: str | None = None, message_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.message = MagicMock()
    update.message.media_group_id = media_group_id
    update.message.message_id = message_id
    update.message.caption = None
    return update


class TestMediaGroupCollector:
    @pytest.mark.asyncio
    async def test_single_message_returns_immediately(self) -> None:
        collector = MediaGroupCollector()
        update = _make_update(media_group_id=None)
        result = await collector.add(update)
        assert result is not None
        assert len(result) == 1
        assert result[0] is update

    @pytest.mark.asyncio
    async def test_album_buffers_and_returns(self) -> None:
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
        await asyncio.sleep(0.2)
        result = collector.pop_ready("group_1")
        assert result is not None
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_different_groups_independent(self) -> None:
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


# ---------------------------------------------------------------------------
# AttachmentProcessor test helpers
# ---------------------------------------------------------------------------

_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
_JPEG_HEADER = b"\xff\xd8\xff" + b"\x00" * 100
_PDF_CONTENT = b"%PDF-1.4 fake pdf content"
_PY_CONTENT = b"def hello():\n    return 'world'\n"
_CSV_CONTENT = b"name,age\nAlice,30\nBob,25\n"
_INI_CONTENT = b"[section]\nkey=value\n"
_UTF8_CONTENT = b"Hello, \xe4\xb8\x96\xe7\x95\x8c"  # valid UTF-8 with CJK chars
_BINARY_CONTENT = b"\x00\x01\x02\x80\x81\x82\xff\xfe"  # non-UTF-8 binary


def _make_telegram_file(content: bytes) -> AsyncMock:
    file = AsyncMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(content))
    return file


def _make_photo_message(image_bytes: bytes = _PNG_HEADER) -> MagicMock:
    photo_size = MagicMock()
    photo_size.file_size = len(image_bytes)
    photo_size.file_id = "photo_123"
    photo_size.get_file = AsyncMock(return_value=_make_telegram_file(image_bytes))
    message = MagicMock()
    message.photo = [MagicMock(), photo_size]  # pick last (largest)
    message.document = None
    message.caption = None
    return message


def _make_document_message(
    content: bytes, filename: str, mime_type: str | None = None
) -> MagicMock:
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


# ---------------------------------------------------------------------------
# TestAttachmentProcessorPhoto
# ---------------------------------------------------------------------------


class TestAttachmentProcessorPhoto:
    @pytest.mark.asyncio
    async def test_photo_creates_image_block(self) -> None:
        processor = AttachmentProcessor()
        message = _make_photo_message(_PNG_HEADER)
        att = await processor.process(message)

        assert att.content_block["type"] == "image"
        source = att.content_block["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "image/png"
        expected_data = base64.standard_b64encode(_PNG_HEADER).decode()
        assert source["data"] == expected_data
        assert att.media_type == "image/png"
        assert att.size == len(_PNG_HEADER)

    @pytest.mark.asyncio
    async def test_photo_jpeg_detection(self) -> None:
        processor = AttachmentProcessor()
        message = _make_photo_message(_JPEG_HEADER)
        att = await processor.process(message)

        assert att.content_block["type"] == "image"
        assert att.content_block["source"]["media_type"] == "image/jpeg"
        assert att.media_type == "image/jpeg"


# ---------------------------------------------------------------------------
# TestAttachmentProcessorDocument
# ---------------------------------------------------------------------------


class TestAttachmentProcessorDocument:
    @pytest.mark.asyncio
    async def test_image_document_creates_image_block(self) -> None:
        processor = AttachmentProcessor()
        message = _make_document_message(_PNG_HEADER, "image.png", "image/png")
        att = await processor.process(message)

        assert att.content_block["type"] == "image"
        assert att.content_block["source"]["media_type"] == "image/png"
        assert att.filename == "image.png"

    @pytest.mark.asyncio
    async def test_pdf_creates_document_block(self) -> None:
        processor = AttachmentProcessor()
        message = _make_document_message(_PDF_CONTENT, "report.pdf", "application/pdf")
        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        source = att.content_block["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "application/pdf"
        expected_data = base64.standard_b64encode(_PDF_CONTENT).decode()
        assert source["data"] == expected_data
        assert att.content_block["title"] == "report.pdf"
        assert att.media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_text_file_creates_text_document_block(self) -> None:
        processor = AttachmentProcessor()
        message = _make_document_message(_PY_CONTENT, "script.py", "text/x-python")
        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        source = att.content_block["source"]
        assert source["type"] == "text"
        assert source["data"] == _PY_CONTENT.decode("utf-8")
        assert att.content_block["title"] == "script.py"

    @pytest.mark.asyncio
    async def test_csv_detected_as_text(self) -> None:
        processor = AttachmentProcessor()
        message = _make_document_message(_CSV_CONTENT, "data.csv", "text/csv")
        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"
        assert att.content_block["source"]["data"] == _CSV_CONTENT.decode("utf-8")

    @pytest.mark.asyncio
    async def test_unknown_text_extension_detected(self) -> None:
        # .ini has no MIME but is in _TEXT_EXTENSIONS
        processor = AttachmentProcessor()
        message = _make_document_message(_INI_CONTENT, "config.ini", None)
        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"
        assert att.content_block["source"]["data"] == _INI_CONTENT.decode("utf-8")

    @pytest.mark.asyncio
    async def test_unknown_binary_detected_via_utf8_attempt(self) -> None:
        # Valid UTF-8 with unknown extension and no MIME — falls through to UTF-8 try
        processor = AttachmentProcessor()
        message = _make_document_message(_UTF8_CONTENT, "unknown.xyz", None)
        att = await processor.process(message)

        assert att.content_block["type"] == "document"
        assert att.content_block["source"]["type"] == "text"
        assert att.content_block["source"]["data"] == _UTF8_CONTENT.decode("utf-8")

    @pytest.mark.asyncio
    async def test_binary_file_rejected(self) -> None:
        processor = AttachmentProcessor()
        message = _make_document_message(_BINARY_CONTENT, "file.bin", None)

        with pytest.raises(UnsupportedAttachmentError) as exc_info:
            await processor.process(message)

        assert exc_info.value.filename == "file.bin"
        assert exc_info.value.mime_type is None

    @pytest.mark.asyncio
    async def test_no_photo_no_document_raises(self) -> None:
        processor = AttachmentProcessor()
        message = MagicMock()
        message.photo = None
        message.document = None

        with pytest.raises(ValueError, match="no photo or document"):
            await processor.process(message)
