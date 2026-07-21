"""Pretty console tables for benchmark result printing."""

from __future__ import annotations

from typing import Any, Optional, Sequence


def _cell(value: Any, width: int, *, align: str = "left") -> str:
    text = "" if value is None else str(value)
    if len(text) > width:
        text = text[: max(width - 1, 1)] + "…"
    if align == "right":
        return text.rjust(width)
    if align == "center":
        return text.center(width)
    return text.ljust(width)


def _col_widths(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    min_width: int = 4,
) -> list[int]:
    widths = [max(min_width, len(str(h))) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    return widths


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    aligns: Optional[Sequence[str]] = None,
    title: str = "",
) -> str:
    """Render a Unicode box table.

    ``aligns`` entries are ``left`` / ``right`` / ``center`` per column.
    """
    if not headers:
        return ""
    n = len(headers)
    aligns = list(aligns or (["left"] + ["right"] * (n - 1)))
    while len(aligns) < n:
        aligns.append("right")
    widths = _col_widths(headers, rows)

    def line(left: str, mid: str, right: str, fill: str = "─") -> str:
        parts = [fill * (w + 2) for w in widths]
        return left + mid.join(parts) + right

    def row_line(cells: Sequence[Any]) -> str:
        parts = []
        for i, w in enumerate(widths):
            val = cells[i] if i < len(cells) else ""
            parts.append(f" {_cell(val, w, align=aligns[i])} ")
        return "│" + "│".join(parts) + "│"

    out: list[str] = []
    if title:
        out.append(title)
    out.append(line("┌", "┬", "┐"))
    out.append(row_line(headers))
    out.append(line("├", "┼", "┤"))
    for row in rows:
        out.append(row_line(row))
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    aligns: Optional[Sequence[str]] = None,
    title: str = "",
) -> None:
    print(format_table(headers, rows, aligns=aligns, title=title))


def format_kv_table(
    pairs: Sequence[tuple[str, Any]],
    *,
    title: str = "",
    key_header: str = "Field",
    value_header: str = "Value",
) -> str:
    rows = [(k, v) for k, v in pairs]
    return format_table(
        [key_header, value_header],
        rows,
        aligns=["left", "left"],
        title=title,
    )


def print_kv_table(
    pairs: Sequence[tuple[str, Any]],
    *,
    title: str = "",
    key_header: str = "Field",
    value_header: str = "Value",
) -> None:
    print(
        format_kv_table(
            pairs,
            title=title,
            key_header=key_header,
            value_header=value_header,
        )
    )


def fmt_float(value: Any, digits: int = 3) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def percentile_metric_rows(
    metrics: dict[str, Any],
    names: Sequence[tuple[str, str, str]],
) -> list[list[str]]:
    """Build rows for (label, unit) metrics that have mean/p50/p95/p99.

    ``names``: list of ``(key, label, unit)``.
    """
    rows: list[list[str]] = []
    for key, label, unit in names:
        stats = metrics.get(key)
        if not isinstance(stats, dict):
            continue
        display = f"{label} ({unit})" if unit else label
        rows.append(
            [
                display,
                fmt_float(stats.get("mean")),
                fmt_float(stats.get("p50")),
                fmt_float(stats.get("p95")),
                fmt_float(stats.get("p99")),
            ]
        )
    return rows
