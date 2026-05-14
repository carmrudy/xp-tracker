"""
D&D-style cumulative XP thresholds (levels 1–20). Source: PHB-style progression.

`LEVEL_XP_THRESHOLDS[L - 1]` = minimum total XP to *be* level L.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

# Index i = minimum total XP to have reached level (i + 1).
LEVEL_XP_THRESHOLDS: tuple[int, ...] = (
    0,
    300,
    900,
    2700,
    6500,
    14000,
    23000,
    34000,
    48000,
    64000,
    85000,
    100000,
    120000,
    140000,
    165000,
    195000,
    225000,
    265000,
    305000,
    355000,
)

MAX_LEVEL = len(LEVEL_XP_THRESHOLDS)


@dataclass(frozen=True)
class DerivedProgress:
    """total_xp is authoritative; level and xp_to_next_level match the table."""

    total_xp: int
    level: int
    xp_to_next_level: int


def progress_from_total(total_xp: int) -> DerivedProgress:
    """Map total XP to current level and XP still needed for the next level (0 at max)."""
    t = max(0, int(total_xp))
    level = bisect.bisect_right(LEVEL_XP_THRESHOLDS, t)
    if level >= MAX_LEVEL:
        return DerivedProgress(total_xp=t, level=MAX_LEVEL, xp_to_next_level=0)
    nxt = LEVEL_XP_THRESHOLDS[level]
    return DerivedProgress(total_xp=t, level=level, xp_to_next_level=nxt - t)
