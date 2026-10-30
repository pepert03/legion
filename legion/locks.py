"""File locks that the operating system frees when their process ends, however it ends."""

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


@contextmanager
def held(path: Path, wait: float = 120):
    """Hold the lock at `path`, waiting up to `wait` seconds for it; after that, go on without it rather than
    stop an agent (the worst case is a git ref that moved meanwhile, which the caller retries)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115
    got, end = lock_file(f), time.time() + wait
    while not got and time.time() < end:
        time.sleep(0.2)
        got = lock_file(f)
    try:
        yield got
    finally:
        if got:
            unlock_file(f)
        f.close()
