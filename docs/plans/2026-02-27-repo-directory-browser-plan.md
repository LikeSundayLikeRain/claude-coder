# Repo Directory Browser Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn `/repo` from a flat 1-level directory lister into a navigable directory browser with `.`/`..` navigation and unlimited depth.

**Architecture:** Extract directory browsing logic into a helper module (`src/bot/utils/repo_browser.py`) that classifies directories as branch/leaf, builds keyboard layouts, and resolves relative paths. The orchestrator's `agentic_repo` and `_agentic_callback` methods delegate to this module. New callback prefixes `nav:` (browse into) and `sel:` (select directory) replace the current `cd:` pattern for repo browsing, while `cd:` remains handled for backward compat.

**Tech Stack:** Python 3.10+, python-telegram-bot (InlineKeyboardButton/Markup), pathlib, pytest-asyncio

---

### Task 1: Extract directory filtering into a helper module

**Files:**
- Create: `src/bot/utils/repo_browser.py`
- Test: `tests/unit/test_bot/test_repo_browser.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_bot/test_repo_browser.py`:

```python
"""Tests for repo browser utilities."""

import tempfile
from pathlib import Path

import pytest

from src.bot.utils.repo_browser import FILTERED_DIRS, list_visible_children


@pytest.fixture
def workspace(tmp_path):
    """Create a workspace with mixed directories."""
    (tmp_path / "projectA").mkdir()
    (tmp_path / "projectA" / "src").mkdir()
    (tmp_path / "projectA" / ".git").mkdir()
    (tmp_path / "projectA" / "node_modules").mkdir()
    (tmp_path / "projectA" / "__pycache__").mkdir()
    (tmp_path / "projectB").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "build").mkdir()
    return tmp_path


def test_list_visible_children_filters_dotfiles(workspace):
    children = list_visible_children(workspace)
    names = [c.name for c in children]
    assert ".hidden" not in names
    assert ".git" not in names


def test_list_visible_children_filters_noise_dirs(workspace):
    children = list_visible_children(workspace / "projectA")
    names = [c.name for c in children]
    assert "node_modules" not in names
    assert "__pycache__" not in names
    assert "src" in names


def test_list_visible_children_filters_build_at_root(workspace):
    children = list_visible_children(workspace)
    names = [c.name for c in children]
    assert "build" not in names


def test_list_visible_children_sorted(workspace):
    children = list_visible_children(workspace)
    names = [c.name for c in children]
    assert names == sorted(names)


def test_list_visible_children_empty_dir(tmp_path):
    children = list_visible_children(tmp_path)
    assert children == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.bot.utils.repo_browser'`

**Step 3: Write minimal implementation**

Create `src/bot/utils/repo_browser.py`:

```python
"""Repo directory browser utilities.

Provides directory listing, classification (branch vs leaf), and keyboard
building for the /repo navigable directory browser.
"""

from pathlib import Path
from typing import List

# Directories filtered from browser listings (dotfiles handled separately)
FILTERED_DIRS = frozenset({
    "node_modules",
    "__pycache__",
    "target",
    "build",
    "dist",
    ".tox",
})


def list_visible_children(directory: Path) -> List[Path]:
    """List visible child directories, filtering dotfiles and noise.

    Returns sorted list of child directories that are not hidden
    (dotfiles) and not in the FILTERED_DIRS set.
    """
    try:
        return sorted(
            [
                d
                for d in directory.iterdir()
                if d.is_dir()
                and not d.name.startswith(".")
                and d.name not in FILTERED_DIRS
            ],
            key=lambda d: d.name,
        )
    except OSError:
        return []
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add src/bot/utils/repo_browser.py tests/unit/test_bot/test_repo_browser.py
git commit -m "feat: add repo browser directory listing with filtering"
```

---

### Task 2: Add branch/leaf classification

**Files:**
- Modify: `src/bot/utils/repo_browser.py`
- Test: `tests/unit/test_bot/test_repo_browser.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_repo_browser.py`:

```python
from src.bot.utils.repo_browser import is_branch_dir


def test_branch_dir_with_visible_children(workspace):
    """projectA has src/ -> branch."""
    assert is_branch_dir(workspace / "projectA") is True


def test_leaf_dir_no_visible_children(workspace):
    """projectB has no subdirs -> leaf."""
    assert is_branch_dir(workspace / "projectB") is False


def test_branch_ignores_filtered_children(tmp_path):
    """Dir with only node_modules -> leaf (filtered out)."""
    d = tmp_path / "proj"
    d.mkdir()
    (d / "node_modules").mkdir()
    (d / ".git").mkdir()
    assert is_branch_dir(d) is False
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v -k "branch or leaf"`
Expected: FAIL with `ImportError: cannot import name 'is_branch_dir'`

**Step 3: Write minimal implementation**

Add to `src/bot/utils/repo_browser.py`:

```python
def is_branch_dir(directory: Path) -> bool:
    """Check if directory has visible child directories (is navigable).

    A branch directory has at least one child that passes the
    visibility filter. A leaf directory has none.
    """
    return len(list_visible_children(directory)) > 0
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add src/bot/utils/repo_browser.py tests/unit/test_bot/test_repo_browser.py
git commit -m "feat: add branch/leaf directory classification"
```

---

### Task 3: Add keyboard builder for directory browser

**Files:**
- Modify: `src/bot/utils/repo_browser.py`
- Test: `tests/unit/test_bot/test_repo_browser.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_repo_browser.py`:

```python
from src.bot.utils.repo_browser import build_browser_keyboard


def test_keyboard_has_dot_and_dotdot(workspace):
    """First row should have . and .. buttons."""
    rows = build_browser_keyboard(
        browse_dir=workspace / "projectA",
        workspace_root=workspace,
    )
    first_row = rows[0]
    assert len(first_row) == 2
    assert first_row[0].text == ". (select)"
    assert first_row[0].callback_data == "sel:."
    assert first_row[1].text == ".."
    assert first_row[1].callback_data == "nav:.."


def test_keyboard_hides_dotdot_at_single_root(workspace):
    """At root with single workspace, .. is hidden."""
    rows = build_browser_keyboard(
        browse_dir=workspace,
        workspace_root=workspace,
    )
    first_row = rows[0]
    assert len(first_row) == 1
    assert first_row[0].text == ". (select)"


def test_keyboard_shows_dotdot_at_root_multi_workspace(workspace):
    """At root with multiple workspaces, .. goes to root picker."""
    rows = build_browser_keyboard(
        browse_dir=workspace,
        workspace_root=workspace,
        multi_root=True,
    )
    first_row = rows[0]
    assert len(first_row) == 2
    assert first_row[1].callback_data == "nav:.."


def test_keyboard_branch_dirs_get_nav_prefix(workspace):
    """Branch dirs use nav: callback prefix."""
    rows = build_browser_keyboard(
        browse_dir=workspace,
        workspace_root=workspace,
    )
    # projectA is branch (has src/), should have nav: prefix
    dir_buttons = [btn for row in rows[1:] for btn in row]
    project_a_btn = next(b for b in dir_buttons if "projectA" in b.text)
    assert project_a_btn.callback_data.startswith("nav:")


def test_keyboard_leaf_dirs_get_sel_prefix(workspace):
    """Leaf dirs use sel: callback prefix."""
    rows = build_browser_keyboard(
        browse_dir=workspace,
        workspace_root=workspace,
    )
    dir_buttons = [btn for row in rows[1:] for btn in row]
    project_b_btn = next(b for b in dir_buttons if "projectB" in b.text)
    assert project_b_btn.callback_data.startswith("sel:")


def test_keyboard_callback_uses_relative_paths(workspace):
    """Callback data should use paths relative to workspace root."""
    rows = build_browser_keyboard(
        browse_dir=workspace,
        workspace_root=workspace,
    )
    dir_buttons = [btn for row in rows[1:] for btn in row]
    project_a_btn = next(b for b in dir_buttons if "projectA" in b.text)
    assert project_a_btn.callback_data == "nav:projectA"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v -k "keyboard"`
Expected: FAIL with `ImportError: cannot import name 'build_browser_keyboard'`

**Step 3: Write minimal implementation**

Add to `src/bot/utils/repo_browser.py`:

```python
from telegram import InlineKeyboardButton


def build_browser_keyboard(
    browse_dir: Path,
    workspace_root: Path,
    multi_root: bool = False,
) -> List[List[InlineKeyboardButton]]:
    """Build inline keyboard rows for directory browser.

    Args:
        browse_dir: The directory currently being browsed.
        workspace_root: The workspace root this directory is under.
        multi_root: Whether there are multiple workspace roots.

    Returns:
        List of keyboard rows (each a list of InlineKeyboardButton).
    """
    rows: List[List[InlineKeyboardButton]] = []

    # Navigation row: . and ..
    nav_row: List[InlineKeyboardButton] = [
        InlineKeyboardButton(". (select)", callback_data="sel:."),
    ]
    # Show .. unless we're at the root of a single-root workspace
    at_root = browse_dir == workspace_root
    if not at_root or multi_root:
        nav_row.append(InlineKeyboardButton("..", callback_data="nav:.."))
    rows.append(nav_row)

    # Directory entries
    children = list_visible_children(browse_dir)
    for i in range(0, len(children), 2):
        row: List[InlineKeyboardButton] = []
        for j in range(2):
            if i + j < len(children):
                child = children[i + j]
                rel_path = str(child.relative_to(workspace_root))
                prefix = "nav" if is_branch_dir(child) else "sel"
                row.append(
                    InlineKeyboardButton(
                        child.name, callback_data=f"{prefix}:{rel_path}"
                    )
                )
        rows.append(row)

    return rows
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add src/bot/utils/repo_browser.py tests/unit/test_bot/test_repo_browser.py
git commit -m "feat: add keyboard builder for repo directory browser"
```

---

### Task 4: Add browse header text builder

**Files:**
- Modify: `src/bot/utils/repo_browser.py`
- Test: `tests/unit/test_bot/test_repo_browser.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_repo_browser.py`:

```python
from src.bot.utils.repo_browser import build_browse_header


def test_header_at_root(workspace):
    header = build_browse_header(workspace, workspace)
    assert "Browsing:" in header
    assert "/" in header


def test_header_at_subdir(workspace):
    header = build_browse_header(workspace / "projectA", workspace)
    assert "projectA/" in header


def test_header_at_nested_subdir(workspace):
    header = build_browse_header(workspace / "projectA" / "src", workspace)
    assert "projectA/src/" in header
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v -k "header"`
Expected: FAIL with `ImportError`

**Step 3: Write minimal implementation**

Add to `src/bot/utils/repo_browser.py`:

```python
def build_browse_header(browse_dir: Path, workspace_root: Path) -> str:
    """Build the header text showing current browse location.

    Args:
        browse_dir: Current directory being browsed.
        workspace_root: Workspace root for computing relative path.

    Returns:
        HTML-formatted header string.
    """
    rel = browse_dir.relative_to(workspace_root)
    display = "/" if str(rel) == "." else f"{rel}/"
    return f"\U0001f4c2 <b>Browsing:</b> <code>{display}</code>"
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v`
Expected: All 17 tests PASS

**Step 5: Commit**

```bash
git add src/bot/utils/repo_browser.py tests/unit/test_bot/test_repo_browser.py
git commit -m "feat: add browse header builder for repo browser"
```

---

### Task 5: Add path resolution helper

**Files:**
- Modify: `src/bot/utils/repo_browser.py`
- Test: `tests/unit/test_bot/test_repo_browser.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_repo_browser.py`:

```python
from src.bot.utils.repo_browser import resolve_browse_path


def test_resolve_single_segment(workspace):
    result = resolve_browse_path("projectA", [workspace])
    assert result == workspace / "projectA"


def test_resolve_multi_segment(workspace):
    result = resolve_browse_path("projectA/src", [workspace])
    assert result == workspace / "projectA" / "src"


def test_resolve_not_found(workspace):
    result = resolve_browse_path("nonexistent", [workspace])
    assert result is None


def test_resolve_searches_multiple_roots(tmp_path):
    root1 = tmp_path / "ws1"
    root2 = tmp_path / "ws2"
    root1.mkdir()
    root2.mkdir()
    target = root2 / "myrepo"
    target.mkdir()
    result = resolve_browse_path("myrepo", [root1, root2])
    assert result == target


def test_resolve_not_a_directory(workspace):
    """Files should not be resolved."""
    (workspace / "afile.txt").touch()
    result = resolve_browse_path("afile.txt", [workspace])
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v -k "resolve"`
Expected: FAIL with `ImportError`

