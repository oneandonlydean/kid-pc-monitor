"""TCP client for kid PC agents (pc_control.py remote server on port 9999)."""

from __future__ import annotations

import errno
import ipaddress
import secrets
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from kid_pc_monitor import agent_protocol as proto
from kid_pc_monitor import shared_secret
from kid_pc_monitor.network import get_local_ip

# Raised by send_request when the panel has no shared secret configured; the
# broad ``except`` blocks below treat it like any other unreachable condition.
SharedSecretMissing = shared_secret.SharedSecretMissing

DEFAULT_PORT = 9999
CONNECT_TIMEOUT = 5
SCAN_CONNECT_TIMEOUT = 0.5
QUERY_TIMEOUT = 2

# Cap on concurrent scan probes so a /24 uses a bounded pool instead of one
# thread per host.
SCAN_MAX_WORKERS = 64

# Refuse to scan networks larger than a /24 (anything with a shorter prefix).
MIN_SCAN_PREFIXLEN = 24

# Optional friendly names (same format as web_panel.py)
CUSTOM_PC_NAMES: dict[str, str] = {
    # Example: '192.168.1.105': "Tommy's Laptop",
}


# Thread-safe lock so parallel scan threads don't interleave verbose output.
_verbose_lock = threading.Lock()


def _print_frame(prefix: str, frame: str, out: Any = sys.stdout) -> None:
    """Print a length-prefixed frame line-by-line with a curl-style prefix."""
    length = len(frame.encode("utf-8"))
    with _verbose_lock:
        print(f"{prefix} {length}", file=out)
        for line in frame.splitlines():
            print(f"{prefix} {line}", file=out)


def _legacy_access_status(status: str, manual_lock: bool) -> str:
    """Fallback access label for agents that do not expose access_status."""
    if manual_lock:
        return "Locked — manual lock"
    if status == "LOCKED":
        return "Screen locked"
    return "Unlocked"


def get_default_scan_network() -> ipaddress.IPv4Network:
    local_ip = get_local_ip()
    network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("scan requires an IPv4 network")
    return network


def parse_scan_subnet(subnet_arg: str | None) -> tuple[ipaddress.IPv4Network, str]:
    """
    Parse a user-supplied subnet into an ip_network.

    Accepts CIDR (192.168.123.0/24), three octets (192.168.123),
    or a host IP on a /24 (192.168.123.50). Empty means default LAN.
    """
    if subnet_arg is None or not str(subnet_arg).strip():
        network = get_default_scan_network()
        return network, str(network)

    raw = str(subnet_arg).strip()
    normalized = raw

    if "/" not in normalized:
        parts = normalized.split(".")
        if len(parts) == 3:
            normalized = f"{normalized}.0/24"
        elif len(parts) == 4:
            normalized = f"{'.'.join(parts[:3])}.0/24"
        else:
            raise ValueError(
                f"Invalid network '{raw}'. Use CIDR (192.168.123.0/24), "
                "three octets (192.168.123), or a host IP (192.168.123.50)."
            )

    try:
        network = ipaddress.ip_network(normalized, strict=False)
    except ValueError as exc:
        raise ValueError(f"Invalid network '{raw}': {exc}") from exc

    if network.version != 4:
        raise ValueError("Only IPv4 networks are supported.")

    if network.prefixlen < MIN_SCAN_PREFIXLEN:
        raise ValueError(
            f"Network '{network}' is too large to scan "
            f"({network.num_addresses} addresses). "
            "Limit scans to a /24 (256 addresses) or smaller."
        )

    return network, str(network)


# ---------------------------------------------------------------------------
# Structured protocol (version 2) client helpers
# ---------------------------------------------------------------------------
def _resolve_secret(secret: str | None) -> str:
    """Return the supplied secret or load the configured shared secret."""
    if secret is not None:
        return secret
    return shared_secret.require_shared_secret()


def _discover_name(
    client: socket.socket, secret: str, *, verbose: bool = False, out: Any = sys.stdout
) -> str:
    """Run the unnamed ``get name`` handshake and return the agent's hostname.

    The request is signed with the raw shared secret (no ``name`` yet); the
    agent's signed reply carries its hostname, which we verify before trusting.
    """
    body = proto.build_request("get", secret=secret, var="name", req_id=secrets.token_hex(3))
    if verbose:
        _print_frame(">", body, out=out)
    client.sendall(proto.encode_frame(body))
    response_body = proto.read_frame(client)
    if verbose:
        _print_frame("<", response_body, out=out)
    resp = proto.parse_response(response_body, secret=secret)
    if not resp.ok or not resp.name:
        raise proto.ProtocolError(
            proto.AUTHENTICATION_FAILED, "could not discover the agent's name"
        )
    return str(resp.name)


