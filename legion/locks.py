"""File locks that the operating system frees when their process ends, however it ends."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


def lock_file(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock_file(f) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _who(path: Path) -> Path:
    return path.with_name(path.name + ".who")


@contextmanager
def held(path: Path, wait: float = 120, what: str = ""):
    """Hold the lock at `path`, waiting up to `wait` seconds for it; after that, go on without it rather than
    stop an agent (the worst case is a git ref that moved meanwhile, which the caller retries). `what`: what it
    is held for, which whoever waits for it can say (see holder)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115
    got, end = lock_file(f), time.time() + wait
    while not got and time.time() < end:
        time.sleep(0.2)
        got = lock_file(f)
    said = False
    if got and what:
        try:
            _who(path).write_text(json.dumps({"pid": os.getpid(), "what": what, "since": time.time()}),
                                  encoding="utf-8")
            said = True
        except OSError:
            pass
    try:
        yield got
    finally:
        if said:
            try:
                _who(path).unlink()
            except OSError:
                pass
        if got:
            unlock_file(f)
        f.close()


def holder(path: Path) -> str:
    """Who holds the lock at `path` and what for, as far as it said: "pid 412, the sync process's round, for 38 s".
    A command that waited 15 s for its clone's lock said another process had it, and nothing said which: on
    muse's machine one held it through a whole claim, 300 s, and nobody could tell what it was."""
    try:
        who = json.loads(_who(path).read_text(encoding="utf-8"))
        return f"pid {who['pid']}, {who['what']}, for {time.time() - float(who['since']):.0f} s"
    except (OSError, ValueError, KeyError, TypeError):
        return ""
