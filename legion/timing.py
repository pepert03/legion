"""LEGION_TIMING=1: where a command's time went, on stderr. A step of the sync round says how long it took as it
ends; what happens many times (loading the state, git's calls) is summed and said at the end, with how much of
the whole the process spent working and how much waiting.

Instinct's agents measured claims of 60 to 120 s from the outside and nothing said which step they went on. Then
muse's took 306 s of which the steps named 89, on a machine with a load of 26, and Instinct's 19 s of which they
named 0.4: the rest was loading Python, loading the state, waiting for a lock to record the event, and waiting
for a turn on the CPU, and nothing said which."""

import os
import sys
import time

BEGAN = time.time()  # when legion's code was first loaded in this process
_sums: dict[str, list] = {}  # what -> [seconds, times]
_named = 0.0  # seconds the steps said so far add up to (they come one after another)
STATE, GIT = "loading the state", "git"


def on() -> bool:
    return bool(os.environ.get("LEGION_TIMING"))


def timed(what: str, since: float, part: bool = False) -> None:
    """A step that happens once: said as it ends. `part`: a detail of a step that is said too (not added twice)."""
    global _named
    if on():
        took = time.time() - since
        if not part:
            _named += took
        print(f"legion: timing: {what} {took:.1f} s", file=sys.stderr, flush=True)


def add(what: str, since: float) -> None:
    """A step that happens many times: summed, and said at the end."""
    if on():
        s = _sums.setdefault(what, [0.0, 0])
        s[0] += time.time() - since
        s[1] += 1


def note(text: str) -> None:
    if on():
        print(f"legion: timing: {text}", file=sys.stderr, flush=True)


def process_began() -> float:
    """When this process started, before Python and legion's code were loaded (BEGAN where it can't tell)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.GetCurrentProcess.restype = wintypes.HANDLE
            k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            made, a, b, c = (wintypes.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(k.GetCurrentProcess(), made, a, b, c):
                return BEGAN
            t = ((made.dwHighDateTime << 32) | made.dwLowDateTime) / 1e7 - 11644473600  # from 1601 to 1970
        else:
            with open("/proc/self/stat", encoding="utf-8") as f:
                ticks = float(f.read().rsplit(")", 1)[1].split()[19])  # starttime, in ticks since the boot
            with open("/proc/stat", encoding="utf-8") as f:
                boot = float(next(line for line in f if line.startswith("btime")).split()[1])
            t = boot + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:  # noqa: BLE001 (no /proc: a Mac)
        return BEGAN
    return t if 0 <= BEGAN - t < 3600 else BEGAN


def machine() -> str:
    """How busy the machine is: a command that waits for its turn on the CPU takes as many times longer."""
    try:
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
        return f" (load {os.getloadavg()[0]:.1f} on {cpus} CPU{'s' if cpus != 1 else ''})"
    except (OSError, AttributeError):
        return ""


def summary(cmd: str, started: float) -> None:
    """The last lines of a command: what was summed, what no step named, and work against waiting."""
    if not on():
        return
    now = time.time()
    state = _sums.get(STATE, [0.0, 0])
    for what, (secs, n) in _sums.items():
        if what != GIT:
            print(f"legion: timing: {what}, {n} time{'s' if n != 1 else ''}: {secs:.1f} s", file=sys.stderr)
    began = process_began()
    if started - began >= 0.05:
        print(f"legion: timing: starting up (Python and legion's code) {started - began:.1f} s", file=sys.stderr)
    rest = (now - started) - _named - state[0]
    if rest >= 0.05:
        print(f"legion: timing: the rest (the command's own work, and what it prints) {rest:.1f} s", file=sys.stderr)
    calls = _sums.get(GIT)
    if calls:
        print(f"legion: timing: git was called {calls[1]} times: {calls[0]:.1f} s of the above", file=sys.stderr)
    t = os.times()
    ran = t.children_user + t.children_system
    print(f"legion: timing: of {now - began:.1f} s, {t.user + t.system:.1f} on the CPU"
          + (f" and {ran:.1f} the programs it ran" if ran >= 0.05 else "")
          + f": the rest it waited, for the network, a lock, the disk or its turn{machine()}", file=sys.stderr)
    print(f"legion: timing: {cmd} took {now - started:.1f} s in all", file=sys.stderr)
