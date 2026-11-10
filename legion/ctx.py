"""Shared context for the commands: repository, identity and state."""

import os
import subprocess
import sys
import time
from pathlib import Path

from . import coordgit
from . import events as E
from . import locks
from . import state as S
from . import timing
from . import transport
from .identity import Identity


EMITTED: list[dict] = []  # the events this process recorded, in order: what a command's --json reports


class Fail(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def repo_root() -> Path:
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if p.returncode:
        raise Fail("you are not inside a git repository")
    return Path(p.stdout.strip())


def need_identity(repo=None) -> Identity:
    ident = Identity.load(repo=repo)
    if not ident:
        raise Fail("you have no identity: python -m legion id new --name YOUR_NAME")
    return ident


AGENT_VARS = (("LEGION_AGENT", "legion.agent"), ("LEGION_MODEL", "legion.model"))
LOCK_WAIT, PUBLISH_WAIT, SETTLED = transport.LOCK_WAIT, transport.PUBLISH_WAIT, transport.SETTLED  # older callers
RECORD_WAIT, RECORD_TRIES = 30, 5  # seconds an event waits for the clone's lock; tries to append it without it


def remember_agent(repo) -> None:
    """Which agent and model work in this repository: kept in its git config, so the agent only has to say it
    once (at join), not in every shell. A variable set in the shell wins, and is remembered for next time."""
    for var, key in AGENT_VARS:
        saved = " ".join(coordgit.setting(repo, key))
        value = os.environ.get(var) or (E.detect_agent() if var == "LEGION_AGENT" else None)
        if value and value != saved:
            subprocess.run(["git", "config", key, value], cwd=repo, capture_output=True)
        elif saved and not os.environ.get(var):
            os.environ[var] = saved


class Ctx:
    last = None  # the latest one made in this process: what a command's last lines read the agent's inbox from

    def __init__(self, sync: bool = True):
        Ctx.last = self
        self.repo = repo_root()
        self.ident = need_identity(self.repo)
        remember_agent(self.repo)
        self.unreachable: list[str] = []
        self.unpublished: list[str] = []  # events recorded here that haven't gone out yet
        if sync:
            self.sync()
        else:
            self.reload()

    @property
    def me(self) -> str:
        return self.ident.author

    def reload(self) -> None:
        self.st = coordgit.state(self.repo)

    strict = False  # the integrator sets it: a pass with a failed sync is retried, not run on stale state

    def sync(self) -> None:
        """Pull what the network has and push what this clone recorded: one round of transport.sync, unless
        LEGION_SYNC=off says commands stay local (then `legion netsync`, or the sync process, does it)."""
        transport.sync(self)

    def _publish(self, wait: bool = False) -> bool:
        return transport.publish(self, wait)

    def _sync_failed(self, e: Exception) -> None:
        transport.sync_failed(self, e)

    def emit(self, type: str, **payload) -> dict:
        """Validate the event against the current state and, if valid, append it to the local coord."""
        ev = E.create(self.ident, type, self.st.max_lamport + 1, **payload)
        trial = coordgit.state(self.repo, extra=[ev])
        err = next((e for x, e in trial.ignored if x.get("id") == ev["id"]), None)
        if err:
            raise Fail(f"event rejected: {err}")
        # This machine's sync process may be merging into it. Not two minutes for the lock, in silence: on muse's
        # machine something held it through a whole claim, and that was most of the claim's 300 s. Without it, the
        # log may move between reading it and writing it, and the event is appended again to where it is by then.
        lock, t0 = transport.repo_lock(self.repo), time.time()
        with locks.held(lock, wait=RECORD_WAIT, what="a command recording an event") as got:
            if time.time() - t0 >= 1:
                who = locks.holder(lock)
                timing.timed("waiting for the clone's lock, to record"
                             + ("" if got else f" (not got: {who or 'another process holds it'})"), t0)
            for left in range(RECORD_TRIES, 0, -1):
                try:
                    coordgit.append(self.repo, self.ident, ev)
                    break
                except coordgit.GitError:
                    if got or left == 1:
                        raise
                    time.sleep(0.3)
        self.st = trial
        EMITTED.append(ev)
        # The receipt, before anything goes out: an agent whose tool cut the command off while it published used
        # to retry, or check by hand, not knowing whether the event counted. Recorded here, it counts, and goes
        # out with this command's sync or the next command's.
        self.unpublished.append(f"{type} {ev['id'][:8]}")
        print(f"legion: recorded {type} {ev['id'][:8]} (it counts even if this command is cut off: it goes out "
              "now, or with your next command)", file=sys.stderr, flush=True)
        return ev

    def task(self, ref: str) -> S.Task:
        t = self.st.find_task(ref)
        if not t:
            raise Fail(f"there is no task {ref}")
        return t

    def require_integrator(self) -> None:
        if self.me not in self.st.integrators:
            raise Fail("only an integrator can use this command")

    def holder_is_me(self, tid: str) -> bool:
        """Whether I work on the task: I hold it, or joined whoever does."""
        return self.st.works_on(tid, self.me, time.time())
