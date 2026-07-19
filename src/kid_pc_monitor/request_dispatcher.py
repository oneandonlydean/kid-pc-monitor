"""Execute validated agent protocol requests against a control object."""

from __future__ import annotations

from typing import Any

from kid_pc_monitor.agent_protocol import (
    CLEARABLE_VARIABLES,
    DEFAULT_LOG_TAIL_LINES,
    DEFAULT_SHUTDOWN_SECONDS,
    FORBIDDEN,
    INVALID_VALUE,
    MAX_BREAK_DURATION,
    MAX_BREAK_INTERVAL,
    MAX_DAILY_LIMIT,
    MAX_EARN_CAP,
    MAX_EARN_QUESTIONS,
    MAX_EARN_REWARD,
    MAX_FRAME_BYTES,
    MIN_DAILY_LIMIT,
    UNKNOWN_ACTION,
    UNKNOWN_VARIABLE,
    VARIABLES,
    WRITABLE_VARIABLES,
    Node,
    ProtocolError,
    Request,
    _parse_int,
    block_content,
    capabilities_content,
    ok_content,
    serialize,
)


def _format_time(value: Any) -> str | None:
    """Render a datetime.time as HH:MM, passing through None."""
    if value is None:
        return None
    return f"{value.hour:02d}:{value.minute:02d}"


def _parse_hhmm(raw: Any, req_id: str | None) -> tuple[int, int]:
    try:
        hour_str, minute_str = str(raw).split(":")
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        raise ProtocolError(INVALID_VALUE, "time must be HH:MM", req_id) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ProtocolError(INVALID_VALUE, "time out of range (00:00–23:59)", req_id)
    return hour, minute


def _parse_bool(raw: Any, req_id: str | None) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    raise ProtocolError(INVALID_VALUE, "expected a boolean", req_id)


