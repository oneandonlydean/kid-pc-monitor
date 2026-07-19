"""Formatting for the kid-side on-screen time-remaining overlay.

Pure helper with no GUI or OS dependencies so it can be unit-tested anywhere.
The Windows agent renders the returned text in a small always-on-top window
(see ``WindowsHostPlatform.update_time_overlay``); platforms without a desktop
UI ignore it.
"""

from __future__ import annotations

import math


def overlay_label(minutes_remaining: float | None) -> str | None:
    """Return the on-screen countdown text, or ``None`` when nothing should show.

    ``minutes_remaining`` is the value produced by ``minutes_until_lock``:
    ``None`` when no daily allowance or bedtime applies, ``0`` when already at a
    limit (the agent locks, so the overlay hides), or the minutes until the next
    lock. The value is rounded up to whole minutes to match the agent's warning
    popups, so ``"Time left: 1 min"`` shows until the screen actually locks.
    """
    if minutes_remaining is None or minutes_remaining <= 0:
        return None

    total_minutes = max(1, math.ceil(minutes_remaining))
    if total_minutes >= 60:
        hours, minutes = divmod(total_minutes, 60)
        return f"Time left: {hours}h {minutes:02d}m"
    return f"Time left: {total_minutes} min"
