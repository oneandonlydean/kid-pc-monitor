"""A mutating panel action refreshes the reverse session's cached settings.

Without this, session.pc_info stays stale until the next 5s periodic refresh, so
a page reload right after a save renders the pre-save value (e.g. a checkbox
appears to untick itself).
"""

from __future__ import annotations

import unittest

from kid_pc_monitor import panel_reverse_server as reverse


class _FakeResponse:
    def __init__(self, *, ok=True, text="ok", settings=None) -> None:
        self.ok = ok
        self.text = text
        self.settings = settings


class _FakeSession:
    def __init__(self, settings_after) -> None:
        self.hostname = "KidPC"
        self.peer_ip = "1.2.3.4"
        self.pc_info: dict = {"carryover_enabled": False}
        self._settings_after = settings_after
        self.calls: list[tuple] = []

    def submit(self, *, action, var=None, val=None, tail=None, timeout=None):
        self.calls.append((action, var, val))
        if action == "get" and var == "settings":
            return _FakeResponse(ok=True, settings=dict(self._settings_after))
        return _FakeResponse(ok=True, text=str(val))


def _server_with_session(session: _FakeSession) -> reverse.PanelReverseServer:
    server = reverse.PanelReverseServer(host="127.0.0.1", port=0)
    server._sessions_by_ip[session.peer_ip] = session
    return server


class PerformActionRefreshTests(unittest.TestCase):
    def test_mutating_action_refreshes_cached_settings(self) -> None:
        session = _FakeSession({"carryover_enabled": True, "name": "KidPC"})
        server = _server_with_session(session)

        ok, _text = server.perform_action("1.2.3.4", "set_carryover_enabled", {"enabled": True})

        self.assertTrue(ok)
        # The set was applied, then a settings re-read updated the cache.
        self.assertIn(("set", "carryover_enabled", True), session.calls)
        self.assertIn(("get", "settings", None), session.calls)
        self.assertTrue(session.pc_info["carryover_enabled"])

    def test_read_only_action_does_not_trigger_extra_refresh(self) -> None:
        session = _FakeSession({"carryover_enabled": True})
        server = _server_with_session(session)

        server.perform_action("1.2.3.4", "get_logs", {})

        # No follow-up "get settings" issued for a read-only action.
        self.assertNotIn(("get", "settings", None), session.calls)

    def test_failed_action_does_not_refresh(self) -> None:
        session = _FakeSession({"carryover_enabled": True})
        # Make the mutating submit report failure.
        session.submit = lambda **kw: _FakeResponse(ok=False, text="nope")  # type: ignore[assignment]
        server = _server_with_session(session)

        ok, text = server.perform_action("1.2.3.4", "set_carryover_enabled", {"enabled": True})
        self.assertFalse(ok)
        self.assertEqual(text, "nope")
        self.assertFalse(session.pc_info["carryover_enabled"])  # untouched


if __name__ == "__main__":
    unittest.main()
