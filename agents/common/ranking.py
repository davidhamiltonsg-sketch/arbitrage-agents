"""Multi-key descending sort and top-N slice."""
from __future__ import annotations

from typing import Any, Iterable, Sequence


def rank(items: Iterable[dict[str, Any]], *, keys: Sequence[tuple[str, bool]], top: int) -> list[dict[str, Any]]:
    """Sort ``items`` by ``keys`` ((field, descending), ...) and return the first ``top``.

    Missing or non-numeric values sort last. ``top`` <= 0 returns everything.
    """
    def sort_key(item: dict[str, Any]):
        parts = []
        for field, descending in keys:
            value = item.get(field)
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = float("-inf") if descending else float("inf")
            parts.append(-number if descending else number)
        return tuple(parts)

    ordered = sorted(items, key=sort_key)
    return ordered[:top] if top > 0 else ordered
