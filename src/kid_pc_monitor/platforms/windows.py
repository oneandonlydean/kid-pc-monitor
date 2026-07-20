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
from pathlib import Path
from typing import TYPE_CHECKING

from kid_pc_monitor.agent_overlay import VALID_CORNERS, corner_geometry
from kid_pc_monitor.host_platform import HostPlatform
from kid_pc_monitor.network import get_primary_ipv4

if TYPE_CHECKING:
    import tkinter as tk
    from collections.abc import Callable

    from kid_pc_monitor.earn_quiz import EarnSession

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


class _QuizDialog:
    """A centered modal quiz window that steps through questions and awards time.

    Unlike the old per-question popups (which appeared under the corner overlay),
    this is one window centered on the screen; the result is shown in-place so it
    is never hidden behind the timer.
    """

    def __init__(self, root, subject, items, session, award) -> None:
        import tkinter as tk

        from kid_pc_monitor import earn_quiz

        self._earn_quiz = earn_quiz
        self._items = items
        self._award = award
        self._index = 0
        self._correct = 0
        self._done = False

        title = "Earn time — " + ("Maths" if subject == earn_quiz.MATHS else "Spelling")
        top = tk.Toplevel(root)
        top.title(title)
        top.configure(bg="#202020", padx=22, pady=16)
        top.resizable(False, False)
        top.attributes("-topmost", True)
        self._top = top

        header = (
            f"Answer to earn up to {session.remaining_minutes} min "
            f"({session.reward_minutes} min per correct answer)."
        )
        tk.Label(
            top,
            text=header,
            font=("Segoe UI", 9),
            fg="#bbbbbb",
            bg="#202020",
            wraplength=360,
            justify="left",
        ).pack(fill="x")

        self._progress = tk.StringVar()
        tk.Label(
            top,
            textvariable=self._progress,
            font=("Segoe UI", 9, "bold"),
            fg="#4caf50",
            bg="#202020",
        ).pack(fill="x", pady=(10, 2))

        self._prompt = tk.StringVar()
        tk.Label(
            top,
            textvariable=self._prompt,
            font=("Segoe UI", 13),
            fg="#ffffff",
            bg="#202020",
            wraplength=360,
            justify="center",
        ).pack(fill="x", pady=(6, 10))

        self._entry = tk.Entry(top, font=("Segoe UI", 13), justify="center")
        self._entry.pack(fill="x")
        self._entry.bind("<Return>", lambda _e: self._submit())

        self._submit_button = tk.Button(
            top,
            text="Submit",
            font=("Segoe UI", 10, "bold"),
            bg="#2e7d32",
            fg="#ffffff",
            relief="flat",
            cursor="hand2",
            command=self._submit,
        )
        self._submit_button.pack(fill="x", pady=(12, 0))

        top.protocol("WM_DELETE_WINDOW", self._cancel)
        self._show_current()
        self._center()
        top.grab_set()
        self._entry.focus_set()

    def _center(self) -> None:
        top = self._top
        top.update_idletasks()
        width, height = top.winfo_width(), top.winfo_height()
        screen_w, screen_h = top.winfo_screenwidth(), top.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        top.geometry(f"+{x}+{y}")

    def _show_current(self) -> None:
        item = self._items[self._index]
        self._progress.set(f"Question {self._index + 1} of {len(self._items)}")
        self._prompt.set(item.prompt)
        self._entry.delete(0, "end")

    def _submit(self) -> None:
        if self._done:
            return
        item = self._items[self._index]
        if self._earn_quiz.is_correct(item.answer, self._entry.get()):
            self._correct += 1
        self._index += 1
        if self._index >= len(self._items):
            self._finish()
        else:
            self._show_current()

    def _cancel(self) -> None:
        # Keep whatever they earned so far, then close without a result screen.
        if not self._done:
            self._done = True
            try:
                self._award(self._correct)
            except Exception:
                pass
        self._close()

    def _finish(self) -> None:
        self._done = True
        awarded = 0
        try:
            awarded = self._award(self._correct)
        except Exception as exc:
            logging.getLogger("PCTimeControl").error("Award failed: %s", exc)
        if awarded > 0:
            message = f"Great job!\nYou got {self._correct} right and earned {awarded} minute(s)."
        else:
            message = f"You got {self._correct} right.\nNo minutes earned this time."
        self._progress.set("All done!")
        self._prompt.set(message)
        try:
            self._entry.pack_forget()
        except Exception:
            pass
        self._submit_button.config(text="Close", command=self._close)
        self._center()

    def _close(self) -> None:
        try:
            self._top.grab_release()
            self._top.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self._top.wait_window()


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
        self._desired_detail = ""
        self._started = False
        self._root: tk.Tk | None = None
        self._label: tk.Label | None = None
        self._detail_label: tk.Label | None = None
        self._button: tk.Button | None = None
        self._earn_spell_button: tk.Button | None = None
        self._earn_math_button: tk.Button | None = None
        self._move_button: tk.Button | None = None
        self._on_request: Callable[[], None] | None = None
        # Kid-chosen position (persisted locally, independent of parent settings).
        self._corner = "top-right"
        self._monitor = 0
        self._monitors: list[tuple[int, int, int, int]] = []
        self._placed_key: tuple[str, int, int, int] | None = None
        self._earn_start: Callable[[], EarnSession | None] | None = None
        self._earn_award: Callable[[int], int] | None = None

    def set_request_handler(self, handler: Callable[[], None] | None) -> None:
        """Set the callback invoked when the kid clicks 'Ask for more time'."""
        with self._lock:
            self._on_request = handler

    def set_earn_handler(
        self,
        start_session: Callable[[], EarnSession | None] | None,
        award: Callable[[int], int] | None,
    ) -> None:
        """Set the callbacks for the 'Earn time' spelling quiz."""
        with self._lock:
            self._earn_start = start_session
            self._earn_award = award

    def update(self, text: str | None, urgent: bool = False, detail: str = "") -> None:
        """Publish the text/urgency to display (or ``None`` to hide); start UI lazily."""
        with self._lock:
            self._desired_text = text
            self._desired_urgent = urgent
            self._desired_detail = detail
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
            # Ignore window-close requests (Alt+F4, "End task" close) so a child
            # can't dismiss the timer. Enforcement is independent, but the kid
            # should always see the countdown; hiding is done via withdraw().
            root.protocol("WM_DELETE_WINDOW", lambda: None)
            root.configure(bg=self._NORMAL_BG)
            try:
                root.attributes("-alpha", 0.9)
            except tk.TclError:
                pass  # transparency is best-effort
            label = tk.Label(
                root,
                text="",
                font=("Segoe UI", 11, "bold"),
                fg="#ffffff",
                bg=self._NORMAL_BG,
                padx=14,
                pady=7,
            )
            label.pack(fill="x")
            detail_label = tk.Label(
                root,
                text="",
                font=("Segoe UI", 8),
                fg="#bbbbbb",
                bg=self._NORMAL_BG,
                justify="center",
            )
            # Packed/unpacked on demand in _refresh so an empty detail leaves no gap.
            button = tk.Button(
                root,
                text="Ask for more time",
                font=("Segoe UI", 9),
                relief="flat",
                bg="#3a3a3a",
                fg="#ffffff",
                activebackground="#4a4a4a",
                activeforeground="#ffffff",
                cursor="hand2",
                command=self._handle_request_click,
            )
            button.pack(fill="x", padx=6, pady=(0, 4))
            earn_spell_button = tk.Button(
                root,
                text="Earn: Spelling",
                font=("Segoe UI", 9),
                relief="flat",
                bg="#2e7d32",
                fg="#ffffff",
                activebackground="#1b5e20",
                activeforeground="#ffffff",
                cursor="hand2",
                command=lambda: self._handle_earn_click("spelling"),
            )
            earn_spell_button.pack(fill="x", padx=6, pady=(0, 4))
            earn_math_button = tk.Button(
                root,
                text="Earn: Maths",
                font=("Segoe UI", 9),
                relief="flat",
                bg="#1565c0",
                fg="#ffffff",
                activebackground="#0d47a1",
                activeforeground="#ffffff",
                cursor="hand2",
                command=lambda: self._handle_earn_click("maths"),
            )
            earn_math_button.pack(fill="x", padx=6, pady=(0, 4))
            move_button = tk.Button(
                root,
                text="Move ▾",
                font=("Segoe UI", 8),
                relief="flat",
                bg="#2a2a2a",
                fg="#cccccc",
                activebackground="#3a3a3a",
                activeforeground="#ffffff",
                cursor="hand2",
                command=self._handle_move_click,
            )
            move_button.pack(fill="x", padx=6, pady=(0, 6))
            self._root = root
            self._label = label
            self._detail_label = detail_label
            self._button = button
            self._earn_spell_button = earn_spell_button
            self._earn_math_button = earn_math_button
            self._move_button = move_button
            self._load_position()
            self._monitors = self._enumerate_monitors(root)
            self._place(root, force=True)
            root.after(self._POLL_MS, self._refresh)
            try:
                root.mainloop()
            finally:
                # If the window is ever torn down (a forced close, a Tk crash),
                # let the next update() from the monitor loop relaunch it so the
                # kid never permanently loses the timer.
                self._root = None
                with self._lock:
                    self._started = False
        except Exception as exc:
            logger.error("On-screen timer crashed: %s", exc, exc_info=True)

    def _handle_request_click(self) -> None:
        with self._lock:
            handler = self._on_request
        if handler is not None:
            try:
                handler()
            except Exception as exc:
                logging.getLogger("PCTimeControl").error("Time-request handler failed: %s", exc)
        button = self._button
        root = self._root
        if button is not None:
            try:
                button.config(text="Request sent", state="disabled")
                if root is not None:
                    root.after(4000, self._restore_request_button)
            except Exception:
                pass

    def _restore_request_button(self) -> None:
        button = self._button
        if button is not None:
            try:
                button.config(text="Ask for more time", state="normal")
            except Exception:
                pass

    def _refresh_earn_button(self) -> None:
        """Enable the Earn buttons only when a quiz is currently available."""
        buttons = [self._earn_spell_button, self._earn_math_button]
        if not any(buttons):
            return
        available = False
        with self._lock:
            start = self._earn_start
        if start is not None:
            try:
                available = start() is not None
            except Exception:
                available = False
        for button in buttons:
            if button is not None:
                try:
                    button.config(state="normal" if available else "disabled")
                except Exception:
                    pass

    def _handle_earn_click(self, subject: str) -> None:
        with self._lock:
            start = self._earn_start
            award = self._earn_award
        root = self._root
        if start is None or award is None or root is None:
            return
        try:
            import tkinter.messagebox as messagebox

            from kid_pc_monitor import earn_quiz
        except Exception as exc:
            logging.getLogger("PCTimeControl").error("Quiz UI unavailable: %s", exc)
            return

        session = start()
        if session is None:
            messagebox.showinfo(
                "Earn time", "Earning extra time isn't available right now.", parent=root
            )
            return

        from random import Random

        difficulty = (
            session.maths_difficulty if subject == earn_quiz.MATHS else session.spelling_difficulty
        )
        items = earn_quiz.generate_quiz(subject, session.questions, Random(), difficulty)
        try:
            _QuizDialog(root, subject, items, session, award).run()
        except Exception as exc:
            logging.getLogger("PCTimeControl").error("Quiz dialog failed: %s", exc, exc_info=True)

    def _pos_path(self) -> Path:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KidPCMonitor"
        return base / "overlay_pos.json"

    def _load_position(self) -> None:
        try:
            data = json.loads(self._pos_path().read_text(encoding="utf-8"))
        except Exception:
            return
        corner = data.get("corner")
        if corner in VALID_CORNERS:
            self._corner = corner
        monitor = data.get("monitor")
        if isinstance(monitor, int) and monitor >= 0:
            self._monitor = monitor

    def _save_position(self) -> None:
        try:
            path = self._pos_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"corner": self._corner, "monitor": self._monitor}),
                encoding="utf-8",
            )
        except Exception as exc:
            logging.getLogger("PCTimeControl").debug("Could not save overlay position: %s", exc)

    def _enumerate_monitors(self, root: tk.Tk) -> list[tuple[int, int, int, int]]:
        """Return each monitor's (left, top, right, bottom); falls back to primary."""
        rects: list[tuple[int, int, int, int]] = []
        try:
            enum_proc = ctypes.WINFUNCTYPE(
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.RECT),
                ctypes.c_void_p,
            )

            def _callback(_hmon, _hdc, lprc, _lparam) -> int:
                r = lprc.contents
                rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
                return 1

            ctypes.windll.user32.EnumDisplayMonitors(0, 0, enum_proc(_callback), 0)
        except Exception as exc:
            logging.getLogger("PCTimeControl").debug("Monitor enumeration failed: %s", exc)
        if rects:
            return rects
        try:
            return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
        except Exception:
            return [(0, 0, 1920, 1080)]

    def _current_monitor_rect(self, root: tk.Tk) -> tuple[int, int, int, int]:
        monitors = self._monitors
        if monitors and 0 <= self._monitor < len(monitors):
            return monitors[self._monitor]
        if monitors:
            return monitors[0]
        return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())

    def _place(self, root: tk.Tk, *, force: bool = False) -> None:
        root.update_idletasks()
        width = root.winfo_width()
        height = root.winfo_height()
        key = (self._corner, self._monitor, width, height)
        if not force and key == self._placed_key:
            return
        x, y = corner_geometry(self._corner, self._current_monitor_rect(root), width, height)
        root.geometry(f"+{x}+{y}")
        self._placed_key = key

    def _handle_move_click(self) -> None:
        import tkinter as tk

        root = self._root
        button = self._move_button
        if root is None or button is None:
            return
        self._monitors = self._enumerate_monitors(root)
        menu = tk.Menu(root, tearoff=0)
        for label, corner in (
            ("Top-left", "top-left"),
            ("Top-right", "top-right"),
            ("Bottom-left", "bottom-left"),
            ("Bottom-right", "bottom-right"),
        ):
            mark = "● " if corner == self._corner else "   "
            menu.add_command(label=mark + label, command=lambda c=corner: self._set_corner(c))
        if len(self._monitors) > 1:
            menu.add_separator()
            for index in range(len(self._monitors)):
                mark = "● " if index == self._monitor else "   "
                menu.add_command(
                    label=f"{mark}Screen {index + 1}",
                    command=lambda i=index: self._set_monitor(i),
                )
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _set_corner(self, corner: str) -> None:
        self._corner = corner
        self._save_position()
        if self._root is not None:
            self._place(self._root, force=True)

    def _set_monitor(self, index: int) -> None:
        self._monitor = index
        self._save_position()
        if self._root is not None:
            self._place(self._root, force=True)

    def _update_detail(self, detail: str, *, bg: str, urgent: bool) -> None:
        """Show the breakdown lines under the countdown, hiding the label when empty."""
        widget = self._detail_label
        if widget is None:
            return
        if not detail:
            widget.pack_forget()
            return
        fg = self._URGENT_FG if urgent else "#bbbbbb"
        if widget.cget("text") != detail or widget.cget("bg") != bg:
            widget.config(text=detail, bg=bg, fg=fg)
        if not widget.winfo_ismapped():
            # Sits between the countdown and the buttons; falls back to append
            # order if the buttons are not built yet.
            if self._button is not None:
                widget.pack(fill="x", before=self._button, padx=6, pady=(0, 4))
            else:
                widget.pack(fill="x", padx=6, pady=(0, 4))

    def _refresh(self) -> None:
        root = self._root
        label = self._label
        if root is None or label is None:
            return
        with self._lock:
            text = self._desired_text
            urgent = self._desired_urgent
            detail = self._desired_detail
        try:
            if text is None:
                root.withdraw()
            else:
                bg = self._URGENT_BG if urgent else self._NORMAL_BG
                fg = self._URGENT_FG if urgent else self._NORMAL_FG
                if label.cget("text") != text or label.cget("bg") != bg:
                    label.config(text=text, bg=bg, fg=fg)
                    root.configure(bg=bg)
                self._update_detail(detail, bg=bg, urgent=urgent)
                self._place(root)
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)  # reassert above other windows
                self._refresh_earn_button()
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

    def update_time_overlay(
        self, text: str | None, *, urgent: bool = False, detail: str = ""
    ) -> None:
        self._time_overlay.update(text, urgent, detail)

    def set_overlay_request_handler(self, handler: Callable[[], None] | None) -> None:
        self._time_overlay.set_request_handler(handler)

    def set_overlay_earn_handler(
        self,
        start_session: Callable[[], EarnSession | None] | None,
        award: Callable[[int], int] | None,
    ) -> None:
        self._time_overlay.set_earn_handler(start_session, award)

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
                # parent=root is essential: without it the dialog attaches to the
                # process default root — the persistent countdown overlay, on
                # another thread — and tears it down when the dialog closes.
                messagebox.showwarning(title, message, parent=root)
            except Exception as exc:
                logging.getLogger("PCTimeControl").error("Error showing message: %s", exc)
            finally:
                if root:
                    try:
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
