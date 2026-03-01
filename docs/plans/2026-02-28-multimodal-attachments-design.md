# Multimodal Attachments Design

## Goal

Send files and images from Telegram to Claude as native Anthropic API content blocks, replacing the current text-extraction approach. Claude sees images visually and reads documents directly.

## Scope

**In scope:**
- Receiving images (PNG, JPG, GIF, WebP) as `image` content blocks
- Receiving PDFs as `document` content blocks (base64)
- Receiving text-based files (code, CSV, JSON, YAML, etc.) as `document` content blocks (plain text)
- Telegram media groups (albums) — multiple attachments in one query
- Agentic mode only

**Out of scope:**
- Sending files/images back to users (response direction)
- Classic mode changes
- Unsupported binary files (xlsx, docx, zip) — rejected with helpful message

## SDK Capabilities

The Claude Agent SDK (`claude-agent-sdk` v0.1.43) `query()` accepts:
- `str` — plain text prompt
- `AsyncIterable[dict[str, Any]]` — stream-json messages with content blocks

Content blocks follow the Anthropic Messages API format:

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {"type": "text", "text": "What is this?"},
      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    ]
  },
  "session_id": "",
  "parent_tool_use_id": null
}
```

Supported block types:
- `image` — base64 or URL, media types: image/png, image/jpeg, image/gif, image/webp
- `document` — base64 PDF (`application/pdf`) or plain text (`text/plain`), optional `title` field

Multiple content blocks per message are supported (images + documents + text in one query).

## Architecture

### New: `src/bot/attachments.py`

Single file containing all attachment handling. Components:

#### `Attachment` (frozen dataclass)

```python
@dataclass(frozen=True)
class Attachment:
    content_block: dict[str, Any]   # Anthropic API content block
    filename: str
    size: int
    media_type: str
```

#### `Query` (frozen dataclass)

```python
@dataclass(frozen=True)
class Query:
    text: str | None = None
    attachments: tuple[Attachment, ...] = ()
```

Replaces the plain `str` prompt throughout the pipeline. Used by both text messages and attachments. Tuple for immutability.

#### `AttachmentProcessor`

Stateless. Converts Telegram messages to `Attachment` objects.

```python
class AttachmentProcessor:
    async def process(self, message: Message) -> Attachment
