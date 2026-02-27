"""HTML formatting utilities for Telegram messages.

Telegram's HTML mode only requires escaping 3 characters (<, >, &) vs the many
ambiguous Markdown v1 metacharacters, making it far more robust for rendering
Claude's output which contains underscores, asterisks, brackets, etc.
"""

import re
from typing import List, Tuple


def escape_html(text: str) -> str:
    """Escape the 3 HTML-special characters for Telegram.

    This replaces all 3 _escape_markdown functions previously scattered
    across the codebase.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_telegram_html(text: str) -> str:
    """Convert Claude's markdown output to Telegram-compatible HTML.

    Telegram supports a narrow HTML subset: <b>, <i>, <code>, <pre>,
    <a href>, <s>, <u>. This function converts common markdown patterns
    to that subset while preserving code blocks verbatim.

    Order of operations:
    1. Extract fenced code blocks -> placeholders
    2. Extract inline code -> placeholders
    3. HTML-escape remaining text
    4. Convert bold (**text** / __text__)
    5. Convert italic (*text*, _text_ with word boundaries)
    6. Convert links [text](url)
    7. Convert headers (# Header -> <b>Header</b>)
    8. Convert strikethrough (~~text~~)
    9. Restore placeholders
    """
    placeholders: List[Tuple[str, str]] = []
    placeholder_counter = 0

    def _make_placeholder(html_content: str) -> str:
        nonlocal placeholder_counter
        key = f"\x00PH{placeholder_counter}\x00"
        placeholder_counter += 1
        placeholders.append((key, html_content))
        return key

    # --- 1. Extract fenced code blocks ---
    def _replace_fenced(m: re.Match) -> str:  # type: ignore[type-arg]
        lang = m.group(1) or ""
        code = m.group(2)
        escaped_code = escape_html(code)
        if lang:
            html = f'<pre><code class="language-{escape_html(lang)}">{escaped_code}</code></pre>'
        else:
            html = f"<pre><code>{escaped_code}</code></pre>"
        return _make_placeholder(html)

    text = re.sub(
        r"```(\w+)?\n(.*?)```",
        _replace_fenced,
        text,
        flags=re.DOTALL,
    )

    # --- 1b. Convert markdown tables to <pre> blocks ---
    def _replace_table(m: re.Match) -> str:  # type: ignore[type-arg]
        table_text = m.group(0)
        rows = [row.strip() for row in table_text.strip().split("\n")]
        parsed_rows = []
        for row in rows:
            # Strip leading/trailing pipes and split
            cells = [c.strip() for c in row.strip("|").split("|")]
            parsed_rows.append(cells)

        if len(parsed_rows) < 2:
            return table_text  # Not a valid table

        # Skip separator row (row with only dashes/colons)
        data_rows = [
            r
            for r in parsed_rows
            if not all(re.match(r"^[:\-]+$", c.strip()) for c in r)
        ]

        if not data_rows:
            return table_text

        # Calculate column widths
        num_cols = max(len(r) for r in data_rows)
        col_widths = [0] * num_cols
        for row in data_rows:
            for i, cell in enumerate(row):
                if i < num_cols:
                    col_widths[i] = max(col_widths[i], len(cell))

        # Build aligned output
        lines = []
        for row_idx, row in enumerate(data_rows):
            padded = []
            for i in range(num_cols):
                cell = row[i] if i < len(row) else ""
                padded.append(cell.ljust(col_widths[i]))
            lines.append("  ".join(padded))
            # Add separator after header
            if row_idx == 0 and len(data_rows) > 1:
                sep = "  ".join("─" * w for w in col_widths)
                lines.append(sep)

        pre_content = escape_html("\n".join(lines))
        return _make_placeholder(f"<pre>{pre_content}</pre>")

    # Match consecutive lines that look like table rows (start/end with |)
    text = re.sub(
        r"(?:^[ \t]*\|.+\|[ \t]*$\n?){2,}",
        _replace_table,
        text,
        flags=re.MULTILINE,
    )

    # --- 2. Extract inline code ---
    def _replace_inline_code(m: re.Match) -> str:  # type: ignore[type-arg]
        code = m.group(1)
        escaped_code = escape_html(code)
        return _make_placeholder(f"<code>{escaped_code}</code>")

    text = re.sub(r"`([^`\n]+)`", _replace_inline_code, text)

    # --- 3. HTML-escape remaining text ---
    text = escape_html(text)

    # --- 4. Bold: **text** or __text__ ---
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # --- 5. Italic: *text* (require non-space after/before) ---
    text = re.sub(r"\*(\S.*?\S|\S)\*", r"<i>\1</i>", text)
    # _text_ only at word boundaries (avoid my_var_name)
    text = re.sub(r"(?<!\w)_(\S.*?\S|\S)_(?!\w)", r"<i>\1</i>", text)

    # --- 6. Links: [text](url) ---
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )

    # --- 7. Headers: # Header -> <b>Header</b> ---
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # --- 8. Strikethrough: ~~text~~ ---
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # --- 9. Restore placeholders ---
    for key, html_content in placeholders:
        text = text.replace(key, html_content)

    return text
