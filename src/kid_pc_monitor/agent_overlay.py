"""Formatting and placement for the kid-side on-screen time-remaining overlay.

Pure logic with no GUI or OS dependencies so it can be unit-tested anywhere.
The Windows agent renders the returned text in a small always-on-top window
(see ``WindowsHostPlatform.update_time_overlay``); platforms without a desktop
UI ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Below this many minutes remaining, the overlay switches to an urgent style
# (red/bold) so the last stretch before a lock is unmissable.
URGENT_THRESHOLD_MINUTES = 5.0

# The corners the kid can move the overlay to.
VALID_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


@dataclass(frozen=True)
class OverlayState:
    """What the on-screen overlay should currently show."""

    text: str
    urgent: bool


def overlay_label(minutes_remaining: float | None) -> str | None:
    """Return the on-screen countdown text, or ``None`` when nothing should show.

    ``minutes_remaining`` is the value produced by ``minutes_until_lock``:
    ``None`` when no daily allowance or bedtime applies, ``0`` when already at a
    limit (the agent locks, so the overlay hides), or the minutes until the next
    lock. The value is shown as a live ``M:SS`` (or ``H:MM:SS``) countdown that
    ticks down each second; at least ``0:01`` shows until the screen locks.
    """
    if minutes_remaining is None or minutes_remaining <= 0:
        return None

    total_seconds = max(1, int(minutes_remaining * 60))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"Time left: {hours}:{minutes:02d}:{seconds:02d}"
    return f"Time left: {minutes}:{seconds:02d}"


def overlay_state(
    minutes_remaining: float | None,
    *,
    urgent_below_minutes: float = URGENT_THRESHOLD_MINUTES,
) -> OverlayState | None:
    """Return the overlay text and urgency, or ``None`` when nothing should show.

    Urgency is on once ``minutes_remaining`` drops to ``urgent_below_minutes`` or
    below (but is still positive), which the renderer uses to switch to a red
    style for the final stretch before a lock.
    """
    label = overlay_label(minutes_remaining)
    if label is None or minutes_remaining is None:
        return None
    return OverlayState(text=label, urgent=minutes_remaining <= urgent_below_minutes)


def corner_geometry(
    corner: str,
    monitor_rect: tuple[int, int, int, int],
    win_w: int,
    win_h: int,
    *,
    margin: int = 20,
) -> tuple[int, int]:
    """Return the (x, y) top-left position to place a ``win_w`` x ``win_h`` window
    in the given ``corner`` of a monitor.

    ``monitor_rect`` is ``(left, top, right, bottom)`` in virtual-screen pixels,
    so this works for any monitor. Unknown corners fall back to top-right. The
    result is clamped to the monitor's top-left so the window is never pushed off
    the visible area on that side.
    """
    if corner not in VALID_CORNERS:
        corner = "top-right"
    left, top, right, bottom = monitor_rect
    x = left + margin if "left" in corner else right - win_w - margin
    y = top + margin if "top" in corner else bottom - win_h - margin
    return max(left, int(x)), max(top, int(y))