```

Internally checks `message.photo` vs `message.document` and routes:

| Telegram input | Detection | Anthropic block |
|---|---|---|
| `message.photo` | Always image | `image` block, base64 |
| Document with image MIME | `mime_type.startswith("image/")` | `image` block, base64 |
| Document with `application/pdf` | MIME type | `document` block, base64 |
| Document with text MIME or known text extension | MIME + extension heuristic | `document` block, plain text |
| Document, unknown binary | Fallback | Raise `UnsupportedAttachmentError` |

Text detection heuristic for documents without clear MIME type:
1. Check MIME type: `text/*` -> text
2. Check extension against known text extensions (`.py`, `.js`, `.csv`, `.json`, `.yaml`, `.xml`, `.md`, `.txt`, `.toml`, `.ini`, `.cfg`, `.log`, `.sh`, etc.)
3. Attempt UTF-8 decode of first 8KB — if successful, treat as text
4. Otherwise, reject as unsupported binary

No filename validation — files are never written to disk. No size limits — delegated to Telegram (20MB) and the Anthropic API. Only check: can we convert this to a content block?

#### `MediaGroupCollector`

Buffers Telegram album messages and groups them.

```python
class MediaGroupCollector:
    async def add(self, update: Update) -> list[Update] | None
```

Behavior:
- Message has `media_group_id` -> buffer it, schedule 1s timeout. Return `None`.
- Timeout fires (no new items for 1s) -> return all buffered items as a list.
- Message has no `media_group_id` -> return `[update]` immediately (group of 1).

Edge cases:
- Caption only on first album item (Telegram behavior) -> use that caption for the whole group.
- Mixed types in album (photos + documents) -> each processed individually.

#### `UnsupportedAttachmentError`

```python
class UnsupportedAttachmentError(Exception):
    def __init__(self, filename: str, mime_type: str | None):
        self.filename = filename
        self.mime_type = mime_type
```

### Modified: `src/claude/user_client.py`

`submit()` accepts `Query` instead of `str`.

Always uses the `AsyncIterable[dict]` prompt path — no branching between string and multimodal:

```python
async def submit(self, query: Query) -> None:
    # Build content blocks
    content_blocks = []
    if query.text:
        content_blocks.append({"type": "text", "text": query.text})
    for att in query.attachments:
        content_blocks.append(att.content_block)
    # Enqueue for worker
    await self._queue.put(_WorkItem(content_blocks=content_blocks))
```

Worker sends via async iterable:

```python
async def _make_prompt(self, content_blocks: list[dict]) -> AsyncIterator[dict]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "session_id": self._session_id or "",
        "parent_tool_use_id": None,
    }
```

### Modified: `src/bot/orchestrator.py`

#### Single unified handler

Replace `agentic_document()` + `agentic_photo()` with one `agentic_attachment()`:

```python
async def agentic_attachment(self, update, context):
    group = await self._media_collector.add(update)
    if group is None:
        return  # still buffering album

    attachments = []
    caption = None
    for item in group:
        try:
            att = await self._processor.process(item.message)
            attachments.append(att)
        except UnsupportedAttachmentError as e:
            # Skip unsupported, notify user
            ...
        if not caption and item.message.caption:
            caption = item.message.caption

    if not attachments:
        return  # all items were unsupported

    query = Query(text=caption or "Analyze this.", attachments=tuple(attachments))
    await self._run_query(query, update, context)
```

#### Text handler change

`agentic_text()` wraps the message in a `Query`:

```python
query = Query(text=message_text)
await self._run_query(query, update, context)
```

#### Handler registration

```python
# Replace separate photo/document handlers with one:
app.add_handler(MessageHandler(
    filters.PHOTO | filters.Document.ALL,
    self._inject_deps(self.agentic_attachment),
))
```

### Modified: `src/bot/features/registry.py`

- Remove `FileHandler` and `ImageHandler` initialization
- Remove `get_file_handler()` and `get_image_handler()` methods
- Optionally register `AttachmentProcessor` (though it's stateless and could just be instantiated directly)

### Modified: `src/config/features.py`

- Replace `enable_file_uploads` with `enable_attachments` (single flag)
- Remove any image-specific flags

### Removed

- `src/bot/features/file_handler.py` — entire file
- `src/bot/features/image_handler.py` — entire file

### Unchanged

- `StreamHandler` — response format unchanged regardless of input type
- `ProgressMessageManager` — still shows real-time activity during processing
- `SecurityValidator` — stays for other uses (classic mode, path validation), just not called by attachment pipeline
- Error handling — existing `_format_error_message()` catches API errors (file too large, too many PDF pages, etc.)

## Error Handling

**Pre-query rejections (AttachmentProcessor):**
- Unsupported binary type -> reply: "I can't process .xlsx files. Try sending as PDF or text."
- Album with mixed supported/unsupported -> process supported, skip unsupported with a note

**Post-query errors (API):**
- File too large for API -> caught by existing error handler, surfaced to user
- PDF too many pages -> same
- Invalid format -> same
- Network/timeout -> same

No custom size limits. No filename validation. Telegram's 20MB cap and the Anthropic API are the only gates.

## Data Flow

```
Telegram photo/document/album
  -> MediaGroupCollector.add(update)
     -> media_group_id? buffer, 1s timeout, return group
     -> no media_group_id? return [update] (group of 1)
  -> AttachmentProcessor.process(message) for each item
     -> download bytes from Telegram
     -> detect type (image / PDF / text / unsupported)
     -> build Anthropic content block
     -> return Attachment
  -> Build Query(text=caption, attachments=[...])
  -> UserClient.submit(query)
     -> build content_blocks list
     -> yield stream-json message dict via AsyncIterable
     -> SDK sends to claude CLI stdin
  -> Claude processes, streams response
  -> StreamHandler + ProgressMessageManager (unchanged)
  -> Response sent to Telegram (unchanged)
```

## Testing Strategy

- Unit tests for `AttachmentProcessor`: mock Telegram file downloads, verify correct content block format for each type
- Unit tests for `MediaGroupCollector`: verify buffering, timeout, single-item bypass
- Unit tests for `Query` construction in orchestrator handler
- Integration test for `UserClient` with `Query` containing attachments
- Manual test: send image, PDF, code file, album via Telegram and verify Claude sees them
