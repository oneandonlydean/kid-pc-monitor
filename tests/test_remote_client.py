"""Tests for remote_client reachability helpers."""

from __future__ import annotations

import io
import socket
import threading
import unittest
from unittest import mock

from kid_pc_monitor import agent_protocol as proto
from kid_pc_monitor.remote_client import (
    AgentLogsUnavailable,
    _legacy_access_status,
    _print_frame,
    action_request_fields,
    get_agent_logs,
    is_pc_reachable,
    parse_scan_subnet,
    refresh_discovered_entry,
    scan_for_servers,
    send_request,
    settings_to_pc_info,
)

SECRET = "test-shared-secret"
HOSTNAME = "kid-pc"


class RemoteClientTests(unittest.TestCase):
    def test_legacy_access_status_fallback(self) -> None:
        self.assertEqual(_legacy_access_status("UNLOCKED", False), "Unlocked")
        self.assertEqual(_legacy_access_status("LOCKED", False), "Screen locked")
        self.assertEqual(_legacy_access_status("UNLOCKED", True), "Locked — manual lock")

    def test_dismiss_time_request_maps_to_clear(self) -> None:
        self.assertEqual(
            action_request_fields("dismiss_time_request"),
            ("clear", "time_request", None, None),
        )

    def test_set_show_timer_maps_to_boolean_set(self) -> None:
        self.assertEqual(
            action_request_fields("set_show_timer", {"enabled": True}),
            ("set", "show_timer", True, None),
        )

    def test_settings_to_pc_info_surfaces_timer_and_request(self) -> None:
        info = settings_to_pc_info(
            {"show_timer": False, "time_request": "2026-07-20T15:30:00"},
            host="192.168.1.9",
        )
        self.assertIs(info["show_timer"], False)
        self.assertEqual(info["time_request"], "2026-07-20T15:30:00")
        # Defaults when the agent omits the fields.
        legacy = settings_to_pc_info({}, host="192.168.1.9")
        self.assertIs(legacy["show_timer"], True)
        self.assertIsNone(legacy["time_request"])

    def test_is_pc_reachable_open_port(self) -> None:
        ready = threading.Event()
        port_holder: list[int] = []

        def serve() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                port_holder.append(server.getsockname()[1])
                ready.set()
                conn, _addr = server.accept()
                with conn:
                    pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(timeout=2)
        port = port_holder[0]

        self.assertTrue(is_pc_reachable("127.0.0.1", port=port, timeout=1.0))
        thread.join(timeout=2)

    def test_is_pc_reachable_closed_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(is_pc_reachable("127.0.0.1", port=port, timeout=0.5))

    def test_refresh_discovered_entry_marks_offline(self) -> None:
        entry = {
            "hostname": "Test",
            "status": "online",
            "locked": True,
            "current_user": "kid",
            "usage_limit": 60,
        }
        refresh_discovered_entry("127.0.0.1", entry, port=1)
        self.assertFalse(entry["reachable"])
        self.assertEqual(entry["status"], "offline")
        self.assertFalse(entry["locked"])
        self.assertNotIn("current_user", entry)
        self.assertNotIn("usage_limit", entry)


class ScanSubnetTests(unittest.TestCase):
    def test_rejects_networks_larger_than_24(self) -> None:
        for oversized in ("10.0.0.0/16", "10.0.0.0/8", "0.0.0.0/0"):
            with self.subTest(network=oversized):
                with self.assertRaises(ValueError):
                    parse_scan_subnet(oversized)

    def test_accepts_24_and_smaller(self) -> None:
        for ok in ("192.168.1.0/24", "192.168.1.0/30", "192.168.1.50", "192.168.1"):
            with self.subTest(network=ok):
                network, _label = parse_scan_subnet(ok)
                self.assertGreaterEqual(network.prefixlen, 24)


class ScanForServersTests(unittest.TestCase):
    def test_discovers_listening_host_through_bounded_pool(self) -> None:
        ready = threading.Event()
        stop = threading.Event()
        port_holder: list[int] = []

        def serve() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(8)
                server.settimeout(0.2)
                port_holder.append(server.getsockname()[1])
                ready.set()
                while not stop.is_set():
                    try:
                        conn, _addr = server.accept()
                    except TimeoutError:
                        continue
                    conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(timeout=2)
        port = port_holder[0]

        try:
            # /30 keeps the probe set tiny: 127.0.0.1 (our listener) + 127.0.0.2.
            discovered = scan_for_servers(port=port, subnet="127.0.0.0/30")
        finally:
            stop.set()
            thread.join(timeout=2)

        self.assertIn("127.0.0.1", discovered)
        self.assertEqual(discovered["127.0.0.1"]["status"], "online")


