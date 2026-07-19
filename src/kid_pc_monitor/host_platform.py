"""OS-specific host operations used by the kid PC monitoring agent."""

from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod


class HostPlatform(ABC):
    """Session lock, shutdown, UI, and connectivity hooks for the current OS."""

    @abstractmethod
    def check_session_locked(self) -> bool:
        """True when this process's interactive session is locked."""

    @abstractmethod
    def session_is_active(self) -> bool:
        """True when this session is the active console session and not locked."""

    @abstractmethod
    def lock_workstation(self) -> None:
        """Lock the interactive session (e.g. LockWorkStation)."""

    @abstractmethod
    def shutdown(self, seconds: int = 60) -> None:
        """Begin a timed shutdown."""

    @abstractmethod
    def cancel_shutdown(self) -> None:
        """Cancel a pending shutdown, if supported."""

    @abstractmethod
    def show_message(self, message: str, title: str = "PC Time Control") -> None:
        """Show a non-blocking warning or notice to the logged-in user."""

    @abstractmethod
    def get_hostname(self) -> str:
        """Machine name for agent protocol responses."""

    def update_time_overlay(self, text: str | None, *, urgent: bool = False) -> None:
        """Show or update an always-on-top on-screen time-remaining overlay for
        the logged-in user, or hide it when ``text`` is ``None``. ``urgent``
        asks for an attention-grabbing style for the final minutes before a lock.

        Default is a no-op so only platforms with a desktop UI need to implement
        it (the agent calls this every tick from its monitor loop)."""
        return

    def log_connectivity_diagnostics(
        self,
        logger: logging.Logger,
        *,
        agent_port: int,
        log_file: str,
        log_level_name: str,
        python_executable: str,
    ) -> None:
        """Log OS-specific network/firewall hints; default is a no-op."""
        return


def get_default_platform() -> HostPlatform:
    """Return the platform implementation for the running OS."""
    if sys.platform == "win32":
        from kid_pc_monitor.platforms.windows import WindowsHostPlatform

        return WindowsHostPlatform()

    raise NotImplementedError(
        f"No HostPlatform implementation for {sys.platform!r} yet "
        "(Windows is supported; Linux/macOS can subclass HostPlatform)."
    )
