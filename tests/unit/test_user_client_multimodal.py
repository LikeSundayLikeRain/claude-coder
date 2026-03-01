"""Tests for UserClient multimodal (Query-based) interface."""

from __future__ import annotations

import asyncio

import pytest

from src.bot.attachments import Attachment, Query
from src.claude.user_client import WorkItem


class TestUserClientMultimodal:
    def test_work_item_accepts_query(self) -> None:
        q = Query(text="hello")
        loop = asyncio.new_event_loop()
        future: asyncio.Future[object] = loop.create_future()
        item = WorkItem(query=q, future=future)
        assert item.query.text == "hello"
        loop.close()

    def test_work_item_with_attachments(self) -> None:
        att = Attachment(
            content_block={
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "abc",
                },
            },
            filename="test.png",
            size=100,
            media_type="image/png",
        )
        q = Query(text="look at this", attachments=(att,))
        loop = asyncio.new_event_loop()
        future: asyncio.Future[object] = loop.create_future()
        item = WorkItem(query=q, future=future)
        assert len(item.query.attachments) == 1
        loop.close()

    def test_work_item_query_only_no_text(self) -> None:
        att = Attachment(
            content_block={"type": "text", "text": "doc content"},
            filename="doc.txt",
            size=50,
            media_type="text/plain",
        )
        q = Query(attachments=(att,))
        loop = asyncio.new_event_loop()
        future: asyncio.Future[object] = loop.create_future()
        item = WorkItem(query=q, future=future)
        assert item.query.text is None
        assert len(item.query.attachments) == 1
        loop.close()

    def test_query_to_content_blocks_text_only(self) -> None:
        q = Query(text="hello world")
        blocks = q.to_content_blocks()
        assert blocks == [{"type": "text", "text": "hello world"}]

    def test_query_to_content_blocks_with_image(self) -> None:
        image_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "xyz"},
        }
        att = Attachment(
            content_block=image_block,
            filename="photo.jpg",
            size=200,
            media_type="image/jpeg",
        )
        q = Query(text="describe this", attachments=(att,))
        blocks = q.to_content_blocks()
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "describe this"}
        assert blocks[1] == image_block

    def test_query_to_content_blocks_empty(self) -> None:
        q = Query()
        blocks = q.to_content_blocks()
        assert blocks == []

    def test_work_item_on_stream_defaults_none(self) -> None:
        q = Query(text="ping")
        loop = asyncio.new_event_loop()
        future: asyncio.Future[object] = loop.create_future()
        item = WorkItem(query=q, future=future)
        assert item.on_stream is None
        loop.close()
