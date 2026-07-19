"""Windows implementation of HostPlatform."""

from __future__ import annotations

import ctypes
import getpass
import json
import logging
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from typing import TYPE_CHECKING

from kid_pc_monitor.host_platform import HostPlatform
from kid_pc_monitor.network import get_primary_ipv4

if TYPE_CHECKING:
    import tkinter as tk

# Must match scripts/install.py FIREWALL_RULE_DISPLAY_NAME
_FIREWALL_RULE_DISPLAY_NAME = "Kid PC Monitor Agent (TCP 9999)"
_INVALID_HANDLE_VALUE = ctypes.c_size_t(-1).value
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

# WTSQuerySessionInformation lock-state polling.
_WTS_CURRENT_SERVER_HANDLE = 0
_WTS_CURRENT_SESSION = 0xFFFFFFFF
_WTS_INFO_CLASS_SESSION_INFO_EX = 25  # WTSSessionInfoEx
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1
_WTS_SESSIONSTATE_UNKNOWN = 0xFFFFFFFF


class _WTSINFOEX_LEVEL1(ctypes.Structure):
    """Prefix of WTSINFOEX_LEVEL1_W — only the fields we read.

    The full structure continues with name strings and timestamps, but
    SessionFlags is the third field and everything after it is irrelevant
    to lock detection, so we deliberately stop here.
    """

    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("SessionState", ctypes.c_long),
        ("SessionFlags", ctypes.c_long),
    ]


class _WTSINFOEX(ctypes.Structure):
    """WTSINFOEXW prefix. The `_pad` field forces the union (Data) onto the
    8-byte boundary it really has (it contains LARGE_INTEGERs), so SessionFlags
    is read at the correct offset even though we only declare a 4-byte prefix."""

    _fields_ = [
        ("Level", wintypes.DWORD),
        ("_pad", wintypes.DWORD),
        ("Data", _WTSINFOEX_LEVEL1),
    ]


def _wts_session_flags_inverted() -> bool:
    """True on Windows 7 / Server 2008 R2 (NT 6.1), where the documented
    WTS_SESSIONSTATE_LOCK/UNLOCK values are reversed due to a code defect."""
    try:
        version = sys.getwindowsversion()
    except AttributeError:
        return False
    return version.major == 6 and version.minor == 1


def _session_flags_indicate_locked(session_flags: int, *, inverted: bool) -> bool | None:
    """Interpret a WTSINFOEX SessionFlags value.

    Returns True if locked, False if unlocked, or None when the state is
    unknown (so the caller can fall back to another signal).
    """
    if session_flags == _WTS_SESSIONSTATE_UNKNOWN:
        return None
    if session_flags == _WTS_SESSIONSTATE_LOCK:
        return False if inverted else True
    if session_flags == _WTS_SESSIONSTATE_UNLOCK:
        return True if inverted else False
    return None


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


def _subprocess_creationflags() -> int:
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def _run_powershell_json(script: str) -> dict | list | None:
    """Run a PowerShell snippet that prints a single JSON object; return dict or None."""
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=_subprocess_creationflags(),
        ).strip()
        if not out:
            return None
        return json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        logging.getLogger("kid_pc_monitor").debug("PowerShell diagnostic failed: %s", exc)
        return None


def _current_session_id() -> int | None:
    kernel32 = ctypes.windll.kernel32
    sid = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid)):
        return None
    return sid.value


def _process_exists_in_session(image_name: str, session_id: int | None) -> bool:
    """Return True when image_name is running in session_id (or any session if None)."""
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, _INVALID_HANDLE_VALUE):
        return False

    target = image_name.lower()
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile.lower() == target:
                if session_id is None:
                    return True
                pid_sid = wintypes.DWORD()
                if (
                    kernel32.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(pid_sid))
                    and pid_sid.value == session_id
                ):
                    return True
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return False


def _query_session_locked_via_wts() -> bool | None:
    """Authoritative lock state for the current session via WTSSessionInfoEx.

    Returns True (locked), False (unlocked), or None when the API call fails
    or reports an unknown state. Unlike LogonUI.exe detection, the WTS session
    flags stay correct even when the lock screen's display has gone to sleep.
    """
    try:
        wtsapi32 = ctypes.windll.wtsapi32
        buffer = ctypes.c_void_p()
        bytes_returned = wintypes.DWORD()
        ok = wtsapi32.WTSQuerySessionInformationW(
            _WTS_CURRENT_SERVER_HANDLE,
            _WTS_CURRENT_SESSION,
            _WTS_INFO_CLASS_SESSION_INFO_EX,
            ctypes.byref(buffer),
            ctypes.byref(bytes_returned),
        )
        if not ok or not buffer:
            return None
        try:
            if bytes_returned.value < ctypes.sizeof(_WTSINFOEX):
                return None
            info = ctypes.cast(buffer, ctypes.POINTER(_WTSINFOEX)).contents
            return _session_flags_indicate_locked(
                info.Data.SessionFlags, inverted=_wts_session_flags_inverted()
            )
        finally:
            wtsapi32.WTSFreeMemory(buffer)
    except Exception as exc:
        logging.getLogger("PCTimeControl").debug("WTS lock query failed: %s", exc, exc_info=True)
        return None


