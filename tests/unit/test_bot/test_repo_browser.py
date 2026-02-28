"""Tests for repo browser utilities."""

from pathlib import Path

import pytest

from src.bot.utils.repo_browser import (
    FILTERED_DIRS,
    build_browse_header,
    build_browser_keyboard,
    is_branch_dir,
    list_visible_children,
    resolve_browse_path,
)


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
