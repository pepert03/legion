"""One sync process per machine, so commands don't sync.

Each command used to copy the Radicle storage's refs into its clone (6 s through WSL) and push what it wrote (10 to
20 s), and each agent on a machine did it on its own, so their pushes collided on the one node. Now a single process
per machine (per LEGION_HOME) does it for every clone that used legion lately: it syncs each one every EVERY
seconds, or at once when a command wakes it with something to publish. The commands then only read and write
locally. Its sync also starts the background network fetch, which restarts the Radicle node if it stopped (a
suspended computer stopped it twice, and the agents worked cut off for hours without knowing).

It's started by the first command that finds it isn't running, and it ends by itself when legion is updated (the
next command starts the new version) or when no clone has used it for IDLE seconds. LEGION_NO_DAEMON=1 turns it
off: every command syncs by itself, as before."""

import hashlib
import os
import sys
import time
from pathlib import Path

from . import coordgit
from .identity import Identity, home
from .locks import held, lock_file

EVERY = float(os.environ.get("LEGION_DAEMON_EVERY", 10))  # seconds between syncs of each clone
FRESH = 60  # a daemon whose last beat is older than this is taken as gone
IDLE = 6 * 3600  # a clone unused this long is dropped; with none left, the daemon ends


def folder() -> Path:
    return home() / "daemon"


def enabled() -> bool:
    return not os.environ.get("LEGION_NO_DAEMON")


