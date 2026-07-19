"""Parent web panel for Kid PC Monitor."""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from kid_pc_monitor import agent_protocol as proto
from kid_pc_monitor.agent_poller import POLL_INTERVAL_SEC, start_agent_poller
from kid_pc_monitor.agent_sync_store import clear_agent_for_hostname, record_agent_poll
from kid_pc_monitor.panel_format import (
    format_minutes_duration,
    format_seconds_duration,
    format_snapshot_recorded_at,
    usage_chart,
)
from kid_pc_monitor.panel_reverse_server import (
    get_reverse_server,
    reverse_listen_port,
    start_panel_reverse_server,
)
from kid_pc_monitor.paths import (
    config_dir,
    package_dir,
    resolve_tls_cert_paths,
    static_dir,
    template_dir,
)
from kid_pc_monitor.remote_client import (
    AgentLogsUnavailable,
    connection_failure_fields,
    format_agent_connection_error,
    get_agent_logs,
    get_default_scan_network,
    inspect_pc,
    parse_scan_subnet,
    perform_action,
    scan_for_servers,
    settings_to_pc_info,
)
from kid_pc_monitor.scan_store import (
    get_pc_poll_updated_at,
    get_scan_pc,
    get_scan_pc_by_hostname,
    load_scan_metadata,
    record_poll_inspect,
    save_scan,
    save_scan_error,
)
from kid_pc_monitor.snapshot_store import (
    get_dashboard_pcs,
    get_latest_panel_snapshot_for_pc,
    get_latest_snapshot_for_pc,
    get_usage_history_for_user,
    save_poll_snapshot,
    save_snapshot,
)

logger = logging.getLogger(__name__)

PANEL_USERNAME = "Kid PC Monitor"
AUTH_FILE = "web_panel_auth.json"
SESSION_AUTH_KEY = "panel_authenticated"
CSRF_SESSION_KEY = "_csrf_token"


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _csrf_valid() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(supplied) and secrets.compare_digest(expected, supplied)


def _safe_next_url(target: str | None) -> str:
    if not target:
        return url_for("index")
    ref = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    if test.scheme not in ("http", "https"):
        return url_for("index")
    if test.netloc and test.netloc != ref.netloc:
        return url_for("index")
    return target


def _auth_path() -> Path:
    """Return the auth file to read; prefer canonical config_dir, then legacy package dir."""
    canonical = config_dir() / AUTH_FILE
    if canonical.is_file():
        return canonical
    legacy = package_dir() / AUTH_FILE
    if legacy.is_file():
        return legacy
    return canonical


def _auth_save_path() -> Path:
    """Where new passwords are written (always under the user config directory)."""
    path = config_dir() / AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _stored_password_hash(record: dict | None) -> str | None:
    if not record:
        return None
    value = record.get("password_hash")
    return value if isinstance(value, str) and value else None


def _panel_secret_key(record: dict | None) -> str | None:
    if not record:
        return None
    key = record.get("secret_key")
    return key if isinstance(key, str) and len(key) >= 16 else None


def _verify_password(record: dict, password: str) -> bool:
    stored = _stored_password_hash(record)
    if not stored:
        return False
    return check_password_hash(stored, password)


def load_auth_record() -> dict | None:
    path = _auth_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def password_is_configured() -> bool:
    return _stored_password_hash(load_auth_record()) is not None


