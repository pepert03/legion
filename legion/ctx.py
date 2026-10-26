"""Shared context for the commands: repository, identity and state."""

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


class Ctx:
    def __init__(self, sync: bool = True):
        self.repo = repo_root()
        self.ident = need_identity(self.repo)
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

    def sync(self) -> None:
        self.unreachable = coordgit.sync(self.repo, self.ident)
        self.reload()
        if coordgit.is_p2p(self.repo):
            coordgit.update_main(self.repo, self.st.main_tip)
            coordgit.prune_patches(self.repo, {p.branch for p in self.st.patches.values() if p.verdict})
            peers = coordgit.peers(self.repo)
            if "network" in self.unreachable:
                print("legion: warning: couldn't reach the Radicle network; working with what the local "
                      "storage has", file=sys.stderr)
            elif peers and len(self.unreachable) == len(peers):
                print(f"legion: warning: couldn't reach any peer ({', '.join(peers)}); "
                      "working with what you have locally", file=sys.stderr)

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
        h = self.st.holder(tid, time.time())
        return bool(h and h.author == self.me)
