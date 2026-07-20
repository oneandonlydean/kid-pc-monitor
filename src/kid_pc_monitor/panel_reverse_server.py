"""Panel-side reverse TCP listener for agent-initiated native protocol sessions."""

from __future__ import annotations

import logging
import os
import queue
import socket
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kid_pc_monitor import agent_protocol as proto
from kid_pc_monitor import shared_secret
from kid_pc_monitor.remote_client import action_request_fields, exchange_on_socket

logger = logging.getLogger(__name__)

DEFAULT_REVERSE_PORT = 9998
REVERSE_PORT_ENV = "KID_PC_MONITOR_REVERSE_PORT"
STATUS_REFRESH_SEC = 5.0
EXCHANGE_TIMEOUT_SEC = 30.0
ACCEPT_TIMEOUT_SEC = 1.0

# Protocol verbs that only read state; everything else mutates the agent and
# should trigger an immediate settings refresh of the cached session.pc_info.
_READ_ONLY_ACTIONS = frozenset({"get", "get_logs"})

StatusCallback = Callable[[str, str, dict[str, Any]], object]
DisconnectCallback = Callable[[str, str], object]


def reverse_listen_port() -> int:
    raw = os.environ.get(REVERSE_PORT_ENV, str(DEFAULT_REVERSE_PORT)).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_REVERSE_PORT


@dataclass
class ReverseSession:
    hostname: str
    peer_ip: str
    sock: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_seen: datetime = field(default_factory=lambda: datetime.now().astimezone())
    pc_info: dict[str, Any] = field(default_factory=dict)
    _requests: queue.Queue[tuple[dict[str, Any], Future[proto.Response]] | None] = field(
        default_factory=queue.Queue
    )
    _closed: bool = False

    def close(self) -> None:
        self._closed = True
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def submit(
        self,
        *,
        action: str,
        var: str | None = None,
        val: Any = None,
        tail: int | None = None,
        timeout: float = EXCHANGE_TIMEOUT_SEC,
    ) -> proto.Response:
        if self._closed:
            raise ConnectionError("Reverse agent session is closed")
        future: Future[proto.Response] = Future()
        self._requests.put(
            (
                {"action": action, "var": var, "val": val, "tail": tail},
                future,
            )
        )
        return future.result(timeout=timeout)


