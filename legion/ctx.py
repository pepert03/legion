"""Shared context for the commands: repository, identity and state."""

import os
import subprocess
import sys
import time
from pathlib import Path

from . import coordgit
from . import events as E
from . import state as S
from .identity import Identity


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


def remember_agent(repo) -> None:
    """Which agent and model work in this repository: kept in its git config, so the agent only has to say it
    once (at join), not in every shell. A variable set in the shell wins, and is remembered for next time."""
    for var, key in AGENT_VARS:
        saved = subprocess.run(["git", "config", "--get", key], cwd=repo, capture_output=True, text=True).stdout.strip()
        value = os.environ.get(var) or (E.detect_agent() if var == "LEGION_AGENT" else None)
        if value and value != saved:
            subprocess.run(["git", "config", key, value], cwd=repo, capture_output=True)
        elif saved and not os.environ.get(var):
            os.environ[var] = saved


class Ctx:
    def __init__(self, sync: bool = True):
        self.repo = repo_root()
        self.ident = need_identity(self.repo)
        remember_agent(self.repo)
        self.unreachable: list[str] = []
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
        try:
            self.unreachable = coordgit.sync(self.repo, self.ident)
        except coordgit.GitError as e:
            self._sync_failed(e)
        self.reload()
        if coordgit.is_p2p(self.repo):
            # The push went out before the state said where main is now: when that moves what this node offers
            # as main (it published a main, or finalized a patch), push again, or the commit would wait for
            # this node's next command, and a founder's `main publish` may have none after it.
            if coordgit.update_main(self.repo, self.st.main_tip) and coordgit.is_radicle(self.repo):
                try:
                    coordgit.publish_radicle(self.repo, self.ident)
                except coordgit.GitError as e:
                    self._sync_failed(e)
            coordgit.prune_patches(self.repo, {p.branch for p in self.st.patches.values() if p.verdict}
                                   | {w["branch"] for tid, ws in self.st.saved.items() if self.st.done(tid) for w in ws})
            peers = coordgit.peers(self.repo)
            if "network" in self.unreachable:
                print("legion: warning: couldn't reach the Radicle network; working with what the local "
                      "storage has", file=sys.stderr)
            elif peers and len(self.unreachable) == len(peers):
                print(f"legion: warning: couldn't reach any peer ({', '.join(peers)}); "
                      "working with what you have locally", file=sys.stderr)

    def _sync_failed(self, e: Exception) -> None:
        if self.strict:
            raise e
        # A slow network, or another process on the same Radicle node: carry on with the local state, and
        # what this command records goes out with the next sync. An agent mustn't stop over it.
        print(f"legion: warning: couldn't sync ({str(e).strip()[-200:]}); working with the local state, "
              "and your events go out on the next command", file=sys.stderr)

    def emit(self, type: str, **payload) -> dict:
        """Validate the event against the current state and, if valid, append it to the local coord."""
        ev = E.create(self.ident, type, self.st.max_lamport + 1, **payload)
        trial = coordgit.state(self.repo, extra=[ev])
        err = next((e for x, e in trial.ignored if x.get("id") == ev["id"]), None)
        if err:
            raise Fail(f"event rejected: {err}")
        coordgit.append(self.repo, self.ident, ev)
        self.st = trial
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
