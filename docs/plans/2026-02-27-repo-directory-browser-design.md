# Repo Directory Browser Design

Turn `/repo` from a flat 1-level directory lister into a navigable directory browser with unlimited depth, `.` (select current), and `..` (go up).

## Requirements

- Add `.` to select the currently browsed directory (switch working dir + resume session)
- Add `..` to navigate up to parent directory
- Remove the 1-level depth limit — allow browsing into subdirectories
- `/repo path/to/dir` supports multi-level relative paths
- No regressions on session switching or resume behavior

## Directory Classification & Filtering

**Filtered out** (never shown):
- Dotfiles (`.git`, `.venv`, `.cache`, etc.)
- Common non-project dirs: `node_modules`, `__pycache__`, `target`, `build`, `dist`, `.tox`

**Two types of visible directories:**
- **Branch** — has visible child directories → clicking navigates in (re-renders listing in place)
- **Leaf** — no visible child directories → clicking selects it (switches working dir + resumes session)

Existing "click to switch" behavior is preserved for leaf directories.

## Navigation Buttons

Three button types in every listing:

1. **`.` (Select here)** — Top of keyboard. Selects the currently browsed directory as working dir, resumes session if available. Same switch logic as today.
2. **`..` (Go up)** — Next to `.`. Goes to parent. At workspace root: multiple roots → root picker view; single root → hidden.
3. **Directory entries** — One per visible subdirectory. Branch dirs navigate deeper (edit message in place). Leaf dirs select immediately.

**Message editing:** Navigation edits the existing message in place rather than sending new messages.

**Header:** Shows current path, e.g. `📂 Browsing: myproject/src/`

**`/repo path/to/dir`:** Resolves multi-level relative path against workspace roots. Branch target → show listing. Leaf target → select it.

## Callback Data Format

Relative paths from workspace root, with action prefixes:
- `sel:{rel_path}` — select directory (switch + resume session)
- `nav:{rel_path}` — navigate into directory (re-render listing)
- `nav:..` — go up one level
- `sel:.` — select current browsed directory

**Browsing state** in `user_data`:
- `repo_browse_root` — which workspace root we're under (Path)
- `repo_browse_rel` — relative path within that root (str)

## Session Resume (no regression)

The select action (`sel:` / `sel:.`) runs the same logic as today's `cd:` handler:
1. Resolve full path from root + relative
2. Set `current_directory` in `user_data`
3. Persist to database via `storage.save_user_directory()`
4. Look up resumable session via `ClientManager.get_latest_session()`
5. Set `claude_session_id` in `user_data`
6. Show confirmation with git badge + session resumed badge

## Backward Compatibility

Old `cd:{abs_path}` callbacks from existing messages in chat are still handled — the callback handler checks the prefix and routes accordingly.

## Edge Cases

- **Stale keyboards:** Directory renamed/deleted after listing → show "Directory not found" error.
- **Security boundary:** All resolved paths validated against `approved_directories`. Cannot navigate outside workspace roots.
- **`..` at root:** Single root → button hidden. Multiple roots → root picker view.

## Approach

Relative-path callbacks (Approach 3). Acceptable trade-off: 64-byte Telegram callback_data limit constrains very deep nesting, but paths won't be deeply nested in practice.