class _TimeOverlay:
    """A small always-on-top window showing the kid's remaining time.

    tkinter requires every call for a given root to run on one thread, so the
    window lives on its own daemon thread running the Tk event loop. The
    monitoring thread only publishes the desired text via ``update``; the UI
    thread polls it and repaints. The thread starts lazily the first time there
    is something to show, so an unmonitored or unlimited session never creates a
    window. Once running it stays alive and just hides (``withdraw``) whenever
    the text is ``None``, so it can reappear without restarting.
    """

    _POLL_MS = 500
    _NORMAL_BG = "#202020"
    _NORMAL_FG = "#ffffff"
    _URGENT_BG = "#b00020"  # red for the final minutes before a lock
    _URGENT_FG = "#ffffff"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._desired_text: str | None = None
        self._desired_urgent = False
        self._started = False
        self._root: tk.Tk | None = None
        self._label: tk.Label | None = None

    def update(self, text: str | None, urgent: bool = False) -> None:
        """Publish the text/urgency to display (or ``None`` to hide); start UI lazily."""
        with self._lock:
            self._desired_text = text
            self._desired_urgent = urgent
            if self._started or text is None:
                return
            self._started = True
        threading.Thread(target=self._run, name="kpm-time-overlay", daemon=True).start()

    def _run(self) -> None:
        logger = logging.getLogger("PCTimeControl")
        try:
            import tkinter as tk
        except Exception as exc:
            logger.warning("On-screen timer unavailable (tkinter import failed): %s", exc)
            return
        try:
            root = tk.Tk()
            root.title("PC Time Control")
            root.overrideredirect(True)  # borderless — no title bar to click away
            root.attributes("-topmost", True)
            try:
                root.attributes("-alpha", 0.85)
            except tk.TclError:
                pass  # transparency is best-effort
            label = tk.Label(
                root,
                text="",
                font=("Segoe UI", 11, "bold"),
                fg="#ffffff",
                bg="#202020",
                padx=14,
                pady=7,
            )
            label.pack()
            self._root = root
            self._label = label
            root.after(self._POLL_MS, self._refresh)
            root.mainloop()
        except Exception as exc:
            logger.error("On-screen timer crashed: %s", exc, exc_info=True)

    def _place_top_right(self, root: tk.Tk) -> None:
        root.update_idletasks()
        width = root.winfo_width()
        screen_width = root.winfo_screenwidth()
        x = max(0, screen_width - width - 20)
        root.geometry(f"+{x}+20")

    def _refresh(self) -> None:
        root = self._root
        label = self._label
        if root is None or label is None:
            return
        with self._lock:
            text = self._desired_text
            urgent = self._desired_urgent
        try:
            if text is None:
                root.withdraw()
            else:
                bg = self._URGENT_BG if urgent else self._NORMAL_BG
                fg = self._URGENT_FG if urgent else self._NORMAL_FG
                if label.cget("text") != text or label.cget("bg") != bg:
                    label.config(text=text, bg=bg, fg=fg)
                    root.configure(bg=bg)
                    self._place_top_right(root)
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)  # reassert above other windows
        except Exception as exc:
            logging.getLogger("PCTimeControl").debug("Overlay refresh error: %s", exc)
        finally:
            try:
                root.after(self._POLL_MS, self._refresh)
            except Exception:
                pass  # root is being torn down


