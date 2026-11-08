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
import threading
import time
from pathlib import Path

from . import coordgit
from .identity import Identity, home
from .locks import held, lock_file

EVERY = float(os.environ.get("LEGION_DAEMON_EVERY", 10))  # seconds between syncs of each clone
FRESH = 60  # a daemon whose last beat is older than this is taken as gone
IDLE = 6 * 3600  # a clone unused this long is dropped; with none left, the daemon ends
ASIDE = True  # pushes in a thread of their own (see publish_aside); off, in line
FINISH = 120  # seconds an ending daemon gives the pushes under way


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
    """A beat in the last FRESH seconds from a process that still runs. Where sandboxes kill it as each command
    returns, its beat stayed fresh for a minute after: the next commands woke a dead process and waited for a
    push that never came."""
    from .radicle import pid_alive
    beat = folder() / "beat"
    try:
        if time.time() - beat.stat().st_mtime >= FRESH:
            return False
        pid = beat.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return pid_alive(int(pid), "daemon") if pid.isdigit() else True


def serving(repo) -> bool:
    """Whether the daemon syncs this clone now. Registers it either way, and starts the daemon if it isn't
    running: this command then syncs by itself, the next ones won't have to."""
    if not enabled():
        return False
    register(repo)
    if alive():
        return True
    if not short_lived():
        start()
    return False


SHORT_LIVED, RETRY_SHORT = 120, 1800


