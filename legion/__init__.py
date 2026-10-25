"""Legion: coordinating many agents on one repository, without a central server."""

import os
import subprocess

__version__ = "0.1.0"


def _hide_child_consoles() -> None:
    """On Windows, a process with no console (started by a GUI app, or detached) makes every console
    program it runs (git, wsl) open its own window, which flashes on screen. If legion has no console,
    start those programs with a hidden one instead."""
    import ctypes
    if ctypes.windll.kernel32.GetConsoleCP():  # attached to a console: children share it, nothing pops up
        return
    original = subprocess.Popen.__init__
    own_console = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE

    def init(self, *args, **kw):
        if not kw.get("creationflags", 0) & own_console:
            kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        original(self, *args, **kw)

    subprocess.Popen.__init__ = init


if os.name == "nt":
    _hide_child_consoles()