class VerboseOutputTests(unittest.TestCase):
    """Tests for the -v / --verbose curl-style frame logging."""

    def _serve_one(
        self, port_holder: list[int], ready: threading.Event, response_body: str
    ) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port_holder.append(server.getsockname()[1])
            ready.set()
            conn, _addr = server.accept()
            with conn:
                # Read and discard the request frame(s)
                proto.read_frame(conn)
                conn.sendall(proto.encode_frame(response_body))

    def test_print_frame_prefixes_lines(self) -> None:
        out = io.StringIO()
        _print_frame(">", "v 2\naction get", out=out)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "> 14")
        self.assertEqual(lines[1], "> v 2")
        self.assertEqual(lines[2], "> action get")

    def test_send_request_verbose_emits_connection_and_frames(self) -> None:
        ready = threading.Event()
        port_holder: list[int] = []
        response = proto.sign_response(
            proto.ok_content("kid-pc"),
            secret=SECRET,
            hostname=HOSTNAME,
        )

        thread = threading.Thread(
            target=self._serve_one, args=(port_holder, ready, response), daemon=True
        )
        thread.start()
        ready.wait(timeout=2)
        port = port_holder[0]

        out = io.StringIO()
        resp = send_request(
            "127.0.0.1",
            "get",
            var="name",
            port=port,
            secret=SECRET,
            verbose=True,
            out=out,
        )
        thread.join(timeout=2)

        self.assertTrue(resp.ok)
        self.assertEqual(resp.result, "kid-pc")

        text = out.getvalue()
        self.assertIn("* Connected to 127.0.0.1:", text)
        self.assertIn(">", text)
        self.assertIn("<", text)
        # Verify the response frame was printed
        self.assertIn("status ok", text)
        self.assertIn("result kid-pc", text)

    def test_send_request_verbose_with_discovery_handshake(self) -> None:
        """Write actions trigger a discovery handshake; both frames are logged."""
        ready = threading.Event()
        port_holder: list[int] = []

        # Server will receive two frames: discovery (get name) + the real request
        def serve_two() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                port_holder.append(server.getsockname()[1])
                ready.set()
                conn, _addr = server.accept()
                with conn:
                    # Frame 1: discovery get name
                    _req1 = proto.read_frame(conn)
                    resp1 = proto.sign_response(
                        proto.ok_content("kid-pc"),
                        secret=SECRET,
                        hostname=HOSTNAME,
                    )
                    conn.sendall(proto.encode_frame(resp1))
                    # Frame 2: the actual lock request
                    _req2 = proto.read_frame(conn)
                    resp2 = proto.sign_response(
                        proto.ok_content("locked"),
                        secret=SECRET,
                        hostname=HOSTNAME,
                    )
                    conn.sendall(proto.encode_frame(resp2))

        thread = threading.Thread(target=serve_two, daemon=True)
        thread.start()
        ready.wait(timeout=2)
        port = port_holder[0]

        out = io.StringIO()
        resp = send_request(
            "127.0.0.1",
            "lock",
            port=port,
            secret=SECRET,
            verbose=True,
            out=out,
        )
        thread.join(timeout=2)

        self.assertTrue(resp.ok)
        self.assertEqual(resp.result, "locked")

        text = out.getvalue()
        # Should see two request frames (discovery + lock) and two responses
        self.assertEqual(text.count("* Connected to"), 1)
        self.assertIn("action get", text)
        self.assertIn("action lock", text)
        self.assertIn("result kid-pc", text)
        self.assertIn("result locked", text)

    def test_send_request_silent_when_verbose_false(self) -> None:
        ready = threading.Event()
        port_holder: list[int] = []
        response = proto.sign_response(
            proto.ok_content("kid-pc"),
            secret=SECRET,
            hostname=HOSTNAME,
        )

        thread = threading.Thread(
            target=self._serve_one, args=(port_holder, ready, response), daemon=True
        )
        thread.start()
        ready.wait(timeout=2)
        port = port_holder[0]

        out = io.StringIO()
        resp = send_request(
            "127.0.0.1",
            "get",
            var="name",
            port=port,
            secret=SECRET,
            verbose=False,
            out=out,
        )
        thread.join(timeout=2)

        self.assertTrue(resp.ok)
        self.assertEqual(out.getvalue(), "")


class GetAgentLogsTests(unittest.TestCase):
    def test_unknown_action_raises_agent_logs_unavailable(self) -> None:
        failure = proto.Response(
            version=3,
            id="r1",
            status="failure",
            error_code=proto.UNKNOWN_ACTION,
            error_message="unknown action",
        )
        with mock.patch("kid_pc_monitor.remote_client.send_request", return_value=failure):
            with self.assertRaises(AgentLogsUnavailable) as ctx:
                get_agent_logs("192.168.1.10")
        self.assertIn("does not support log retrieval", str(ctx.exception))

    def test_protocol_error_unknown_action_raises_agent_logs_unavailable(self) -> None:
        with mock.patch(
            "kid_pc_monitor.remote_client.send_request",
            side_effect=proto.ProtocolError(proto.UNKNOWN_ACTION, "unknown action"),
        ):
            with self.assertRaises(AgentLogsUnavailable):
                get_agent_logs("192.168.1.10")


if __name__ == "__main__":
    unittest.main()
