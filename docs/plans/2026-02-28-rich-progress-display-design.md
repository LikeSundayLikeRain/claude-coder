# Rich Progress Display Design

**Date:** 2026-02-28
**Status:** Approved

## Problem

The bot's streaming output is far less informative than the Claude Code CLI. During long operations (planning, multi-step tool usage), the bot shows a "Working..." message that is:

1. **Ephemeral** — deleted when the query completes, leaving no record of what Claude did
2. **Truncated** — intermediate text capped at 200 chars, merged into single entries
3. **Missing tool results** — `UserMessage` events (tool outputs) silently dropped in `UserClient`
4. **Missing content** — entire planning phases, skill invocations, and intermediate reasoning are invisible
5. **Capped** — only last 15 activity entries shown, older ones scroll off
6. **Silent at verbose=0** — no callback at all, user sees blank "Working..." for minutes

The CLI shows the full flow: intermediate text, tool call summaries, tool results, thinking indicators — all persistent in the terminal scrollback.

## Design

### Approach: Evolving Accumulator

A single persistent Telegram message accumulates all activity as a scrolling log. When approaching Telegram's 4096-char limit, the current message is finalized and a new one starts. Messages are **never deleted**.

### Mental Model

```
[Progress message 1 — finalized, stays in chat]
[Progress message 2 — finalized, stays in chat]  (only if overflow)
[Progress message N — finalized on completion]
[Final response message(s) — the actual answer]
```

### Change 1: Event Pipeline — Capture Tool Results

**`src/claude/user_client.py`** — Forward `"user"` events (tool results) to the callback:

```python
elif event.type == "user" and event.content and item.on_stream:
    await item.on_stream("tool_result", event.content)
```

Updated event flow to callback:

| Event | Content | Before | After |
|-------|---------|--------|-------|
| `text` | Intermediate text | Forwarded, truncated 200 chars | Forwarded, **full** |
| `tool_use` | `{name, input}` | Forwarded | Forwarded, with running state |
| `thinking` | Thinking text | Forwarded, truncated | Indicator only (`Thinking...`) |
| `tool_result` | Raw result text | **Dropped** | **Forwarded** |
| `result` | Final response | Stored as return value | No change |

**`src/claude/stream_handler.py`** — No changes needed. Already produces all required event types.

### Change 2: Activity Log Data Model

New file: **`src/bot/progress.py`**

```python
@dataclass
class ActivityEntry:
    kind: Literal["text", "tool", "thinking"]
    content: str           # Full text (no truncation) or tool summary line
    tool_name: str = ""    # For kind="tool"
    tool_detail: str = ""  # Input summary (e.g. "src/foo.py", "git commit")
    tool_result: str = ""  # Brief result summary (e.g. "[main 442f44f] docs: add design")
    is_running: bool = False  # True while tool/thinking is executing
```

**Text accumulation:** Consecutive text deltas merge into one `ActivityEntry` (avoids word-fragment noise from streaming). No character cap. A new text entry starts when a non-text event intervenes.

**Tool result summarization** — Parse `tool_result` content into a brief line:
- Bash: first line of output, truncated to ~100 chars
- Write: "Wrote N lines" (extract from result text)
- Agent/Task: completion stats if available
- Default: first ~80 chars of raw content

### Change 3: ProgressMessageManager

New class in **`src/bot/progress.py`**:

```python
class ProgressMessageManager:
    MAX_MSG_LENGTH = 4000  # Leave 96-char margin under Telegram's 4096 limit
    EDIT_INTERVAL = 2.0    # Seconds between edits (Telegram rate limit)

    messages: list[Message]       # All progress messages sent so far
    activity_log: list[ActivityEntry]
    rendered_up_to: int           # Index frozen in previous messages
    start_time: float
    last_edit_time: float
```

**`update()` method** (called from stream callback):

1. Render entries from `rendered_up_to` to end of `activity_log`
2. Build message: header + rendered lines
3. If fits in 4000 chars → edit current (last) message
4. If exceeds 4000 chars → finalize current message, send new one for overflow, update `rendered_up_to`
5. Respect 2-second edit throttle

**`finalize()` method** (called when query completes):

1. Update header: `"Working... (42s)"` → `"Done (42s)"`
2. Remove `⏳` from any running tools
3. Final edit to last message
4. **Do NOT delete any messages**

**Thinking animation:**
- While active: `💭 Thinking...` with dots cycling (`.`, `..`, `...`) on each 2-second edit
- When done: `💭 Thinking (done)`

**Running tool indicator:**
- While active: `📖 Read src/foo.py ⏳`
- When done: remove `⏳`, optionally show result on next line

### Change 4: Stream Callback Redesign

Replace `_make_stream_callback()` in **`src/bot/orchestrator.py`**:

```python
def _make_stream_callback(self, progress_manager: ProgressMessageManager) -> Callable:
    activity_log = progress_manager.activity_log

    async def _on_stream(event_type: str, content: Any) -> None:
        _close_running_entry(activity_log)

        if event_type == "tool_use" and isinstance(content, dict):
            name = content.get("name", "unknown")
            detail = _summarize_tool_input(name, content.get("input", {}))
            activity_log.append(ActivityEntry(
                kind="tool", tool_name=name, tool_detail=detail, is_running=True
            ))

        elif event_type == "text" and content:
            text = str(content)
            if activity_log and activity_log[-1].kind == "text":
                activity_log[-1].content += text
            else:
                activity_log.append(ActivityEntry(kind="text", content=text))

        elif event_type == "thinking":
            if not (activity_log and activity_log[-1].kind == "thinking"
                    and activity_log[-1].is_running):
                activity_log.append(ActivityEntry(
                    kind="thinking", content="Thinking", is_running=True
                ))

        elif event_type == "tool_result" and content:
            _attach_result_to_last_tool(activity_log, str(content))

        await progress_manager.update()

    return _on_stream
```

**Always returns a callback** — no `None` for verbose=0 (verbose levels removed).

**`_close_running_entry()`:** Finds last entry with `is_running=True`, sets to `False`. For thinking: content becomes `"Thinking (done)"`.

**`_attach_result_to_last_tool()`:** Finds most recent tool entry, sets `tool_result` to a brief summary of the raw content.

### Change 5: Orchestrator Integration

**`agentic_text()` changes:**

```python
# Before:
verbose_level = self._get_verbose_level(context)
progress_msg = await update.message.reply_text("Working...")
tool_log: List[Dict] = []
on_stream = self._make_stream_callback(verbose_level, progress_msg, tool_log, start_time)
...
await progress_msg.delete()

# After:
progress_msg = await update.message.reply_text("Working...")
progress_manager = ProgressMessageManager(progress_msg, start_time=time.time())
on_stream = self._make_stream_callback(progress_manager)
...
await progress_manager.finalize()
```

### Change 6: Remove Verbose Level System

**Removed:**
- `_get_verbose_level()` method from orchestrator
- `_format_verbose_progress()` method from orchestrator
- `/verbose` command handler and registration
- `VERBOSE_LEVEL` from `src/config/settings.py`
- Verbose-related feature flag from `src/config/features.py` (if present)
- `verbose` entry from `get_bot_commands()` return list

**Rationale:** The new persistent progress block provides a single consistent experience. Verbose levels added complexity for a feature that's now obsolete — users always see full activity.

### Rendering Format

**During execution:**
```
Working... (15s)

Let me write the design doc and commit it.

📖 Read docs/plans/design.md
✏️ Write docs/plans/design.md
  ⎿ Wrote 94 lines
💻 Bash: git add ... && git commit
  ⎿ [main 442f44f] docs: add design

Design doc committed. Now transitioning to planning.

🔧 Skill: superpowers:writing-plans
🔍 Grep: get_server_info
📖 Read src/bot/orchestrator.py ⏳
```

**After completion:**
```
Done (42s)

Let me write the design doc and commit it.

📖 Read docs/plans/design.md
✏️ Write docs/plans/design.md
  ⎿ Wrote 94 lines
💻 Bash: git add ... && git commit
  ⎿ [main 442f44f] docs: add design

Design doc committed. Now transitioning to planning.

🔧 Skill: superpowers:writing-plans
🔍 Grep: get_server_info
📖 Read src/bot/orchestrator.py
✏️ Write docs/plans/plan.md
  ⎿ Wrote 817 lines
💻 Bash: git add ... && git commit
  ⎿ [main 9c48bbc] docs: add plan
```

**Rendering rules:**
- Text entries: full content, separated by blank lines from tool blocks
- Tool entries: `{icon} {name}: {detail}` (or `{icon} {name}` if no detail)
- Tool results: indented `  ⎿ {summary}` on next line
- Running tools: append ` ⏳`
- Thinking: `💭 Thinking...` (animated) → `💭 Thinking (done)`
- Plain text mode (no HTML parsing — avoids escaping issues with code in tool results)
- Message overflow at ~4000 chars: finalize current, start new with "(continued)" suffix on header

## Files Touched

- **New:** `src/bot/progress.py` — `ProgressMessageManager`, `ActivityEntry`, helper functions
- **Modified:** `src/bot/orchestrator.py` — replace callback/progress logic, remove verbose, remove `/verbose` command
- **Modified:** `src/claude/user_client.py` — forward `"user"` events as `"tool_result"`
- **Modified:** `src/config/settings.py` — remove `VERBOSE_LEVEL`

## Files Removed / Gutted

- `/verbose` command handler (in orchestrator)
- `_get_verbose_level()`, `_format_verbose_progress()`, `_summarize_tool_input()` (moved to progress.py)

## Not Changed

- `src/claude/stream_handler.py` — already produces all needed event types
- `src/claude/client_manager.py` — no changes needed
- `src/bot/utils/formatting.py` — final response formatting unchanged
- Security middleware — unaffected