class AgentLogsUnavailable(ConnectionError):
    """The agent does not support log retrieval (unknown action)."""


def exchange_on_socket(
    sock: socket.socket,
    action: str,
    *,
    secret: str,
    var: str | None = None,
    val: Any = None,
    name: str | None = None,
    version: int = proto.CLIENT_DEFAULT_VERSION,
    tail: int | None = None,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> proto.Response:
    """Send one signed request on an existing socket and return the response."""
    if name is None and action in proto.WRITE_ACTIONS:
        name = _discover_name(sock, secret, verbose=verbose, out=out)
    body = proto.build_request(
        action,
        secret=secret,
        var=var,
        val=val,
        req_id=secrets.token_hex(3),
        name=name,
        version=version,
        tail=tail,
    )
    if verbose:
        _print_frame(">", body, out=out)
    sock.sendall(proto.encode_frame(body))
    response_body = proto.read_frame(sock)
    if verbose:
        _print_frame("<", response_body, out=out)
    return proto.parse_response(
        response_body, secret=secret, expected_name=name, expected_version=version
    )


def send_request(
    host: str,
    action: str,
    *,
    var: str | None = None,
    val: Any = None,
    port: int = DEFAULT_PORT,
    timeout: float = CONNECT_TIMEOUT,
    secret: str | None = None,
    name: str | None = None,
    version: int = proto.CLIENT_DEFAULT_VERSION,
    tail: int | None = None,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> proto.Response:
    """Send one signed v2 request and return the verified response.

    Write actions are bound to a target agent's hostname.  When ``name`` is not
    supplied for such an action, the client first performs the discovery
    handshake on the same connection to learn (and authenticate) the hostname,
    then signs the write with the per-agent key.

    Raises OSError on connection problems, ProtocolError on a malformed or
    unauthenticated response, and SharedSecretMissing if no secret is set.
    """
    secret = _resolve_secret(secret)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect((host, port))
        if verbose:
            with _verbose_lock:
                print(f"* Connected to {host}:{port}", file=out)
        return exchange_on_socket(
            client,
            action,
            secret=secret,
            var=var,
            val=val,
            name=name,
            version=version,
            tail=tail,
            verbose=verbose,
            out=out,
        )


def get_agent_logs(
    host: str,
    *,
    tail: int = proto.DEFAULT_LOG_TAIL_LINES,
    port: int = DEFAULT_PORT,
    timeout: float = CONNECT_TIMEOUT,
    name: str | None = None,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> proto.LogsResult:
    """Fetch the tail of the agent log file (requires a current agent with ``get_logs``)."""
    try:
        resp = send_request(
            host,
            "get_logs",
            port=port,
            timeout=timeout,
            name=name,
            tail=tail,
            verbose=verbose,
            out=out,
        )
    except proto.ProtocolError as exc:
        if exc.code == proto.UNKNOWN_ACTION:
            raise AgentLogsUnavailable(
                "This PC's agent does not support log retrieval yet. "
                "Update the Kid PC Monitor agent on that computer."
            ) from exc
        raise ConnectionError(str(exc)) from exc

    if not resp.ok:
        if resp.error_code == proto.UNKNOWN_ACTION:
            raise AgentLogsUnavailable(
                "This PC's agent does not support log retrieval yet. "
                "Update the Kid PC Monitor agent on that computer."
            )
        raise ConnectionError(resp.text or "get_logs failed")

    if resp.logs is None:
        raise ConnectionError("get_logs returned no log data")

    return resp.logs


def format_agent_connection_error(exc: BaseException) -> str:
    """Turn low-level agent/client errors into parent-friendly text."""
    text = str(exc).strip()
    lower = text.lower()

    if "timestamp outside allowed window" in lower or "stale_timestamp" in lower:
        return (
            "This PC's clock is out of sync (common after sleep or pausing a VM). "
            "Correct the date and time on that computer, then scan again."
        )

    if "no shared secret is configured" in lower:
        return (
            "No shared secret is configured on this computer. "
            "Re-run the installer on the kid PC and web panel host."
        )

    if "authentication_failed" in lower or "signature did not verify" in lower:
        return (
            "Authentication failed — the shared secret on this panel may not match "
            "the kid PC. Re-run the installers with the same secret."
        )

    marker = ": "
    idx = text.rfind(marker)
    if idx != -1 and "cannot reach agent at" in lower:
        detail = text[idx + len(marker) :].strip()
        if detail and detail.lower() != lower:
            return format_agent_connection_error(ConnectionError(detail))

    return text or "Could not communicate with the agent."


def is_offline_connection_error(exc: BaseException) -> bool:
    """True when the host is unreachable at the network layer (e.g. powered off)."""
    offline_errnos: set[int] = {113}  # Linux EHOSTUNREACH
    for name in ("EHOSTUNREACH", "EHOSTDOWN", "ENETUNREACH"):
        val = getattr(errno, name, None)
        if isinstance(val, int):
            offline_errnos.add(val)

    for err in (exc, exc.__cause__):
        if isinstance(err, OSError) and err.errno in offline_errnos:
            return True

    lower = str(exc).lower()
    return "no route to host" in lower or "[errno 113]" in lower


def connection_failure_fields(exc: BaseException) -> dict[str, Any]:
    """Session/template fields when inspect or control actions cannot reach an agent."""
    if is_offline_connection_error(exc):
        return {"reachable": False}
    return {
        "reachable": False,
        "connection_error": format_agent_connection_error(exc),
    }


def request_text(
    host: str,
    action: str,
    *,
    var: str | None = None,
    val: Any = None,
    port: int = DEFAULT_PORT,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> tuple[bool, str]:
    """Send a structured request; return (success, human-readable text)."""
    try:
        resp = send_request(host, action, var=var, val=val, port=port, verbose=verbose, out=out)
    except (OSError, proto.ProtocolError, SharedSecretMissing) as exc:
        return False, format_agent_connection_error(exc)
    return resp.ok, resp.text


def get_settings(
    host: str, port: int = DEFAULT_PORT, *, verbose: bool = False, out: Any = sys.stdout
) -> dict[str, Any] | None:
    """Fetch every variable in a single round-trip."""
    resp = send_request(host, "get", var="settings", port=port, verbose=verbose, out=out)
    return resp.settings if resp.ok else None


def action_request_fields(
    action_name: str,
    payload: dict[str, Any] | None = None,
) -> tuple[str, str | None, Any, int | None]:
    """Map a panel action name to protocol request fields."""
    p = payload or {}
    if action_name == "lock":
        return "lock", None, None, None
    if action_name == "shutdown":
        return "shutdown", None, None, None
    if action_name == "message":
        return "message", None, p.get("message", ""), None
    if action_name == "extend_time":
        return "extend", None, int(p["minutes"]), None
    if action_name == "clear_manual_lock":
        return "clear", "manual_lock", None, None
    if action_name == "clear_extensions":
        return "clear", "cumulative_extension", None, None
    if action_name == "set_daily_limit":
        minutes = p.get("minutes")
        if minutes is None or minutes == "":
            return "clear", "daily_limit", None, None
        return "set", "daily_limit", int(minutes), None
    if action_name == "set_bed_time":
        return "set", "bed_time", p.get("time"), None
    if action_name in ("clear_bed_time", "clear_lock_times"):
        return "clear", "bed_time", None, None
    if action_name == "set_wake_time":
        return "set", "wake_time", p.get("time"), None
    if action_name == "set_show_timer":
        return "set", "show_timer", bool(p.get("enabled")), None
    if action_name == "dismiss_time_request":
        return "clear", "time_request", None, None
    if action_name == "set_break_interval":
        minutes = p.get("minutes")
        if minutes is None or minutes == "":
            return "clear", "break_interval", None, None
        return "set", "break_interval", int(minutes), None
    if action_name == "set_break_duration":
        return "set", "break_duration", int(p["minutes"]), None
    if action_name == "set_weekend_enabled":
        return "set", "weekend_enabled", bool(p.get("enabled")), None
    if action_name == "set_weekend_bed_time":
        return "set", "weekend_bed_time", p.get("time"), None
    if action_name == "clear_weekend_bed_time":
        return "clear", "weekend_bed_time", None, None
    if action_name == "set_weekend_allowance":
        minutes = p.get("minutes")
        if minutes is None or minutes == "":
            return "clear", "weekend_allowance", None, None
        return "set", "weekend_allowance", int(minutes), None
    if action_name == "clear_usage_limit":
        return "clear", "daily_limit", None, None
    if action_name == "get_logs":
        return "get_logs", None, None, int(p.get("tail") or proto.DEFAULT_LOG_TAIL_LINES)
    raise KeyError(action_name)


def perform_action(
    host: str,
    action_name: str,
    payload: dict[str, Any] | None = None,
    port: int = DEFAULT_PORT,
    *,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> tuple[bool, str]:
    """Run a web-panel action over the structured protocol."""
    try:
        action, var, val, tail = action_request_fields(action_name, payload)
        if tail is not None:
            resp = send_request(
                host, action, var=var, val=val, tail=tail, port=port, verbose=verbose, out=out
            )
            return resp.ok, resp.text
        return request_text(host, action, var=var, val=val, port=port, verbose=verbose, out=out)
    except (ValueError, KeyError, TypeError) as exc:
        return False, f"Invalid value for {action_name}: {exc}"


def is_pc_reachable(host: str, port: int = DEFAULT_PORT, timeout: float = QUERY_TIMEOUT) -> bool:
    """True when the kid PC agent accepts TCP connections on port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def refresh_discovered_entry(
    ip: str,
    entry: dict[str, Any],
    port: int = DEFAULT_PORT,
    *,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> None:
    """Update a cached scan entry with current reachability, lock, and user."""
    reachable = is_pc_reachable(ip, port=port)
    entry["reachable"] = reachable
    entry["status"] = "online" if reachable else "offline"
    if not reachable:
        entry["locked"] = False
        entry.pop("current_user", None)
        for key in (
            "daily_limit",
            "usage_limit",
            "manual_lock_active",
            "bed_time",
            "lock_times",
            "time_remaining",
        ):
            entry.pop(key, None)
        return

    entry["last_seen"] = datetime.now()
    status = check_pc_status(ip, port=port, verbose=verbose, out=out)
    entry["locked"] = status == "LOCKED"
    username = get_current_user(ip, port=port, verbose=verbose, out=out)
    if username:
        entry["current_user"] = username
    else:
        entry.pop("current_user", None)


def _resolve_hostname(ip: str, port: int, *, verbose: bool = False, out: Any = sys.stdout) -> str:
    if ip in CUSTOM_PC_NAMES:
        return CUSTOM_PC_NAMES[ip]
    try:
        resp = send_request(ip, "get", var="name", port=port, verbose=verbose, out=out)
        if resp.ok and resp.result:
            return str(resp.result)
    except (OSError, proto.ProtocolError, SharedSecretMissing):
        pass
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except OSError:
        return f"PC at {ip}"


def scan_for_servers(
    port: int = DEFAULT_PORT,
    subnet: str | None = None,
    *,
    verbose: bool = False,
    out: Any = sys.stdout,
) -> dict[str, dict[str, Any]]:
    """Scan a network for hosts with the kid PC agent listening on port."""
    network, _network_label = parse_scan_subnet(subnet)
    discovered: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    scanned_count = [0]

    def check_host(ip: ipaddress.IPv4Address) -> None:
        ip_str = str(ip)
        try:
            if not is_pc_reachable(ip_str, port=port, timeout=SCAN_CONNECT_TIMEOUT):
                return
            hostname = _resolve_hostname(ip_str, port, verbose=verbose, out=out)
            entry = {
                "hostname": hostname,
                "status": "online",
                "last_seen": datetime.now(),
            }
            with lock:
                discovered[ip_str] = entry
        finally:
            with lock:
                scanned_count[0] += 1

    hosts = list(network.hosts())
    if hosts:
        max_workers = min(SCAN_MAX_WORKERS, len(hosts))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for ip in hosts:
                executor.submit(check_host, ip)

    if verbose:
        total_hosts = scanned_count[0]
        with _verbose_lock:
            print(f"* Scanned {total_hosts} hosts, found {len(discovered)} agent(s)", file=out)

    return discovered


def check_pc_status(
    ip: str, port: int = DEFAULT_PORT, *, verbose: bool = False, out: Any = sys.stdout
) -> str:
    """Return LOCKED, UNLOCKED, or UNKNOWN."""
    try:
        resp = send_request(ip, "get", var="status", port=port, verbose=verbose, out=out)
    except (OSError, proto.ProtocolError, SharedSecretMissing):
        return "UNKNOWN"
    return str(resp.result) if resp.ok and resp.result else "UNKNOWN"


def get_current_user(
    ip: str, port: int = DEFAULT_PORT, *, verbose: bool = False, out: Any = sys.stdout
) -> str | None:
    try:
        resp = send_request(ip, "get", var="current_user", port=port, verbose=verbose, out=out)
    except (OSError, proto.ProtocolError, SharedSecretMissing):
        return None
    if resp.ok and resp.result is not None:
        return str(resp.result)
    return None


def inspect_pc(
    host: str, port: int = DEFAULT_PORT, *, verbose: bool = False, out: Any = sys.stdout
) -> dict[str, Any]:
    """Collect status and limits from a single kid PC in one round-trip."""
    try:
        settings = get_settings(host, port=port, verbose=verbose, out=out)
    except (OSError, proto.ProtocolError, SharedSecretMissing) as exc:
        raise ConnectionError(f"Cannot reach agent at {host}:{port}: {exc}") from exc

    if settings is None:
        raise ConnectionError(f"Cannot reach agent at {host}:{port}")

    return settings_to_pc_info(settings, host=host, port=port)


def settings_to_pc_info(
    settings: dict[str, Any],
    *,
    host: str,
    port: int = DEFAULT_PORT,
    hostname_fallback: str | None = None,
) -> dict[str, Any]:
    """Convert an agent settings payload into the panel/CLI PC-info shape."""
    status = settings.get("status") or "UNKNOWN"
    daily_limit = settings.get("daily_limit")
    bed_time = settings.get("bed_time")
    wake_time = settings.get("wake_time")
    extension_seconds = int(settings.get("cumulative_extension") or 0)
    accumulated_seconds = int(settings.get("accumulated_seconds") or 0)

    # time_remaining arrives as an integer count of minutes (or null); render it
    # the way the panel and CLI expect to display it.
    remaining_min = settings.get("time_remaining")
    time_remaining = f"{remaining_min} minutes" if remaining_min is not None else None

    lock_times: list[str] | None = [bed_time] if bed_time else None

    effective_limit: float | None
    if daily_limit is None and extension_seconds <= 0:
        effective_limit = None
    else:
        effective_limit = (daily_limit or 0) + extension_seconds / 60

    hostname = settings.get("name") or hostname_fallback or f"PC at {host}"

    manual_lock_active = bool(settings.get("manual_lock"))
    enforcement_active = settings.get("enforcement_active")
    if enforcement_active is None:
        enforcement_active = False
    else:
        enforcement_active = bool(enforcement_active)
    enforcement_reason = settings.get("enforcement_reason")
    access_status = settings.get("access_status")
    if access_status is None:
        access_status = _legacy_access_status(status, manual_lock_active)

    return {
        "ip": host,
        "port": port,
        "hostname": hostname,
        "status": status,
        "locked": status == "LOCKED",
        "current_user": settings.get("current_user"),
        "daily_limit": daily_limit,
        "usage_limit": daily_limit,
        "bed_time": bed_time,
        "lock_times": lock_times,
        "wake_time": wake_time,
        "show_timer": bool(settings.get("show_timer", True)),
        "time_request": settings.get("time_request"),
        "break_interval": int(settings.get("break_interval") or 0),
        "break_duration": int(settings.get("break_duration") or 5),
        "on_break": bool(settings.get("on_break")),
        "weekend_enabled": bool(settings.get("weekend_enabled")),
        "weekend_bed_time": settings.get("weekend_bed_time"),
        "weekend_allowance": settings.get("weekend_allowance"),
        "manual_lock_active": manual_lock_active,
        "enforcement_active": enforcement_active,
        "enforcement_reason": enforcement_reason,
        "access_status": access_status,
        "cumulative_extension_seconds": extension_seconds,
        "accumulated_seconds": accumulated_seconds,
        "effective_limit_minutes": effective_limit,
        "time_remaining": time_remaining,
        "reachable": True,
    }