def short_lived() -> bool:
    """The last sync process died within SHORT_LIVED seconds of starting, less than RETRY_SHORT ago: here background
    processes don't outlive the command that starts them (a sandbox). Each command started one anyway, which lived
    a few seconds and only made the commands guess (a young process: push by yourself and wait); now they sync by
    themselves, one way, and one is tried again after RETRY_SHORT."""
    d = folder()
    try:
        started = float((d / "started").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        last = (d / "beat").stat().st_mtime
    except OSError:
        last = started  # gone before its first round
    return last - started < SHORT_LIVED and time.time() - last < RETRY_SHORT


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


def age() -> float:
    """How long the running sync process has been up (0 if it can't tell). One that keeps being started anew
    lives where sandboxes kill background processes, and a command waits for it to push (see Ctx.sync)."""
    try:
        return max(0.0, time.time() - float((folder() / "started").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0.0


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
        publish_aside(repo, ident, log)
        clear_stale_locks(repo, log)
        return True
    except Exception as e:  # noqa: BLE001 (one clone's trouble mustn't stop the others')
        log(f"{time.strftime('%H:%M:%S')} {repo}: {str(e).strip()[-300:]}")
        return False


_publishing: dict[str, threading.Thread] = {}


def publish_aside(repo: str, ident, log=print) -> None:
    """Push this clone's refs in a thread of their own, one at a time per clone. In line, a push that hung (ten
    minutes before it timed out, three times an hour on jose's machine) held up the copy of what the network
    brought, for that clone and every other: the agents read nothing new for ten minutes. A clone whose push is
    still going is offered again at the next round after it ends."""
    if not ASIDE:
        coordgit.publish_radicle(repo, ident)
        return
    running = _publishing.get(repo)
    if running is not None and running.is_alive():
        return

    def push():
        try:
            try:  # a storage rebuilt from an older copy forks this node's chain, and the seeds drop what it
                # publishes: healed here too, not only by the publishes of commands (at most every HEAL_EVERY)
                from . import radicle
                healed = radicle.heal_sigrefs(repo)
                if healed:
                    log(f"{time.strftime('%H:%M:%S')} {repo}: {healed}")
            except Exception:  # noqa: BLE001
                pass
            coordgit.publish_radicle(repo, ident)
        except Exception as e:  # noqa: BLE001
            log(f"{time.strftime('%H:%M:%S')} {repo}: {str(e).strip()[-300:]}")
    t = threading.Thread(target=push, name=f"publish {repo}", daemon=True)
    _publishing[repo] = t
    t.start()


def finish_publishing(seconds: float) -> None:
    """Let the pushes under way end, up to `seconds` in all, before the process does."""
    end = time.time() + seconds
    for t in list(_publishing.values()):
        t.join(max(0.0, end - time.time()))


STALE_LOCK = 120  # seconds after which a lock no process holds open is a leftover, looked for this often (git
# holds them for milliseconds; where sandboxes kill writers mid-write, ten minutes left a node blind for twenty)


def cleanup_enabled(repo) -> bool:
    """LEGION_LOCK_CLEANUP=0, or git config legion.lockcleanup false, turns clear_stale_locks off."""
    value = os.environ.get("LEGION_LOCK_CLEANUP")
    if value is None:
        value = coordgit.run(repo, "config", "--get", "legion.lockcleanup").stdout.decode().strip()
    return value.lower() not in ("0", "false", "no", "off")


def open_files() -> set[str] | None:
    """The files some process on this machine holds open (Linux: /proc; elsewhere lsof), or None if it can't
    tell: then nothing is removed."""
    proc = Path("/proc")
    if proc.is_dir():
        held = set()
        for fd in proc.glob("[0-9]*/fd/*"):
            try:
                held.add(os.readlink(fd))
            except OSError:
                pass
        return held
    import shutil
    import subprocess
    if shutil.which("lsof"):
        out = subprocess.run(["lsof", "-Fn"], capture_output=True, text=True).stdout
        return {line[1:] for line in out.splitlines() if line.startswith("n")}
    return None


# Removes what no process holds open and has been there STALE_LOCK seconds: a writer paused for longer still
# holds its lock open, and on Windows a file another process holds open can't be removed at all.
WSL_CLEAR = """held=$(ls -l /proc/[0-9]*/fd 2>/dev/null | sed -n 's/.* -> //p')
{{ find "$1" -maxdepth 1 -name packed-refs.lock -mmin +$2; find "$1/refs" -name "*.lock" -mmin +$2; }} 2>/dev/null |
while read -r f; do printf '%s\n' "$held" | grep -qxF "$f" || {{ rm -f "$f" && echo "$f"; }}; done"""


def clear_stale_locks(repo, log=print) -> list[str]:
    """Remove the ref locks a Radicle node left in its storage when it was cut off mid-write (packed-refs.lock,
    and any under refs/; not gc.log.lock, which a long background gc holds), when no process holds them open.
    With a stale packed-refs.lock, every fetch that deleted a ref failed ("failed to delete reference"), and since
    nodes drop judged patches and old saves they all do: a node in Erdős #902 saw nothing new for two hours. At
    most every STALE_LOCK seconds per clone (.git/legion-locks-checked); off with LEGION_LOCK_CLEANUP=0.
    And the clone's own (HEAD.lock, index.lock, its ref locks): instinct's `c` had a HEAD.lock that a git cut off by
    its sandbox left, and couldn't commit, check out or save anything for a whole day."""
    from . import radicle
    if not cleanup_enabled(repo):
        return []
    stamp = Path(coordgit.run(repo, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
                 or Path(repo) / ".git") / "legion-locks-checked"
    try:
        if time.time() - stamp.stat().st_mtime < STALE_LOCK:
            return []
    except OSError:
        pass
    git_dir = stamp.parent
    clone = [*(git_dir / name for name in ("HEAD.lock", "index.lock", "packed-refs.lock")),
             *(git_dir / "refs").rglob("*.lock")]
    try:
        stamp.touch()
        held = None if radicle.IS_WINDOWS else open_files()  # Windows refuses to remove an open file itself
        if held is None and not radicle.IS_WINDOWS:
            return []
        gone = _unlink_stale(clone, held)
        where = radicle.storage(repo) if radicle.rid(repo) else None
        if where and radicle.IS_WINDOWS and not radicle.native(repo):  # the storage lives in WSL
            out = radicle.run("sh", "-c", WSL_CLEAR.format(), "sh", where, str(STALE_LOCK // 60), repo=repo,
                              check=False, timeout=120).stdout
            gone += out.split()
        elif where:
            gone += _unlink_stale([*Path(where).glob("packed-refs.lock"), *Path(where, "refs").rglob("*.lock")], held)
    except (radicle.RadError, OSError):
        return []
    if gone:
        log(f"{time.strftime('%H:%M:%S')} {repo}: removed {len(gone)} stale lock file(s), left by a process cut off mid-write: "
            + ", ".join(g.rsplit("/", 1)[-1] for g in gone[:5]))
    return gone


def _unlink_stale(paths, held) -> list[str]:
    """Remove the lock files older than STALE_LOCK that no process holds open. The ones it removed."""
    gone, now = [], time.time()
    for p in paths:
        try:
            if not p.is_file() or now - p.stat().st_mtime <= STALE_LOCK or str(p) in (held or ()):
                continue
            p.unlink()
        except OSError:  # open in another process (Windows), or gone meanwhile
            continue
        gone.append(str(p))
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
    # Started anew by an update, it's the same sync process that stays: it keeps its age. Taken for a young one, a
    # process sandboxes kill, every command synced by itself for two minutes after each update (transport.SETTLED).
    (d / "started").write_text(os.environ.get("LEGION_DAEMON_SINCE") or str(time.time()), encoding="utf-8")
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
            # The new one starts at once: left to the next command, on a machine whose agents were all on other
            # clones, jose's clone in Erdős #902 went two and a half hours without a sync after an update.
            log(f"{time.strftime('%H:%M:%S')} legion was updated: the daemon starts the new one and ends")
            me.close()  # its lock, for the new one
            (d / "beat").unlink(missing_ok=True)
            try:
                os.environ["LEGION_DAEMON_SINCE"] = (d / "started").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            start()
            break
        _wait(d / "wake", EVERY)
    finish_publishing(FINISH)
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