def _read_variable(control: Any, var: str) -> Any:
    if var == "name":
        return control.platform.get_hostname()
    if var == "status":
        return "LOCKED" if control.check_if_locked() else "UNLOCKED"
    if var == "current_user":
        return control.current_user
    if var == "daily_limit":
        return control.daily.allowance
    if var == "bed_time":
        return _format_time(control.daily.bed_time)
    if var == "manual_lock":
        return bool(control.runtime.manual_lock_active)
    if var == "show_timer":
        return bool(control.daily.show_timer)
    if var == "time_request":
        requested_at = control.runtime.time_request_at
        return requested_at.isoformat() if requested_at is not None else None
    if var == "break_interval":
        return int(control.daily.break_interval_minutes)
    if var == "break_duration":
        return int(control.daily.break_duration_minutes)
    if var == "on_break":
        return bool(control.on_break())
    if var == "weekend_enabled":
        return bool(control.daily.weekend_enabled)
    if var == "weekend_bed_time":
        return _format_time(control.daily.weekend_bed_time)
    if var == "weekend_allowance":
        return control.daily.weekend_allowance
    if var == "earn_enabled":
        return bool(control.daily.earn_enabled)
    if var == "earn_reward":
        return int(control.daily.earn_reward_minutes)
    if var == "earn_questions":
        return int(control.daily.earn_questions)
    if var == "earn_cap":
        return int(control.daily.earn_daily_cap_minutes)
    if var == "earned_today":
        return int(control.runtime.earned_today_seconds // 60)
    if var == "wake_time":
        return _format_time(control.daily.wake_time)
    if var == "cumulative_extension":
        return int(control.runtime.cumulative_extension_seconds)
    if var == "accumulated_seconds":
        return int(control.runtime.accumulated_seconds)
    if var == "time_remaining":
        remaining = control.get_time_remaining()
        return None if remaining is None else round(remaining)
    if var == "enforcement_active":
        active, _reason = control.enforcement_lock_state()
        return active
    if var == "enforcement_reason":
        active, reason = control.enforcement_lock_state()
        return reason if active else None
    if var == "access_status":
        return control.get_access_status()
    raise ProtocolError(UNKNOWN_VARIABLE, f"unknown variable: {var}")


def _do_get(control: Any, req: Request) -> list[Node]:
    if req.var == "settings":
        children = [Node(name, [_read_variable(control, name)]) for name in VARIABLES]
        return block_content(Node("settings", children=children))
    return ok_content(_read_variable(control, req.var))  # type: ignore[arg-type]


def _do_set(control: Any, req: Request) -> list[Node]:
    var, val, req_id = req.var, req.val, req.id
    if var not in WRITABLE_VARIABLES:
        raise ProtocolError(FORBIDDEN, f"{var} is read-only", req_id)

    if var == "daily_limit":
        minutes = _parse_int(val, req_id)
        if not (MIN_DAILY_LIMIT <= minutes <= MAX_DAILY_LIMIT):
            raise ProtocolError(
                INVALID_VALUE,
                f"minutes must be between {MIN_DAILY_LIMIT} and {MAX_DAILY_LIMIT}",
                req_id,
            )
        control.set_daily_allowance(minutes)
        result: Any = minutes
    elif var == "bed_time":
        hour, minute = _parse_hhmm(val, req_id)
        control.set_bed_time(hour, minute)
        result = f"{hour:02d}:{minute:02d}"
    elif var == "wake_time":
        hour, minute = _parse_hhmm(val, req_id)
        control.set_wake_time(hour, minute)
        result = f"{hour:02d}:{minute:02d}"
    elif var == "show_timer":
        enabled = _parse_bool(val, req_id)
        control.set_show_timer(enabled)
        result = enabled
    elif var == "break_interval":
        minutes = _parse_int(val, req_id)
        if not (0 <= minutes <= MAX_BREAK_INTERVAL):
            raise ProtocolError(
                INVALID_VALUE, f"minutes must be between 0 and {MAX_BREAK_INTERVAL}", req_id
            )
        control.set_break_interval(minutes)
        result = minutes
    elif var == "break_duration":
        minutes = _parse_int(val, req_id)
        if not (1 <= minutes <= MAX_BREAK_DURATION):
            raise ProtocolError(
                INVALID_VALUE, f"minutes must be between 1 and {MAX_BREAK_DURATION}", req_id
            )
        control.set_break_duration(minutes)
        result = minutes
    elif var == "weekend_enabled":
        enabled = _parse_bool(val, req_id)
        control.set_weekend_enabled(enabled)
        result = enabled
    elif var == "weekend_bed_time":
        hour, minute = _parse_hhmm(val, req_id)
        control.set_weekend_bed_time(hour, minute)
        result = f"{hour:02d}:{minute:02d}"
    elif var == "weekend_allowance":
        minutes = _parse_int(val, req_id)
        if not (MIN_DAILY_LIMIT <= minutes <= MAX_DAILY_LIMIT):
            raise ProtocolError(
                INVALID_VALUE,
                f"minutes must be between {MIN_DAILY_LIMIT} and {MAX_DAILY_LIMIT}",
                req_id,
            )
        control.set_weekend_allowance(minutes)
        result = minutes
    elif var == "earn_enabled":
        enabled = _parse_bool(val, req_id)
        control.set_earn_enabled(enabled)
        result = enabled
    elif var == "earn_reward":
        minutes = _parse_int(val, req_id)
        if not (1 <= minutes <= MAX_EARN_REWARD):
            raise ProtocolError(
                INVALID_VALUE, f"minutes must be between 1 and {MAX_EARN_REWARD}", req_id
            )
        control.set_earn_reward(minutes)
        result = minutes
    elif var == "earn_questions":
        count = _parse_int(val, req_id)
        if not (1 <= count <= MAX_EARN_QUESTIONS):
            raise ProtocolError(
                INVALID_VALUE, f"questions must be between 1 and {MAX_EARN_QUESTIONS}", req_id
            )
        control.set_earn_questions(count)
        result = count
    elif var == "earn_cap":
        minutes = _parse_int(val, req_id)
        if not (0 <= minutes <= MAX_EARN_CAP):
            raise ProtocolError(
                INVALID_VALUE, f"minutes must be between 0 and {MAX_EARN_CAP}", req_id
            )
        control.set_earn_cap(minutes)
        result = minutes
    else:  # manual_lock
        engaged = _parse_bool(val, req_id)
        if engaged:
            control.engage_manual_lock()
        else:
            control.clear_manual_lock()
        result = engaged

    control.save_state()
    return ok_content(result)


def _do_clear(control: Any, req: Request) -> list[Node]:
    var, req_id = req.var, req.id
    if var not in CLEARABLE_VARIABLES:
        raise ProtocolError(FORBIDDEN, f"{var} cannot be cleared", req_id)

    if var == "daily_limit":
        control.set_daily_allowance(None)
    elif var == "bed_time":
        control.clear_bed_time()
        control.warnings_sent.clear()
    elif var == "cumulative_extension":
        control.clear_extensions()
    elif var == "time_request":
        control.clear_time_request()
    elif var == "break_interval":
        control.set_break_interval(0)
    elif var == "weekend_bed_time":
        control.clear_weekend_bed_time()
    elif var == "weekend_allowance":
        control.set_weekend_allowance(None)
    else:  # manual_lock
        control.clear_manual_lock()

    control.save_state()
    return ok_content("cleared")


def _do_extend(control: Any, req: Request) -> list[Node]:
    minutes = _parse_int(req.val, req.id)
    control.extend_time(minutes)
    control.save_state()
    return ok_content(minutes)


def _do_message(control: Any, req: Request) -> list[Node]:
    control.send_parent_message(str(req.val))
    return ok_content("message sent")


def _do_shutdown(control: Any, req: Request) -> list[Node]:
    seconds = DEFAULT_SHUTDOWN_SECONDS if req.val is None else _parse_int(req.val, req.id)
    if seconds < 0:
        raise ProtocolError(INVALID_VALUE, "seconds must not be negative", req.id)
    control.shutdown_pc(seconds)
    return ok_content(seconds)


def _logs_block_byte_size(path: str, lines: list[str], truncated: bool) -> int:
    """Estimate UTF-8 size of a signed v3 response carrying these log lines."""
    lines_node = Node("lines", args=lines)
    block = Node(
        "logs",
        children=[
            Node("path", [path]),
            Node("truncated", [truncated]),
            lines_node,
        ],
    )
    content = block_content(block)
    return len(
        serialize(
            [
                Node("v", [3]),
                Node("name", ["hostname"]),
                Node("timestamp", [0]),
                Node("nonce", ["a" * 32]),
                *content,
                Node("auth", children=[Node("signature", ["x" * 43])]),
            ]
        ).encode("utf-8")
    )


def _fit_log_lines_to_frame(path: str, lines: list[str], truncated: bool) -> tuple[list[str], bool]:
    """Drop oldest lines until the ``logs`` block fits under the frame cap."""
    budget = MAX_FRAME_BYTES - 1024
    while lines and _logs_block_byte_size(path, lines, truncated) > budget:
        lines = lines[1:]
        truncated = True
    return lines, truncated


def _do_get_logs(control: Any, req: Request) -> list[Node]:
    tail = req.tail if req.tail is not None else DEFAULT_LOG_TAIL_LINES
    try:
        lines, truncated = control.read_log_tail(tail=tail)
    except OSError as exc:
        raise ProtocolError(FORBIDDEN, f"cannot read log file: {exc}", req.id) from exc
    path_str = str(control.agent_log_path)
    lines, truncated = _fit_log_lines_to_frame(path_str, lines, truncated)
    lines_node = Node("lines", args=lines)
    block = Node(
        "logs",
        children=[
            Node("path", [path_str]),
            Node("truncated", [truncated]),
            lines_node,
        ],
    )
    return block_content(block)


def dispatch(control: Any, req: Request) -> list[Node]:
    """Execute a validated request against ``control`` and return content nodes.

    The returned nodes are the response body's ``status`` plus payload; the
    signed v2 envelope is added by :func:`kid_pc_monitor.remote_control_server.handle_request`.
    """
    if req.action == "list_capabilities":
        return capabilities_content(req.version)
    if req.action == "get_logs":
        return _do_get_logs(control, req)
    if req.action == "get":
        return _do_get(control, req)
    if req.action == "set":
        return _do_set(control, req)
    if req.action == "clear":
        return _do_clear(control, req)
    if req.action == "extend":
        return _do_extend(control, req)
    if req.action == "message":
        return _do_message(control, req)
    if req.action == "shutdown":
        return _do_shutdown(control, req)
    if req.action == "lock":
        control.engage_manual_lock()
        control.save_state()
        return ok_content("locked")
    if req.action == "unlock":
        control.clear_manual_lock()
        control.save_state()
        return ok_content("unlocked")
    raise ProtocolError(UNKNOWN_ACTION, f"unknown action: {req.action}", req.id)
