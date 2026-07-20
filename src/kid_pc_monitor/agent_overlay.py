"""Formatting and placement for the kid-side on-screen time-remaining overlay.

Pure logic with no GUI or OS dependencies so it can be unit-tested anywhere.
The Windows agent renders the returned text in a small always-on-top window
(see ``WindowsHostPlatform.update_time_overlay``); platforms without a desktop
UI ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime

# Below this many minutes remaining, the overlay switches to an urgent style
# (red/bold) so the last stretch before a lock is unmissable.
URGENT_THRESHOLD_MINUTES = 5.0

# The corners the kid can move the overlay to.
VALID_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


@dataclass(frozen=True)
class OverlayState:
    """What the on-screen overlay should currently show.

    ``text`` is the headline countdown to the next lock (whichever of bedtime or
    the allowance comes first). ``detail`` holds the optional breakdown lines
    that explain *why* that number is what it is — bedtime and the allowance
    balance are separate budgets, and the kid only ever sees the smaller one.
    """

    text: str
    urgent: bool
    detail: str = ""


def format_duration(minutes: float) -> str:
    """Render ``minutes`` as ``M:SS``, or ``H:MM:SS`` once it reaches an hour."""
    total_seconds = max(1, int(minutes * 60))
    hours, remainder = divmod(total_seconds, 3600)
    mins, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{mins:02d}:{seconds:02d}"
    return f"{mins}:{seconds:02d}"


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
    return f"Time left: {format_duration(minutes_remaining)}"


def overlay_detail_lines(
    *,
    bed_time: dtime | None = None,
    minutes_until_bedtime: float | None = None,
    allowance_minutes_left: float | None = None,
    carryover_minutes: int = 0,
) -> list[str]:
    """Break the headline countdown down into the budgets that produced it.

    Returns a bedtime line (clock time plus a live countdown to it) and an
    allowance line (time left in today's screen-time budget, noting how much of
    it came from banked carry-over). Either is omitted when it does not apply,
    so a kid with only a bedtime and no daily cap sees just the one line.
    """
    lines: list[str] = []

    if bed_time is not None:
        label = f"Bedtime {bed_time.hour:02d}:{bed_time.minute:02d}"
        if minutes_until_bedtime is not None and minutes_until_bedtime > 0:
            label += f" — in {format_duration(minutes_until_bedtime)}"
        lines.append(label)

    if allowance_minutes_left is not None:
        label = f"Allowance left: {format_duration(max(0.0, allowance_minutes_left))}"
        if carryover_minutes > 0:
            label += f" (incl. {carryover_minutes} min saved)"
        lines.append(label)

    return lines


def overlay_state(
    minutes_remaining: float | None,
    *,
    urgent_below_minutes: float = URGENT_THRESHOLD_MINUTES,
    bed_time: dtime | None = None,
    minutes_until_bedtime: float | None = None,
    allowance_minutes_left: float | None = None,
    carryover_minutes: int = 0,
) -> OverlayState | None:
    """Return the overlay text, urgency, and detail, or ``None`` when nothing shows.

    Urgency is on once ``minutes_remaining`` drops to ``urgent_below_minutes`` or
    below (but is still positive), which the renderer uses to switch to a red
    style for the final stretch before a lock. The keyword arguments are optional
    context used to build the detail lines; omitting them yields a bare countdown.
    """
    label = overlay_label(minutes_remaining)
    if label is None or minutes_remaining is None:
        return None
    detail = overlay_detail_lines(
        bed_time=bed_time,
        minutes_until_bedtime=minutes_until_bedtime,
        allowance_minutes_left=allowance_minutes_left,
        carryover_minutes=carryover_minutes,
    )
    return OverlayState(
        text=label,
        urgent=minutes_remaining <= urgent_below_minutes,
        detail="\n".join(detail),
    )


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
