"""Repo directory browser utilities.

Provides directory listing, classification (branch vs leaf), and keyboard
building for the /repo navigable directory browser.
"""

from pathlib import Path
from typing import List, Optional

from telegram import InlineKeyboardButton

# Directories filtered from browser listings (dotfiles handled separately)
FILTERED_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "target",
        "build",
        "dist",
        ".tox",
    }
)


def list_visible_children(directory: Path) -> List[Path]:
    """List visible child directories, filtering dotfiles and noise."""
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


def is_branch_dir(directory: Path) -> bool:
    """Check if directory has visible child directories (is navigable)."""
    return len(list_visible_children(directory)) > 0


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

    # Directory entries (2 per row)
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


def build_browse_header(browse_dir: Path, workspace_root: Path) -> str:
    """Build the header text showing current browse location."""
    rel = browse_dir.relative_to(workspace_root)
    display = "/" if str(rel) == "." else f"{rel}/"
    return f"\U0001f4c2 <b>Browsing:</b> <code>{display}</code>"


def resolve_browse_path(target: str, roots: List[Path]) -> Optional[Path]:
    """Resolve a relative path against workspace roots.

    Supports multi-level paths like 'project/src/handlers'. Searches
    each root in order and returns the first match.
    """
    for root in roots:
        candidate = (root / target).resolve()
        if candidate.is_dir() and candidate.is_relative_to(root):
            return candidate
    return None