class PanelReverseServer:
    """Accept agent outbound connections and speak native v3 on those sockets."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int | None = None,
        tls_cert_paths: tuple[str, str] | None = None,
        on_status: StatusCallback | None = None,
        on_disconnect: DisconnectCallback | None = None,
        status_refresh_sec: float = STATUS_REFRESH_SEC,
    ) -> None:
        self.host = host
        self.port = reverse_listen_port() if port is None else port
        self.tls_cert_paths = tls_cert_paths
        self.on_status = on_status
        self.on_disconnect = on_disconnect
        self.status_refresh_sec = status_refresh_sec
        self.running = False
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._sessions_by_hostname: dict[str, ReverseSession] = {}
        self._sessions_by_ip: dict[str, ReverseSession] = {}
        self._registry_lock = threading.Lock()

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._serve,
            name="panel-reverse-server",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Panel reverse TCP listener starting on %s:%s%s",
            self.host,
            self.port,
            " (TLS)" if self.tls_cert_paths else "",
        )

    def stop(self) -> None:
        self.running = False
        with self._registry_lock:
            sessions = list(self._sessions_by_hostname.values())
            self._sessions_by_hostname.clear()
            self._sessions_by_ip.clear()
        for session in sessions:
            session.close()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def session_for_ip(self, ip: str) -> ReverseSession | None:
        with self._registry_lock:
            return self._sessions_by_ip.get(ip)

    def session_for_hostname(self, hostname: str) -> ReverseSession | None:
        with self._registry_lock:
            return self._sessions_by_hostname.get(hostname)

    def has_session_for_ip(self, ip: str) -> bool:
        return self.session_for_ip(ip) is not None

    def perform_action(
        self,
        ip: str,
        action_name: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[bool, str] | None:
        """Run a panel action on a live reverse session.

        Returns ``None`` when no live session exists for ``ip``.
        """
        session = self.session_for_ip(ip)
        if session is None:
            return None
        try:
            action, var, val, tail = action_request_fields(action_name, payload)
            response = session.submit(action=action, var=var, val=val, tail=tail)
            if response.ok and action not in _READ_ONLY_ACTIONS:
                # A mutating action just changed agent state. Refresh the cached
                # settings now so a page reload right after the save (the panel
                # reloads ~1s later) reflects the change, instead of showing the
                # stale value until the next periodic refresh and looking like the
                # save was ignored (e.g. a checkbox appearing to untick itself).
                self._refresh_session_settings(session)
            return response.ok, response.text
        except (ValueError, KeyError, TypeError) as exc:
            return False, f"Invalid value for {action_name}: {exc}"
        except (
            TimeoutError,
            OSError,
            proto.ProtocolError,
            shared_secret.SharedSecretMissing,
        ) as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - surface to UI
            logger.exception("Reverse action %s failed for %s", action_name, ip)
            return False, str(exc)

    def get_logs(
        self,
        ip: str,
        *,
        tail: int = proto.DEFAULT_LOG_TAIL_LINES,
    ) -> proto.LogsResult:
        session = self.session_for_ip(ip)
        if session is None:
            raise ConnectionError(f"No reverse session for {ip}")
        response = session.submit(action="get_logs", tail=tail)
        if response.logs is None:
            raise ConnectionError(response.text or "Agent did not return logs")
        return response.logs

    def _register(self, session: ReverseSession) -> None:
        with self._registry_lock:
            previous = self._sessions_by_hostname.get(session.hostname)
            if previous is not None and previous is not session:
                previous.close()
            old_ip = None
            for ip, existing in list(self._sessions_by_ip.items()):
                if existing is previous or (
                    existing.hostname == session.hostname and existing is not session
                ):
                    old_ip = ip
                    del self._sessions_by_ip[ip]
            if old_ip is not None and old_ip != session.peer_ip:
                pass
            self._sessions_by_hostname[session.hostname] = session
            self._sessions_by_ip[session.peer_ip] = session

    def _unregister(self, session: ReverseSession) -> None:
        with self._registry_lock:
            if self._sessions_by_hostname.get(session.hostname) is session:
                del self._sessions_by_hostname[session.hostname]
            if self._sessions_by_ip.get(session.peer_ip) is session:
                del self._sessions_by_ip[session.peer_ip]

    def _serve(self) -> None:
        while self.running:
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                server.listen(32)
                server.settimeout(ACCEPT_TIMEOUT_SEC)
                if self.tls_cert_paths is not None:
                    cert_path, key_path = self.tls_cert_paths
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
                    server = context.wrap_socket(server, server_side=True)
                self._server_socket = server
                logger.info("Panel reverse TCP listening on %s:%s", self.host, self.port)
                while self.running:
                    try:
                        client, addr = server.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        if self.running:
                            logger.exception("Reverse accept failed")
                        break
                    peer_ip = addr[0]
                    thread = threading.Thread(
                        target=self._handle_client,
                        args=(client, peer_ip),
                        name=f"reverse-{peer_ip}",
                        daemon=True,
                    )
                    thread.start()
            except Exception:
                if self.running:
                    logger.exception("Reverse listener failed; retrying in 5s")
                    time.sleep(5)
            finally:
                if self._server_socket is not None:
                    try:
                        self._server_socket.close()
                    except OSError:
                        pass
                    self._server_socket = None

    def _handle_client(self, client: socket.socket, peer_ip: str) -> None:
        session: ReverseSession | None = None
        try:
            client.settimeout(EXCHANGE_TIMEOUT_SEC)
            secret = shared_secret.require_shared_secret()
            response = exchange_on_socket(
                client,
                "get",
                secret=secret,
                var="settings",
            )
            if not response.ok or response.settings is None or not response.name:
                logger.warning("Reverse identify failed from %s: %s", peer_ip, response.text)
                return
            hostname = str(response.name)
            session = ReverseSession(hostname=hostname, peer_ip=peer_ip, sock=client)
            session.pc_info = dict(response.settings)
            session.last_seen = datetime.now().astimezone()
            self._register(session)
            self._emit_status(session, response.settings)
            logger.info("Reverse agent connected: %s from %s", hostname, peer_ip)
            self._session_loop(session, secret)
        except (
            OSError,
            proto.ProtocolError,
            shared_secret.SharedSecretMissing,
            TimeoutError,
        ) as exc:
            logger.info("Reverse session from %s ended: %s", peer_ip, exc)
        except Exception:
            logger.exception("Unexpected reverse session error from %s", peer_ip)
        finally:
            if session is not None:
                self._unregister(session)
                session.close()
                self._emit_disconnect_if_gone(session)
            else:
                try:
                    client.close()
                except OSError:
                    pass

    def _session_loop(self, session: ReverseSession, secret: str) -> None:
        while self.running and not session._closed:
            try:
                item = session._requests.get(timeout=self.status_refresh_sec)
            except queue.Empty:
                try:
                    self._refresh_status(session, secret)
                except (OSError, proto.ProtocolError, TimeoutError) as exc:
                    logger.info("Reverse status refresh failed for %s: %s", session.hostname, exc)
                    return
                continue

            if item is None:
                return
            fields, future = item
            try:
                response = exchange_on_socket(
                    session.sock,
                    str(fields["action"]),
                    secret=secret,
                    var=fields.get("var"),
                    val=fields.get("val"),
                    name=session.hostname,
                    tail=fields.get("tail"),
                )
                session.last_seen = datetime.now().astimezone()
                if not future.cancelled():
                    future.set_result(response)
            except Exception as exc:
                if not future.cancelled():
                    future.set_exception(exc)
                return

    def _refresh_session_settings(self, session: ReverseSession) -> None:
        """Re-read settings into ``session.pc_info`` via the session's request
        queue (only the session loop thread may use the socket directly).

        Best-effort: a failed refresh must never change the action's result, so
        the caller's (ok, text) is returned regardless of what happens here.
        """
        try:
            response = session.submit(action="get", var="settings")
        except Exception:  # noqa: BLE001 - cache refresh is best-effort
            logger.debug(
                "Post-action settings refresh failed for %s", session.hostname, exc_info=True
            )
            return
        if response.ok and response.settings is not None:
            session.pc_info = dict(response.settings)
            self._emit_status(session, response.settings)

    def _refresh_status(self, session: ReverseSession, secret: str) -> None:
        response = exchange_on_socket(
            session.sock,
            "get",
            secret=secret,
            var="settings",
            name=session.hostname,
        )
        if not response.ok or response.settings is None:
            raise proto.ProtocolError(
                proto.INTERNAL_ERROR, response.text or "status refresh failed"
            )
        session.last_seen = datetime.now().astimezone()
        session.pc_info = dict(response.settings)
        self._emit_status(session, response.settings)

    def _emit_status(self, session: ReverseSession, settings: dict[str, Any]) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(session.hostname, session.peer_ip, settings)
        except Exception:
            logger.exception("Failed to record reverse status for %s", session.hostname)

    def _emit_disconnect_if_gone(self, session: ReverseSession) -> None:
        """Notify listeners only when no replacement session for this agent remains."""
        if self.on_disconnect is None:
            return
        with self._registry_lock:
            if self._sessions_by_hostname.get(session.hostname) is not None:
                return
        try:
            self.on_disconnect(session.hostname, session.peer_ip)
        except Exception:
            logger.exception("Failed to record reverse disconnect for %s", session.hostname)


_server: PanelReverseServer | None = None
_server_lock = threading.Lock()


def get_reverse_server() -> PanelReverseServer | None:
    return _server


def start_panel_reverse_server(
    *,
    host: str = "0.0.0.0",
    port: int | None = None,
    tls_cert_paths: tuple[str, str] | None = None,
    on_status: StatusCallback | None = None,
    on_disconnect: DisconnectCallback | None = None,
) -> PanelReverseServer:
    global _server
    with _server_lock:
        if _server is not None:
            _server.stop()
        _server = PanelReverseServer(
            host=host,
            port=port,
            tls_cert_paths=tls_cert_paths,
            on_status=on_status,
            on_disconnect=on_disconnect,
        )
        _server.start()
        return _server


def stop_panel_reverse_server() -> None:
    global _server
    with _server_lock:
        if _server is not None:
            _server.stop()
            _server = None