**Step 3: Write minimal implementation**

Add to `src/bot/utils/repo_browser.py`:

```python
from typing import Optional


def resolve_browse_path(
    target: str, roots: List[Path]
) -> Optional[Path]:
    """Resolve a relative path against workspace roots.

    Supports multi-level paths like 'project/src/handlers'. Searches
    each root in order and returns the first match.

    Args:
        target: Relative path string (e.g. 'myrepo' or 'myrepo/src/lib').
        roots: List of workspace root directories to search.

    Returns:
        Resolved absolute Path if found and is a directory, else None.
    """
    for root in roots:
        candidate = (root / target).resolve()
        if candidate.is_dir() and candidate.is_relative_to(root):
            return candidate
    return None
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser.py -v`
Expected: All 22 tests PASS

**Step 5: Commit**

```bash
git add src/bot/utils/repo_browser.py tests/unit/test_bot/test_repo_browser.py
git commit -m "feat: add multi-level path resolution for repo browser"
```

---

### Task 6: Wire up `agentic_repo` to use the browser

**Files:**
- Modify: `src/bot/orchestrator.py:1524-1693` (the `agentic_repo` method)

**Step 1: Write the failing test**

Create `tests/unit/test_bot/test_repo_browser_integration.py`:

```python
"""Integration tests for /repo directory browser in orchestrator."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


@pytest.fixture
def workspace(tmp_path):
    """Workspace with branch and leaf dirs."""
    (tmp_path / "projectA" / "src").mkdir(parents=True)
    (tmp_path / "projectA" / ".git").mkdir()
    (tmp_path / "projectB").mkdir()
    (tmp_path / "projectB" / ".git").mkdir()
    return tmp_path


@pytest.fixture
def settings(workspace):
    return create_test_config(
        approved_directory=str(workspace), agentic_mode=True
    )


@pytest.fixture
def orchestrator(settings):
    deps = {
        "storage": MagicMock(),
        "audit_logger": MagicMock(),
        "client_manager": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


def _make_update(text="/repo", user_id=123):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(user_data=None, bot_data=None):
    ctx = MagicMock()
    ctx.user_data = user_data or {}
    ctx.bot_data = bot_data or {}
    return ctx


async def test_repo_no_args_shows_browser(orchestrator, workspace):
    """'/repo' with no args shows browsable listing with . and .. buttons."""
    update = _make_update("/repo")
    ctx = _make_context(bot_data={"storage": MagicMock()})

    await orchestrator.agentic_repo(update, ctx)

    reply_call = update.message.reply_text
    reply_call.assert_called_once()
    text = reply_call.call_args[1].get("text", reply_call.call_args[0][0] if reply_call.call_args[0] else "")

    # Should show browsing header
    assert "Browsing:" in text

    # Should have inline keyboard
    markup = reply_call.call_args[1].get("reply_markup")
    assert markup is not None


async def test_repo_with_multilevel_path_navigates(orchestrator, workspace):
    """'/repo projectA/src' should navigate into projectA/src."""
    update = _make_update("/repo projectA/src")
    ctx = _make_context(bot_data={"storage": MagicMock()})

    await orchestrator.agentic_repo(update, ctx)

    reply_call = update.message.reply_text
    text = reply_call.call_args[1].get("text", reply_call.call_args[0][0] if reply_call.call_args[0] else "")

    # src is a leaf dir, so should switch to it
    assert "Switched to" in text or "projectA/src" in text
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser_integration.py -v`
Expected: FAIL (current `agentic_repo` doesn't produce "Browsing:" header)

**Step 3: Rewrite `agentic_repo` method**

Modify `src/bot/orchestrator.py`. Replace the body of `agentic_repo` (lines ~1524-1693). Key changes:

1. Add imports at top of file:
```python
from .utils.repo_browser import (
    build_browse_header,
    build_browser_keyboard,
    is_branch_dir,
    list_visible_children,
    resolve_browse_path,
)
```

2. Replace `agentic_repo` body:
```python
async def agentic_repo(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Navigable directory browser.

    /repo          — browse current directory (or workspace root)
    /repo <path>   — navigate to path (multi-level supported)
    """
    args = update.message.text.split()[1:] if update.message.text else []
    roots = self.settings.approved_directories
    storage = context.bot_data.get("storage")

    # Determine current browse location
    browse_root = context.user_data.get("repo_browse_root")
    browse_rel = context.user_data.get("repo_browse_rel", "")

    if not browse_root or browse_root not in roots:
        browse_root = roots[0]
        browse_rel = ""

    if args:
        # /repo <path> — resolve multi-level path
        target_name = " ".join(args)
        target_path = resolve_browse_path(target_name, roots)

        if not target_path:
            await update.message.reply_text(
                f"Directory not found: <code>{escape_html(target_name)}</code>",
                parse_mode="HTML",
            )
            return

        # Find which root this path is under
        target_root = next(
            (r for r in roots if target_path == r or target_path.is_relative_to(r)),
            None,
        )
        if not target_root:
            await update.message.reply_text(
                f"Directory not found: <code>{escape_html(target_name)}</code>",
                parse_mode="HTML",
            )
            return

        if is_branch_dir(target_path):
            # Navigate into it — show browser
            context.user_data["repo_browse_root"] = target_root
            context.user_data["repo_browse_rel"] = str(
                target_path.relative_to(target_root)
            )
            await self._send_repo_browser(
                update.message, target_path, target_root, roots, context
            )
        else:
            # Leaf — select it
            await self._select_directory(
                update.message, target_path, storage, context
            )
        return

    # No args — show browser at current browse location
    browse_dir = browse_root / browse_rel if browse_rel else browse_root
    if not browse_dir.is_dir():
        browse_dir = browse_root
        browse_rel = ""
        context.user_data["repo_browse_rel"] = ""

    await self._send_repo_browser(
        update.message, browse_dir, browse_root, roots, context
    )
```

3. Add two helper methods to `MessageOrchestrator`:

```python
async def _send_repo_browser(
    self,
    message,  # telegram.Message
    browse_dir: Path,
    workspace_root: Path,
    roots: list,
    context: ContextTypes.DEFAULT_TYPE,
    edit: bool = False,
) -> None:
    """Render the directory browser for browse_dir."""
    header = build_browse_header(browse_dir, workspace_root)
    children = list_visible_children(browse_dir)

    # Build file listing text
    lines = [header, ""]
    for child in children:
        is_git = (child / ".git").is_dir()
        icon = "\U0001f4e6" if is_git else "\U0001f4c1"
        branch_marker = " \u25b6" if is_branch_dir(child) else ""
        lines.append(f"{icon} <code>{escape_html(child.name)}/</code>{branch_marker}")

    if not children:
        lines.append("<i>No subdirectories</i>")

    keyboard = build_browser_keyboard(
        browse_dir=browse_dir,
        workspace_root=workspace_root,
        multi_root=len(roots) > 1,
    )
    from telegram import InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    text = "\n".join(lines)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def _select_directory(
    self,
    message,  # telegram.Message or callback query message
    target_path: Path,
    storage,
    context: ContextTypes.DEFAULT_TYPE,
    edit: bool = False,
) -> None:
    """Select a directory: set as working dir, resume session."""
    user_id = context._user_id if hasattr(context, '_user_id') else None

    context.user_data["current_directory"] = target_path

    # Persist to database
    if storage and user_id:
        await storage.save_user_directory(user_id, str(target_path))

    # Look for resumable session
    session_id = None
    client_manager = context.bot_data.get("client_manager")
    if client_manager:
        session_id = client_manager.get_latest_session(str(target_path))
    else:
        claude_integration = context.bot_data.get("claude_integration")
        if claude_integration:
            session_id = claude_integration._find_resumable_session_id(target_path)
    context.user_data["claude_session_id"] = session_id

    is_git = (target_path / ".git").is_dir()
    git_badge = " (git)" if is_git else ""
    session_badge = " · session resumed" if session_id else ""

    text = (
        f"Switched to <code>{escape_html(target_path.name)}/</code>"
        f"{git_badge}{session_badge}"
    )

    if edit:
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.reply_text(text, parse_mode="HTML")
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser_integration.py tests/unit/test_orchestrator.py -v`
Expected: All tests PASS (including existing orchestrator tests)

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_bot/test_repo_browser_integration.py
git commit -m "feat: wire up /repo command to use navigable directory browser"
```

---

### Task 7: Update callback handler for nav:/sel: prefixes

**Files:**
- Modify: `src/bot/orchestrator.py` (callback pattern + `_agentic_callback`)

**Step 1: Write the failing tests**

Append to `tests/unit/test_bot/test_repo_browser_integration.py`:

```python
async def test_nav_callback_edits_message(orchestrator, workspace):
    """nav: callback should edit message with new directory listing."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "nav:projectA"
    query.from_user.id = 123
    query.message.edit_text = AsyncMock()
    query.message.chat = MagicMock()

    update = MagicMock()
    update.callback_query = query

    ctx = _make_context(
        user_data={"repo_browse_root": workspace, "repo_browse_rel": ""},
        bot_data={"storage": MagicMock(), "client_manager": MagicMock()},
    )

    await orchestrator._agentic_callback(update, ctx)

    query.message.edit_text.assert_called_once()
    text = query.message.edit_text.call_args[0][0]
    assert "Browsing:" in text
    assert "projectA" in text


async def test_sel_callback_switches_directory(orchestrator, workspace):
    """sel: callback should switch working directory."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "sel:projectB"
    query.from_user.id = 123
    query.message.edit_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    storage_mock = MagicMock()
    storage_mock.save_user_directory = AsyncMock()
    client_mgr = MagicMock()
    client_mgr.get_latest_session = MagicMock(return_value=None)

    ctx = _make_context(
        user_data={"repo_browse_root": workspace, "repo_browse_rel": ""},
        bot_data={"storage": storage_mock, "client_manager": client_mgr},
    )

    await orchestrator._agentic_callback(update, ctx)

    assert ctx.user_data["current_directory"] == workspace / "projectB"
    query.message.edit_text.assert_called_once()


async def test_sel_dot_selects_current_browse_dir(orchestrator, workspace):
    """sel:. should select the current browse directory."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "sel:."
    query.from_user.id = 123
    query.message.edit_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    storage_mock = MagicMock()
    storage_mock.save_user_directory = AsyncMock()
    client_mgr = MagicMock()
    client_mgr.get_latest_session = MagicMock(return_value=None)

    ctx = _make_context(
        user_data={
            "repo_browse_root": workspace,
            "repo_browse_rel": "projectA",
        },
        bot_data={"storage": storage_mock, "client_manager": client_mgr},
    )

    await orchestrator._agentic_callback(update, ctx)

    assert ctx.user_data["current_directory"] == workspace / "projectA"


async def test_nav_dotdot_goes_up(orchestrator, workspace):
    """nav:.. from projectA should go back to root."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "nav:.."
    query.from_user.id = 123
    query.message.edit_text = AsyncMock()
    query.message.chat = MagicMock()

    update = MagicMock()
    update.callback_query = query

    ctx = _make_context(
        user_data={
            "repo_browse_root": workspace,
            "repo_browse_rel": "projectA",
        },
        bot_data={"storage": MagicMock()},
    )

    await orchestrator._agentic_callback(update, ctx)

    query.message.edit_text.assert_called_once()
    text = query.message.edit_text.call_args[0][0]
    assert "Browsing:" in text
    # Should now be at root level
    assert ctx.user_data["repo_browse_rel"] == ""
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser_integration.py -v -k "callback"`
Expected: FAIL (current callback handler doesn't recognize `nav:` or `sel:` prefixes)

**Step 3: Update callback registration and handler**

In `_register_agentic_handlers`, update the pattern to include `nav:` and `sel:`:

```python
# Line ~352: Update callback pattern
pattern=r"^(cd:|nav:|sel:|session:|skill:|model:)",
```

In `_agentic_callback`, add handling for `nav:` and `sel:` prefixes before the existing `cd:` block:

```python
# Handle nav: callbacks (browse into directory)
if prefix == "nav":
    roots = self.settings.approved_directories
    browse_root = context.user_data.get("repo_browse_root", roots[0])
    browse_rel = context.user_data.get("repo_browse_rel", "")

    if value == "..":
        # Go up one level
        if browse_rel:
            parent_rel = str(Path(browse_rel).parent)
            new_rel = "" if parent_rel == "." else parent_rel
        else:
            # At root — if multi-root, show root picker (future)
            # For now, stay at root
            new_rel = ""

        context.user_data["repo_browse_rel"] = new_rel
        browse_dir = browse_root / new_rel if new_rel else browse_root
    else:
        # Navigate into directory
        browse_dir = (browse_root / value).resolve()
        if not browse_dir.is_dir() or not browse_dir.is_relative_to(browse_root):
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(value)}</code>",
                parse_mode="HTML",
            )
            return
        context.user_data["repo_browse_rel"] = str(
            browse_dir.relative_to(browse_root)
        )
        context.user_data["repo_browse_root"] = browse_root

    browse_dir = (
        browse_root / context.user_data["repo_browse_rel"]
        if context.user_data["repo_browse_rel"]
        else browse_root
    )
    await self._send_repo_browser(
        query.message, browse_dir, browse_root, roots, context, edit=True
    )
    return