class WindowsHostPlatform(HostPlatform):
    """Windows session lock, shutdown, messaging, and firewall diagnostics."""

    def __init__(self) -> None:
        self._time_overlay = _TimeOverlay()

    def update_time_overlay(self, text: str | None, *, urgent: bool = False) -> None:
        self._time_overlay.update(text, urgent)

    def check_session_locked(self) -> bool:
        """
        True when this session's workstation is locked.

        Primary signal is the WTS session flags (WTSSessionInfoEx), which stay
        correct even after the locked lock screen's display sleeps. Windows
        terminates LogonUI.exe when the display turns off while locked, so the
        legacy LogonUI presence check is only used as a fallback when the WTS
        query is unavailable or returns an unknown state.

        LogonUI detection is filtered by session to avoid a false LOCKED when
        another user's session is locked under fast user switching, and uses
        Toolhelp APIs so the agent does not spawn a visible console each tick.
        """
        wts_locked = _query_session_locked_via_wts()
        if wts_locked is not None:
            return wts_locked
        try:
            return _process_exists_in_session("LogonUI.exe", _current_session_id())
        except Exception as exc:
            logging.getLogger("PCTimeControl").error(
                "Error checking lock state (LogonUI): %s", exc, exc_info=True
            )
            return False

    def session_is_active(self) -> bool:
        """True if our session is the active console session and not locked."""
        try:
            kernel32 = ctypes.windll.kernel32
            our_sid = ctypes.c_ulong()
            ok = kernel32.ProcessIdToSessionId(
                kernel32.GetCurrentProcessId(), ctypes.byref(our_sid)
            )
            if not ok:
                return not self.check_session_locked()
            active_sid = kernel32.WTSGetActiveConsoleSessionId()
            if active_sid == 0xFFFFFFFF or active_sid != our_sid.value:
                return False
            return not self.check_session_locked()
        except Exception as exc:
            logging.getLogger("PCTimeControl").error(
                "Error checking session active state: %s", exc, exc_info=True
            )
            return not self.check_session_locked()

    def lock_workstation(self) -> None:
        ctypes.windll.user32.LockWorkStation()

    def shutdown(self, seconds: int = 60) -> None:
        os.system(f'shutdown /s /t {seconds} /c "Computer will shutdown in {seconds} seconds"')

    def cancel_shutdown(self) -> None:
        os.system("shutdown /a")

    def show_message(self, message: str, title: str = "PC Time Control") -> None:
        import tkinter as tk
        from tkinter import messagebox

        def display() -> None:
            root = None
            try:
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                root.after(60000, root.destroy)
                messagebox.showwarning(title, message)
            except Exception as exc:
                logging.getLogger("PCTimeControl").error("Error showing message: %s", exc)
            finally:
                if root:
                    try:
                        root.quit()
                        root.destroy()
                    except Exception:
                        pass

        threading.Thread(target=display, daemon=True).start()

    def get_hostname(self) -> str:
        import platform

        return platform.node()

    def log_connectivity_diagnostics(
        self,
        logger: logging.Logger,
        *,
        agent_port: int,
        log_file: str,
        log_level_name: str,
        python_executable: str,
    ) -> None:
        primary_ip = get_primary_ipv4()

        logger.info(
            "Connectivity check: pid=%s user=%s primary_ip=%s python=%s log=%s level=%s",
            os.getpid(),
            getpass.getuser(),
            primary_ip or "none",
            python_executable,
            log_file,
            log_level_name,
        )

        on_public = False
        profiles = _run_powershell_json(
            "@(Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
            "Select-Object InterfaceAlias, IPv4Connectivity, NetworkCategory) | "
            "ConvertTo-Json -Compress"
        )
        if profiles is None:
            logger.warning("Could not read Windows network profiles (Get-NetConnectionProfile)")
        elif isinstance(profiles, dict):
            profiles = [profiles]
        if profiles:
            for entry in profiles:
                logger.info(
                    "Network profile: interface=%s connectivity=%s category=%s",
                    entry.get("InterfaceAlias", "?"),
                    entry.get("IPv4Connectivity", "?"),
                    entry.get("NetworkCategory", "?"),
                )

            def _is_public(category) -> bool:
                if category in (0, "0"):
                    return True
                return str(category).lower() == "public"

            on_public = any(_is_public(p.get("NetworkCategory")) for p in profiles)
            if on_public:
                logger.warning(
                    "At least one interface is Public. The installer "
                    "firewall rule allows inbound TCP %s only on Private/Domain "
                    "unless you chose to include Public. Remote scans and pc_cli "
                    "will fail until the network is Private or the rule includes Public.",
                    agent_port,
                )

        rule_result = _run_powershell_json(
            f"$r = Get-NetFirewallRule -DisplayName '{_FIREWALL_RULE_DISPLAY_NAME}' "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if (-not $r) { @{{found=$false}} | ConvertTo-Json -Compress } "
            "else { "
            "@{{found=$true; enabled=$r.Enabled; profile=$r.Profile; "
            "program=($r | Get-NetFirewallApplicationFilter).Program; "
            "localPort=($r | Get-NetFirewallPortFilter).LocalPort}} | "
            "ConvertTo-Json -Compress "
            "}"
        )
        if rule_result is None:
            logger.warning("Could not query Windows Firewall rule for the agent")
        elif not isinstance(rule_result, dict):
            logger.warning("Unexpected firewall rule query response")
        elif not rule_result.get("found"):
            logger.warning(
                "No firewall rule named %r — inbound TCP 9999 may be blocked. "
                "Re-run scripts/install.py as administrator.",
                _FIREWALL_RULE_DISPLAY_NAME,
            )
        else:
            rule = rule_result
            profile_mask = int(rule.get("profile") or 0)
            profile_names = []
            if profile_mask & 1:
                profile_names.append("Domain")
            if profile_mask & 2:
                profile_names.append("Private")
            if profile_mask & 4:
                profile_names.append("Public")
            logger.info(
                "Firewall rule: enabled=%s profiles=%s (%s) program=%s localPort=%s",
                rule.get("enabled"),
                profile_mask,
                ",".join(profile_names) or "none",
                rule.get("program"),
                rule.get("localPort"),
            )
            if on_public and not (profile_mask & 4):
                logger.warning(
                    "Network is Public but the firewall rule does not include the "
                    "Public profile — LAN clients cannot reach TCP %s. Set the home "
                    "network to Private in Windows Settings, or re-run scripts/install.py "
                    "and allow Public networks.",
                    agent_port,
                )
            program = rule.get("program") or ""
            if program and os.path.normcase(python_executable) != os.path.normcase(program):
                logger.warning(
                    "Firewall rule program %r does not match this process %r — "
                    "inbound connections may be blocked.",
                    program,
                    python_executable,
                )
