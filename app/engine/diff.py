"""
Diff engine — compare regulatory snapshots and detect changes.
"""

from __future__ import annotations

import difflib
import hashlib
import uuid

from app.schemas import DiffResult


def hash_text(text: str) -> str:
    """Return an SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_diff(
    record_id: uuid.UUID,
    old_content: str,
    new_content: str,
    context_lines: int = 3,
) -> DiffResult | None:
    """
    Produce a unified diff between two content snapshots.

    Returns ``None`` when the two snapshots are identical (no change).
    """
    old_hash = hash_text(old_content)
    new_hash = hash_text(new_content)

    if old_hash == new_hash:
        return None  # no change

    diff_lines = list(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile="previous",
            tofile="current",
            n=context_lines,
        )
    )

    return DiffResult(
        record_id=record_id,
        previous_hash=old_hash,
        new_hash=new_hash,
        diff_summary="".join(diff_lines),
    )