# Handle sel: callbacks (select directory)
if prefix == "sel":
    roots = self.settings.approved_directories
    browse_root = context.user_data.get("repo_browse_root", roots[0])
    browse_rel = context.user_data.get("repo_browse_rel", "")
    storage = context.bot_data.get("storage")

    if value == ".":
        target_path = browse_root / browse_rel if browse_rel else browse_root
    else:
        target_path = (browse_root / value).resolve()

    if not target_path.is_dir():
        await query.edit_message_text(
            f"Directory not found: <code>{escape_html(value)}</code>",
            parse_mode="HTML",
        )
        return

    # Validate security boundary
    if not any(
        target_path == r or target_path.is_relative_to(r) for r in roots
    ):
        await query.edit_message_text("Access denied.", parse_mode="HTML")
        return

    await self._select_directory(
        query.message, target_path, storage, context, edit=True
    )

    # Audit log
    audit_logger = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=query.from_user.id,
            command="repo",
            args=[str(target_path)],
            success=True,
        )
    return
```

**Step 4: Run all tests**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser_integration.py tests/unit/test_bot/test_repo_browser.py tests/unit/test_orchestrator.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/bot/orchestrator.py tests/unit/test_bot/test_repo_browser_integration.py
git commit -m "feat: add nav:/sel: callback handling for repo directory browser"
```