def save_password(password: str) -> None:
    record = load_auth_record() or {}
    secret_key = _panel_secret_key(record) or secrets.token_hex(32)
    path = _auth_save_path()
    path.write_text(
        json.dumps(
            {
                "secret_key": secret_key,
                "password_hash": generate_password_hash(password, method="scrypt"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _record_inspect(pc_info: dict[str, Any]) -> None:
    try:
        save_snapshot(pc_info)
    except Exception:
        logger.warning("Failed to save inspect snapshot", exc_info=True)


def _snapshot_lookup_hostname(pc_info: dict[str, Any], ip: str) -> str | None:
    """Prefer a stable agent hostname so snapshots survive DHCP IP changes."""
    hostname = pc_info.get("hostname")
    if hostname and hostname != f"PC at {ip}":
        return hostname
    scan_pc = get_scan_pc(ip)
    if scan_pc and scan_pc.get("hostname"):
        return str(scan_pc["hostname"])
    return hostname


def _apply_pc_snapshot(pc_info: dict[str, Any], ip: str) -> dict[str, Any]:
    if pc_info.get("reachable", True) and not pc_info.get("connection_error"):
        return pc_info
    snapshot = get_latest_panel_snapshot_for_pc(
        hostname=_snapshot_lookup_hostname(pc_info, ip), ip=ip
    )
    if not snapshot:
        return pc_info
    recorded_at, payload = snapshot
    panel_info = {
        **payload,
        "reachable": False,
        "is_snapshot": True,
        "snapshot_recorded_at": recorded_at,
        "ip": ip,
    }
    panel_info.pop("connection_error", None)
    return panel_info


def _is_ip_address(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def _record_reverse_pc_info(hostname: str, ip: str, settings: dict[str, Any]) -> dict[str, Any]:
    pc_info = settings_to_pc_info(settings, host=ip, hostname_fallback=hostname)
    pc_info["ip"] = ip
    record_agent_poll(hostname, ip, pc_info)
    try:
        save_poll_snapshot(pc_info)
        record_poll_inspect(ip, pc_info)
        if pc_info.get("current_user"):
            save_snapshot(pc_info)
    except Exception:
        logger.warning("Failed to persist reverse status", exc_info=True)
    return pc_info


def _record_reverse_disconnect(hostname: str, ip: str) -> None:
    """Mark a reverse agent offline as soon as its TCP session ends."""
    clear_agent_for_hostname(hostname)
    failure = {
        "hostname": hostname,
        "ip": ip,
        "reachable": False,
    }
    try:
        record_poll_inspect(ip, failure)
    except Exception:
        logger.warning("Failed to persist reverse disconnect for %s", hostname, exc_info=True)


def _perform_control_action(
    ip: str, action_name: str, payload: dict[str, Any] | None = None
) -> tuple[bool, str]:
    """Prefer a live reverse session; fall back to legacy inbound TCP 9999."""
    reverse = get_reverse_server()
    if reverse is not None:
        result = reverse.perform_action(ip, action_name, payload)
        if result is not None:
            return result
    return perform_action(ip, action_name, payload)


def _resolve_control_target(target: str) -> tuple[str, str | None]:
    if _is_ip_address(target):
        return target, None
    scan_pc = get_scan_pc_by_hostname(target)
    if not scan_pc or not scan_pc.get("ip"):
        raise LookupError(target)
    return str(scan_pc["ip"]), str(scan_pc.get("hostname") or target)


def _fetch_control_pc_info(target: str) -> tuple[str, dict[str, Any]] | tuple[None, str]:
    try:
        ip, requested_hostname = _resolve_control_target(target)
    except LookupError:
        return None, f"No recent scan result found for {target}. Scan again and try again."

    reverse = get_reverse_server()
    if reverse is not None:
        session = reverse.session_for_ip(ip)
        if session is not None and session.pc_info:
            pc_info = settings_to_pc_info(
                session.pc_info, host=ip, hostname_fallback=session.hostname
            )
            pc_info["ip"] = ip
            pc_info["reachable"] = True
            return ip, pc_info

    try:
        pc_info = inspect_pc(ip)
        pc_info["ip"] = ip
        _record_inspect(pc_info)
    except ConnectionError as exc:
        pc_info = {
            "hostname": _snapshot_lookup_hostname(
                {"hostname": requested_hostname or f"PC at {ip}"}, ip
            )
            or f"PC at {ip}",
            "ip": ip,
            **connection_failure_fields(exc),
        }
    pc_info = _apply_pc_snapshot(pc_info, ip)
    return ip, pc_info


def _fetch_control_pc_info_cached(target: str) -> tuple[str, dict[str, Any]] | tuple[None, str]:
    try:
        ip, requested_hostname = _resolve_control_target(target)
    except LookupError:
        return None, f"No recent scan result found for {target}. Scan again and try again."

    entry = get_scan_pc(ip)
    if entry is None:
        return None, f"No recent scan result found for {target}. Scan again and try again."

    pc_info = {**entry, "ip": ip}
    if not pc_info.get("reachable", True) or pc_info.get("connection_error"):
        pc_info = {
            "hostname": _snapshot_lookup_hostname(
                {"hostname": requested_hostname or pc_info.get("hostname") or f"PC at {ip}"}, ip
            )
            or pc_info.get("hostname")
            or f"PC at {ip}",
            **pc_info,
        }
        pc_info = _apply_pc_snapshot(pc_info, ip)
    return ip, pc_info


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(template_dir()),
        static_folder=str(static_dir()),
    )
    record = load_auth_record()
    app.secret_key = (
        os.environ.get("KID_PC_MONITOR_SECRET")
        or _panel_secret_key(record)
        or secrets.token_hex(32)
    )

    @app.before_request
    def require_csrf_on_post() -> None:
        if request.path.startswith("/agent/v1/"):
            return
        if request.method == "POST" and not _csrf_valid():
            abort(400)

    @app.context_processor
    def inject_panel_context() -> dict[str, Any]:
        return {
            "password_protected": password_is_configured(),
            "panel_auth": session.get(SESSION_AUTH_KEY, False),
            "panel_username": PANEL_USERNAME,
            "format_minutes_duration": format_minutes_duration,
            "format_seconds_duration": format_seconds_duration,
            "format_snapshot_recorded_at": format_snapshot_recorded_at,
            "csrf_token": _csrf_token,
        }

    def login_required(view: Callable):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if password_is_configured() and not session.get(SESSION_AUTH_KEY):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password_is_configured():
            return redirect(url_for("index"))
        if request.method == "POST":
            record = load_auth_record()
            password = request.form.get("password", "")
            if record and _verify_password(record, password):
                session[SESSION_AUTH_KEY] = True
                return redirect(_safe_next_url(request.form.get("next")))
            flash("Incorrect password.", "error")
        next_arg = request.args.get("next", "")
        safe_next = next_arg if _safe_next_url(next_arg) == next_arg else ""
        return render_template("login.html", next=safe_next)

    @app.route("/logout")
    def logout():
        session.pop(SESSION_AUTH_KEY, None)
        return redirect(url_for("index"))

    @app.route("/set-password", methods=["GET", "POST"])
    def set_password():
        if password_is_configured() and not session.get(SESSION_AUTH_KEY):
            return redirect(url_for("login", next=url_for("set_password")))
        changing = password_is_configured()
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("password_confirm", "")
            record = load_auth_record()
            if changing and (
                not record or not _verify_password(record, request.form.get("current_password", ""))
            ):
                flash("Current password is incorrect.", "error")
            elif len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            else:
                save_password(password)
                record = load_auth_record()
                panel_key = _panel_secret_key(record)
                if panel_key:
                    app.secret_key = panel_key
                session[SESSION_AUTH_KEY] = True
                flash("Password saved.", "success")
                return redirect(url_for("index"))
        return render_template("set_password.html", changing=changing)

    @app.route("/agent/v1/discover")
    def agent_discover():
        return {
            "service": "kid-pc-monitor-panel",
            "version": 1,
            "reverse_port": reverse_listen_port(),
        }

    @app.route("/")
    @login_required
    def index():
        scan_meta = load_scan_metadata()
        pcs, last_poll_at = get_dashboard_pcs()
        return render_template(
            "index.html",
            pcs=pcs,
            last_scan=scan_meta["last_scan"],
            last_scan_network=scan_meta["last_scan_network"],
            scan_subnet=scan_meta["scan_subnet"],
            scan_error=scan_meta["scan_error"],
            last_poll_at=last_poll_at,
            default_subnet=str(get_default_scan_network()),
        )

    @app.route("/scan", methods=["POST"])
    @login_required
    def scan():
        subnet_arg = request.form.get("subnet", "").strip()
        try:
            _network, label = parse_scan_subnet(subnet_arg or None)
            discovered = scan_for_servers(subnet=subnet_arg or None)
            scan_entries: dict[str, dict[str, Any]] = {}
            for ip, entry in discovered.items():
                entry["ip"] = ip
                try:
                    info = inspect_pc(ip)
                    entry.update(info)
                    _record_inspect(info)
                except ConnectionError as exc:
                    entry.update(connection_failure_fields(exc))
                scan_entries[ip] = entry
            save_scan(
                pcs=scan_entries,
                scanned_at=datetime.now().astimezone(),
                network_label=label,
                subnet=subnet_arg,
            )
        except ValueError as exc:
            save_scan_error(str(exc), subnet=subnet_arg)
        return redirect(url_for("index"))

    @app.route("/control/<target>")
    @login_required
    def control(target: str):
        fetched = _fetch_control_pc_info(target)
        if fetched[0] is None:
            return fetched[1], 404
        ip, pc_info = fetched
        poll_updated_at = get_pc_poll_updated_at(ip)
        poll_updated_at_iso = (
            poll_updated_at.isoformat(timespec="seconds") if poll_updated_at else None
        )
        return render_template(
            "control.html",
            ip=ip,
            pc_info=pc_info,
            poll_updated_at=poll_updated_at_iso,
        )

    @app.route("/control/<target>/poll-meta")
    @login_required
    def control_poll_meta(target: str):
        try:
            ip, _requested_hostname = _resolve_control_target(target)
        except LookupError:
            abort(404)
        updated_at = get_pc_poll_updated_at(ip)
        return {
            "updated_at": updated_at.isoformat(timespec="seconds") if updated_at else None,
            "poll_interval_sec": POLL_INTERVAL_SEC,
        }

    @app.route("/control/<target>/stats")
    @login_required
    def control_stats(target: str):
        source = request.args.get("source", "live")
        if source == "poll":
            fetched = _fetch_control_pc_info_cached(target)
        else:
            fetched = _fetch_control_pc_info(target)
        if fetched[0] is None:
            abort(404)
        ip, pc_info = fetched
        return render_template("control_today_stats.html", ip=ip, pc_info=pc_info)

    @app.route("/logs/<ip>")
    @login_required
    def agent_logs(ip: str):
        hostname = f"PC at {ip}"
        scan_pc = get_scan_pc(ip)
        if scan_pc:
            hostname = scan_pc.get("hostname", hostname)
        else:
            snapshot = get_latest_snapshot_for_pc(ip=ip)
            if snapshot:
                hostname = snapshot[1].get("hostname", hostname)
        log_text = ""
        truncated = False
        error_message = None

        reverse = get_reverse_server()
        reverse_session = reverse.session_for_ip(ip) if reverse is not None else None
        if reverse_session is not None:
            hostname = reverse_session.hostname or hostname
            try:
                assert reverse is not None
                result = reverse.get_logs(ip)
                truncated = result.truncated
                log_text = "\n".join(result.lines) or "(log file is empty)"
            except AgentLogsUnavailable as exc:
                error_message = str(exc)
            except (ConnectionError, proto.ProtocolError, TimeoutError, OSError) as exc:
                error_message = str(exc)
        else:
            try:
                result = get_agent_logs(ip)
                truncated = result.truncated
                log_text = "\n".join(result.lines)
                if not log_text:
                    log_text = "(log file is empty)"
            except AgentLogsUnavailable as exc:
                error_message = str(exc)
            except ConnectionError as exc:
                error_message = format_agent_connection_error(exc)
        return render_template(
            "logs.html",
            ip=ip,
            hostname=hostname,
            log_text=log_text,
            truncated=truncated,
            error_message=error_message,
        )

    @app.route("/daily_settings/<ip>")
    @login_required
    def daily_settings(ip: str):
        fetched = _fetch_control_pc_info(ip)
        if fetched[0] is None:
            flash(fetched[1], "error")
            return redirect(url_for("index"))
        _ip, pc_info = fetched
        if pc_info.get("connection_error"):
            flash(pc_info.get("connection_error") or "PC unreachable", "error")
            return redirect(url_for("index"))
        return render_template("daily_settings.html", ip=ip, pc_info=pc_info)

    @app.route("/usage/<path:username>")
    @login_required
    def usage_history(username: str):
        pc_groups = get_usage_history_for_user(username, days=7)
        for group in pc_groups:
            group["chart"] = usage_chart(group["days"])
        return render_template(
            "usage_history.html",
            username=username,
            pc_groups=pc_groups,
        )

    @app.route("/action", methods=["POST"])
    @login_required
    def action():
        payload = request.get_json(silent=True) or {}
        ip = payload.get("ip")
        action_name = payload.get("action")
        if not ip or not action_name:
            return {"success": False, "response": "Missing ip or action"}

        ok, response = _perform_control_action(str(ip), str(action_name), payload)
        return {"success": ok, "response": response}

    return app


def main() -> None:
    host = os.environ.get("KID_PC_MONITOR_HOST", "0.0.0.0")
    port = int(os.environ.get("KID_PC_MONITOR_PORT", "5000"))
    app = create_app()
    start_agent_poller()
    tls = resolve_tls_cert_paths()
    reverse_port = reverse_listen_port()
    start_panel_reverse_server(
        host=host,
        port=reverse_port,
        tls_cert_paths=tls,
        on_status=_record_reverse_pc_info,
        on_disconnect=_record_reverse_disconnect,
    )
    scheme = "https" if tls else "http"
    print(f"Kid PC Monitor web panel on {scheme}://{host}:{port}")
    print(
        f"Agent reverse TCP on {scheme}://{host}:{reverse_port} "
        f"(native v3{' with TLS' if tls else ''})"
    )
    if tls:
        print(
            "TLS enabled. iOS Safari password autofill requires the certificate "
            "authority to be trusted on your phone."
        )
    run_kwargs: dict[str, Any] = {"host": host, "port": port, "debug": False}
    if tls:
        run_kwargs["ssl_context"] = tls
    app.run(**run_kwargs)


if __name__ == "__main__":
    main()