def repo_lock(repo) -> Path:
    """The lock that keeps the daemon and a command from moving a clone's coord at the same time."""
    git_dir = coordgit.run(repo, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
    return Path(git_dir or Path(repo) / ".git") / "legion-sync.lock"


def _entry(repo) -> Path:
    path = str(Path(repo).resolve())
    return folder() / "repos" / hashlib.sha1(path.encode()).hexdigest()[:16]


def register(repo) -> None:
    """This clone is in use: the daemon syncs it (for IDLE seconds after its last command)."""
    e = _entry(repo)
    e.parent.mkdir(parents=True, exist_ok=True)
    e.write_text(str(Path(repo).resolve()), encoding="utf-8")


def alive() -> bool:
    try:
        return time.time() - (folder() / "beat").stat().st_mtime < FRESH
    except OSError:
        return False


def serving(repo) -> bool:
    """Whether the daemon syncs this clone now. Registers it either way, and starts the daemon if it isn't
    running: this command then syncs by itself, the next ones won't have to."""
    if not enabled():
        return False
    register(repo)
    if alive():
        return True
    start()
    return False


def start() -> None:
    from .radicle import spawn_detached
    folder().mkdir(parents=True, exist_ok=True)
    log = folder() / "log.txt"
    if log.exists() and log.stat().st_size > 1_000_000:  # keep the last of it only
        log.write_text(log.read_text(encoding="utf-8", errors="replace")[-200_000:], encoding="utf-8")
    try:
        spawn_detached([sys.executable, "-m", "legion", "daemon"], str(folder()), log=folder() / "log.txt")
    except OSError:
        pass


def wake() -> None:
    """Something to publish: sync now rather than at the next round."""
    w = folder() / "wake"
    w.parent.mkdir(parents=True, exist_ok=True)
    w.write_text(str(time.time()), encoding="utf-8")


def _code_stamp() -> float:
    """When legion's code last changed: an update rewrites these files, and the daemon makes way for the new one."""
    here = Path(__file__).parent
    return max((p.stat().st_mtime for p in here.glob("*.py")), default=0.0)


def sync_one(repo: str, log=print) -> bool:
    try:
        ident = Identity.load(repo=repo)
        if ident is None or not coordgit.is_radicle(repo):
            return False
        # The lock only while the coord moves here: commands append to it under the same lock, and held it off
        # for the whole push too (20 s, and minutes for a first one), past the time agents' tools allow them.
        with held(repo_lock(repo)):
            coordgit.sync(repo, ident, publish=False)
        coordgit.publish_radicle(repo, ident)
        clear_stale_locks(repo, log)
        return True
    except Exception as e:  # noqa: BLE001 (one clone's trouble mustn't stop the others')
        log(f"{time.strftime('%H:%M:%S')} {repo}: {str(e).strip()[-300:]}")
        return False


STALE_LOCK = 600  # seconds after which a lock file in the node's storage is a leftover, looked for this often


def clear_stale_locks(repo, log=print) -> list[str]:
    """Remove the ref locks a Radicle node left in its storage when it was cut off mid-write (packed-refs.lock,
    and any under refs/; not gc.log.lock, which a long background gc holds). With a stale
    packed-refs.lock, every fetch that deleted a ref failed ("failed to delete reference"), and since nodes
    drop judged patches and old saves they all do: a node in Erdős #902 saw nothing new for two hours. At most
    every STALE_LOCK seconds per clone (.git/legion-locks-checked)."""
    from . import radicle
    stamp = Path(coordgit.run(repo, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
                 or Path(repo) / ".git") / "legion-locks-checked"
    try:
        if time.time() - stamp.stat().st_mtime < STALE_LOCK:
            return []
    except OSError:
        pass
    try:
        stamp.touch()
        where = radicle.storage(repo)
        if radicle.IS_WINDOWS and not radicle.native(repo):  # the storage lives in WSL
            minutes = STALE_LOCK // 60
            out = radicle.run("sh", "-c", f'find "{where}" -maxdepth 1 -name packed-refs.lock -mmin +{minutes} -print -delete; '
                              f'find "{where}/refs" -name "*.lock" -mmin +{minutes} -print -delete',
                              repo=repo, check=False, timeout=120).stdout
            gone = out.split()
        else:
            gone, now = [], time.time()
            for p in [*Path(where).glob("packed-refs.lock"), *Path(where, "refs").rglob("*.lock")]:
                if now - p.stat().st_mtime > STALE_LOCK:
                    p.unlink()
                    gone.append(str(p))
    except (radicle.RadError, OSError):
        return []
    if gone:
        log(f"{time.strftime('%H:%M:%S')} {repo}: removed {len(gone)} lock file(s) the Radicle node left behind: "
            + ", ".join(g.rsplit("/", 1)[-1] for g in gone[:5]))
    return gone


def _log(message: str) -> None:
    print(message, flush=True)  # its output is a file: unflushed, nothing showed while it ran


def run(rounds: int | None = None, log=_log) -> int:
    """The loop. `rounds` bounds it (for tests); None runs until idle or updated."""
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    me = open(d / "lock", "a+b")  # noqa: SIM115 (held for the process's life)
    if not lock_file(me):
        return 0  # another daemon serves this machine
    stamp, done = _code_stamp(), 0
    log(f"{time.strftime('%H:%M:%S')} legion daemon started (pid {os.getpid()})")
    while rounds is None or done < rounds:
        (d / "beat").write_text(str(os.getpid()), encoding="utf-8")
        entries = sorted((d / "repos").glob("*")) if (d / "repos").exists() else []
        now, used = time.time(), 0
        for e in entries:
            try:
                path, age = e.read_text(encoding="utf-8").strip(), now - e.stat().st_mtime
            except OSError:
                continue
            if age > IDLE or not Path(path).exists():
                e.unlink(missing_ok=True)
                continue
            used += 1
            sync_one(path, log)
        done += 1
        if rounds is not None and done >= rounds:
            break
        if not used and not entries:
            log(f"{time.strftime('%H:%M:%S')} no clone in use: the daemon ends")
            break
        if _code_stamp() != stamp:
            log(f"{time.strftime('%H:%M:%S')} legion was updated: the daemon ends, and the next command starts the new one")
            break
        _wait(d / "wake", EVERY)
    return 0


def _wait(wake: Path, seconds: float) -> None:
    def mtime():
        try:
            return wake.stat().st_mtime
        except OSError:
            return 0.0
    seen, end = mtime(), time.time() + seconds
    while time.time() < end:
        time.sleep(0.5)
        if mtime() != seen:
            return