---

### Task 8: Ensure backward compatibility with old cd: callbacks

**Files:**
- Modify: `src/bot/orchestrator.py` (the existing `cd:` handler block in `_agentic_callback`)

**Step 1: Write the failing test**

Append to `tests/unit/test_bot/test_repo_browser_integration.py`:

```python
async def test_old_cd_callback_still_works(orchestrator, workspace):
    """Old cd:{abs_path} callbacks from existing messages should still work."""
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = f"cd:{workspace / 'projectB'}"
    query.from_user.id = 123
    query.message.edit_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    storage_mock = MagicMock()
    storage_mock.save_user_directory = AsyncMock()
    client_mgr = MagicMock()
    client_mgr.get_latest_session = MagicMock(return_value="session-abc")

    ctx = _make_context(
        user_data={},
        bot_data={
            "storage": storage_mock,
            "client_manager": client_mgr,
            "audit_logger": MagicMock(),
        },
    )

    await orchestrator._agentic_callback(update, ctx)

    assert ctx.user_data["current_directory"] == workspace / "projectB"
    assert ctx.user_data["claude_session_id"] == "session-abc"
    query.message.edit_text.assert_called_once()
    text = query.message.edit_text.call_args[0][0]
    assert "Switched to" in text
    assert "session resumed" in text
```

**Step 2: Run test to verify it passes (existing code should handle this)**

Run: `uv run pytest tests/unit/test_bot/test_repo_browser_integration.py -v -k "old_cd"`
Expected: PASS (existing `cd:` handler is unchanged)

**Step 3: No implementation change needed — this is a regression test**

The existing `cd:` handler block in `_agentic_callback` remains untouched.

**Step 4: Commit**

```bash
git add tests/unit/test_bot/test_repo_browser_integration.py
git commit -m "test: add backward compat regression test for old cd: callbacks"
```

---

### Task 9: Run full test suite and lint

**Step 1: Run all tests**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests PASS

**Step 2: Run linter**

Run: `make lint`
Expected: No errors

**Step 3: Fix any issues found**

If tests or lint fail, fix and re-run.

**Step 4: Final commit if any fixes were needed**

```bash
git add -u
git commit -m "fix: address lint and test issues from repo browser feature"
```
