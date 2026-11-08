"""How a command's clone meets the network: one sync round around each command, pull and push, and the upkeep
that goes with it. The only part of the core that knows of Radicle, its node and this machine's sync process
(daemon.py): ctx, state and the commands deal in events and git, and the network commands live in netcli.py
(`legion-net`). Unix's rule, a program that does one thing: legion coordinates, and this module carries.

LEGION_SYNC=off: commands don't touch the network at all. What they record stays in the clone until
`legion netsync` or the sync process sends it, and what they read is what the clone has.
"""

import os
import sys

from . import coordgit
from . import daemon
from . import locks
from . import radicle

LOCK_WAIT = 15  # seconds a command waits for its clone's lock before reading what it has instead (see sync)
PUBLISH_WAIT = 75  # seconds a command with events of its own waits for their push (see publish)
SETTLED = 120  # seconds after which a sync process is taken to stay, and a command doesn't wait for its push
STARVED = 3  # where no sync process stays, a command that publishes fetches too after this many fetch intervals
repo_lock = daemon.repo_lock


def keep_fetching(repo) -> None:
    """For a reader that stays open (the viewer): the network fetch a command would start, in a process of its
    own and once a minute at most (see radicle.kick_network_sync), and with it the node, started if it stopped.
    Without it the viewer showed what the node held when it last ran: after a reboot, nothing new for hours."""
    if os.environ.get("LEGION_SYNC", "").lower() in ("off", "0", "no") or not coordgit.is_radicle(repo):
        return
    try:
        radicle.kick_network_sync(repo)
    except (radicle.RadError, OSError):
        pass


def sync(c) -> None:
    if os.environ.get("LEGION_SYNC", "").lower() in ("off", "0", "no"):
        if c.unpublished:
            print(f"legion: {', '.join(c.unpublished)} stay in this clone (LEGION_SYNC=off): `legion netsync` "
                  "sends them.", file=sys.stderr)
            c.unpublished = []
        c.reload()
        return
    _sync(c)


def _sync(c) -> None:
    # With this machine's sync process running (see daemon.py), it syncs: the command only reads what it
    # brought, and wakes it when there is something to publish. The integrator always syncs by itself.
    # Not by one that keeps being started anew (younger than SETTLED): it lives where sandboxes kill background
    # processes, its rounds there outlast the command (a push costs 40 s of CPU), and what it was to push died
    # with it. The command pushes by itself then, and waits (see publish).
    served = (not c.strict and coordgit.is_radicle(c.repo) and daemon.serving(c.repo)
              and daemon.age() >= SETTLED)
    if served:
        if getattr(c, "unpublished", None):
            daemon.wake()
            print(f"legion: {', '.join(c.unpublished)} go out within seconds (this machine's sync process "
                  "publishes them).", file=sys.stderr)
            c.unpublished = []
        c.unreachable = [] if radicle.netsync_status(c.repo).get("ok", True) else ["network"]
    else:
        try:
            # Not long for the lock: a sync process of an older legion, or one stuck, held it over pushes of
            # minutes, and commands waited two minutes for it, past their tools' limit. Busy, the clone is
            # being synced already: the command reads what it has.
            wait = 120 if c.strict or not coordgit.is_radicle(c.repo) else LOCK_WAIT
            if (coordgit.is_radicle(c.repo) and not getattr(c, "unpublished", None) and radicle.fetch_due(c.repo)
                    and not (daemon.alive() and daemon.age() >= SETTLED)):  # no sync process stays: fetch, waiting
                print("legion: fetching from the Radicle network (no sync process stays on this machine)",
                      file=sys.stderr, flush=True)
                try:
                    radicle.fetch_inline(c.repo)
                except radicle.RadError:
                    pass
            with locks.held(daemon.repo_lock(c.repo), wait=wait) as got:
                if got or wait > LOCK_WAIT:
                    c.unreachable = coordgit.sync(c.repo, c.ident, publish=False)
                else:
                    print("legion: this clone is being synced by another process right now: reading what it "
                          "has", file=sys.stderr)
            done = True
            if coordgit.is_radicle(c.repo):
                done = c._publish(wait=bool(getattr(c, "unpublished", None)))
                daemon.clear_stale_locks(c.repo, lambda m: print(f"legion: {m}", file=sys.stderr))
                if (done and getattr(c, "unpublished", None) and not (daemon.alive() and daemon.age() >= SETTLED)
                        and radicle.fetch_due(c.repo, radicle.SANDBOX_FETCH_EVERY * STARVED)):
                    # An agent that records something with every command never fetched above, and read nothing
                    # from the network at all: once its events are out, a fetch now and then, for the next command.
                    try:
                        radicle.fetch_inline(c.repo)
                    except radicle.RadError:
                        pass
            if getattr(c, "unpublished", None):
                print(f"legion: published {', '.join(c.unpublished)}." if done else
                      f"legion: {', '.join(c.unpublished)} go out within seconds (a process of its own "
                      "publishes them).", file=sys.stderr)
                c.unpublished = []
        except coordgit.GitError as e:
            c._sync_failed(e)
    c.reload()
    if coordgit.is_p2p(c.repo):
        # The push went out before the state said where main is now: when that moves what this node offers
        # as main (it published a main, or finalized a patch), push again, or the commit would wait for
        # this node's next command, and a founder's `main publish` may have none after it.
        if coordgit.update_main(c.repo, c.st.main_tip) and coordgit.is_radicle(c.repo):
            if served:
                daemon.wake()
            else:
                try:
                    c._publish(wait=True)
                except coordgit.GitError as e:
                    c._sync_failed(e)
        coordgit.prune_patches(c.repo, {p.branch for p in c.st.patches.values() if p.verdict}
                               | {w["branch"] for tid, ws in c.st.saved.items() if c.st.done(tid) for w in ws}
                               | coordgit.superseded(c.st.superseded_saves()))
        peers = coordgit.peers(c.repo)
        if "network" in c.unreachable:
            print("legion: warning: the last fetch from the Radicle network (it runs in the background) "
                  "couldn't reach it; working with what this node has, which its peers keep sending", file=sys.stderr)
        elif peers and len(c.unreachable) == len(peers):
            print(f"legion: warning: couldn't reach any peer ({', '.join(peers)}); "
                  "working with what you have locally", file=sys.stderr)


def publish(c, wait: bool = False) -> bool:
    """With no sync process to push for it, the integrator pushes itself; any other command leaves the push
    to a process of its own (see radicle.kick_publish), and with something of its own to publish (`wait`)
    waits up to PUBLISH_WAIT for it: where a sandbox kills what runs in the background once the command
    returns, the push died with it, and nothing an agent recorded went out. True if it's out."""
    if c.strict:
        coordgit.publish_radicle(c.repo, c.ident)
        return True
    radicle.kick_publish(c.repo)
    return wait and radicle.wait_published(c.repo, PUBLISH_WAIT)


def sync_failed(c, e: Exception) -> None:
    if c.strict:
        raise e
    # A slow network, or another process on the same Radicle node: carry on with the local state, and
    # what this command records goes out with the next sync. An agent mustn't stop over it.
    print(f"legion: warning: couldn't sync ({str(e).strip()[-200:]}); working with the local state, "
          "and your events go out on the next command", file=sys.stderr)
