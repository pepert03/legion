"""How a command's clone meets the network: one sync round around each command, pull and push, and the upkeep
that goes with it. The only part of the core that knows of Radicle, its node and this machine's sync process
(daemon.py): ctx, state and the commands deal in events and git, and the network commands live in netcli.py
(`legion-net`). Unix's rule, a program that does one thing: legion coordinates, and this module carries.

LEGION_SYNC=off: commands don't touch the network at all. What they record stays in the clone until
`legion netsync` or the sync process sends it, and what they read is what the clone has.
"""

import os
import sys
import time

from . import coordgit
from . import daemon
from . import live
from . import locks
from . import radicle

LOCK_WAIT = 15  # seconds a command waits for its clone's lock before reading what it has instead (see sync)
# seconds a command with events of its own waits for their push (see publish). Within the 60 s that Instinct's
# sandboxes give a command, with the fetch from the seeds before it: at 75 every claim there was cut off.
PUBLISH_WAIT = 45
SETTLED = 120  # seconds after which a sync process is taken to stay, and a command doesn't wait for its push
STARVED = 3  # where no sync process stays, a command that publishes fetches too after this many fetch intervals
LIVE_WAIT = 5  # seconds a command gives the relays to answer; they take 0.2 to 0.5
# With the relays bringing the events, a command without a sync process fetches from the Radicle network this many
# times less often: what's left for git to bring are commits, which the relays carry too (coordgit.need_commit).
LIVE_RELAX = 10
JUST_READ = 3  # seconds within which a command doesn't read from the relays twice: once as it starts
repo_lock = daemon.repo_lock


def _fetch_commit(repo, sha: str, ref: str | None, wait: float) -> bool:
    return live.enabled(repo) and live.get_commit(repo, sha, ref, wait)


coordgit.fetch_commit = _fetch_commit


def watch(repo, ident, on_new=None, serve: bool = False):
    """For a process that stays (legion run, legion wait): stay subscribed to the relays for this clone, so what
    anyone publishes is in its log within a moment and `on_new(events)` is told. None where the clone doesn't
    use them: the caller looks every so often instead. Close it when done."""
    try:
        if (os.environ.get("LEGION_SYNC", "").lower() in ("off", "0", "no") or not live.enabled(repo)
                or live.channel(repo) is None):
            return None
        return live.Watcher(repo, ident, on_new, serve)
    except Exception:  # noqa: BLE001
        return None


def refresh(repo, ident, every: float = 20.0) -> None:
    """For a hook, which runs after every step of an agent: read from the relays unless a process keeps this
    clone subscribed or something read within `every` seconds."""
    try:
        if live.enabled(repo) and not live.watched(repo) and time.time() - live.read_at(repo) >= every:
            live.pull(repo, ident, 2.5)
    except Exception:  # noqa: BLE001
        pass


def invite(c) -> str:
    """Make the project one that others join through the relays alone, and return the invitation for it. A
    project with no transport at all (a repository on this machine and nothing else) becomes one of relays only,
    with a secret of its own. Then everything it has goes out: every event whatever its age, the log whole for
    newcomers, and the main the project agreed on."""
    repo = c.repo
    if not coordgit.is_p2p(repo):
        if coordgit.has_origin(repo):
            raise ValueError("this project syncs through its origin: whoever can clone that joins with its URL")
        coordgit.git(repo, "config", "legion.transport", "relay")
        if not live._cfg(repo, "legion.livekey"):
            coordgit.git(repo, "config", "legion.livekey", os.urandom(32).hex())
    if not live.enabled(repo):
        raise ValueError("the relays are off for this clone (LEGION_LIVE, or git config legion.live)")
    known = live.sent(repo)
    events = [e for e in coordgit.read_events(repo) if e.get("id") not in known]
    for i in range(0, len(events), 200):
        if not live.push(repo, events[i:i + 200], 20):
            raise ValueError("no relay took the project's events: try again in a moment (`legion relay` says "
                             "which answer)")
    live.put_log(repo)
    tip = c.st.main_tip
    if tip and coordgit.has_commit(repo, tip) and not live.on_relays(repo, tip):
        live.put_commit(repo, tip, [])
    return live.invitation(repo, c.st)


def read_invitation(token: str) -> dict:
    return live.read_invitation(token)


def join(dest, info: dict) -> None:
    """A new clone for an invitation: a repository with no remote, told to use the relays and nothing else."""
    p = coordgit.run(dest.parent, "init", "-q", "-b", "main", str(dest))
    if p.returncode:
        raise coordgit.GitError(p.stderr.decode("utf-8", "replace").strip())
    for setting in coordgit.CLONE_CONFIG[1::2]:  # what a clone is made with
        coordgit.git(dest, "config", *setting.split("=", 1))
    coordgit.git(dest, "config", "legion.transport", "relay")
    coordgit.git(dest, "config", "legion.livekey", info["key"])
    if info.get("relays"):
        coordgit.git(dest, "config", "legion.relays", " ".join(info["relays"]))


def first_read(repo, ident) -> int:
    """A newcomer's first read: the latest copy of the log, then what the relays hold besides. How many events."""
    n = len(live.get_log(repo, ident))
    new, answered = live.pull(repo, ident, 15, deep=n == 0)
    if not answered and not n:
        raise coordgit.GitError("no relay answered: check the network (`legion relay`) and join again")
    return n + len(new)


def relay_only(repo) -> None:
    """This clone leaves Radicle: the relays carry its events and the commits they name, and no node runs here."""
    coordgit.git(repo, "config", "legion.transport", "relay")


def relay_status(repo) -> list[str]:
    from . import nostr
    if not live.enabled(repo):
        return ["The relays are off for this clone: `git config legion.live true` turns them on (or it uses "
                "neither Radicle nor relays: `legion invite` makes it a project of relays)."]
    ch = live.channel(repo)
    lines = ["This clone sends its events to the relays and reads the others' from them"
             + (" (and nothing else: no Radicle node)." if coordgit.is_relay(repo) else "; git (Radicle) keeps the archive.")]
    for url in live.all_relays(repo):
        t0 = time.time()
        _, answered = nostr.query([url], [{"kinds": [live.KIND], "#t": [ch.tag if ch else "x"], "limit": 1}], 5, grace=5)
        lines.append(f"  {url}: " + (f"answers in {1000 * (time.time() - t0):.0f} ms" if answered else "no answer"))
    last = live.read_at(repo)
    lines.append("A process stays subscribed for this clone: what anyone sends is here within a moment."
                 if live.watched(repo) else
                 "No process stays subscribed here: each command reads from the relays as it starts"
                 + (f" (last read {time.time() - last:.0f} s ago)." if last else "."))
    if live.own_pending(repo):
        lines.append("Events recorded here haven't gone out yet: the next command sends them.")
    return lines


def live_round(c) -> tuple[bool, bool]:
    """The relays' part of a command's round (see live.py): read what was published since the last read, unless
    a process that stays subscribed keeps this clone up to date, and send what this clone recorded and hasn't
    sent, the commits it names first. (Whether a relay answered the read, whether this clone's events are out.)
    Both take a fraction of a second, and neither needs a node, a seed coming to fetch, or a process left behind."""
    read = out = False
    try:
        if not live.reachable(c.repo):
            return False, False  # none answered a moment ago: git carries, and the relays are tried again later
        t0 = time.time()
        if live.watched(c.repo) or time.time() - getattr(c, "live_read", 0) < JUST_READ:
            read = True
        else:
            _, read = live.pull(c.repo, c.ident, LIVE_WAIT)
            if read:
                c.live_read = time.time()
            timed("reading from the relays", t0)
        if live.own_pending(c.repo):
            t0 = time.time()
            own = coordgit.rev(c.repo, coordgit.OWN)
            st = getattr(c, "st", None)
            events = st.events + [ev for ev, _ in st.ignored] if st else coordgit.read_events(c.repo)
            mine = live.outbox(c.repo, events, c.me)
            if mine:
                live.offer(c.repo, st, mine)
                out = live.push(c.repo, mine, LIVE_WAIT + 1)
            else:
                out = True
            if out:
                live.own_sent(c.repo, own)
            timed("sending to the relays" + ("" if out else " (no relay took it)"), t0)
        else:
            out = True
    except Exception as e:  # noqa: BLE001 (the relays' trouble mustn't stop a command: git still carries)
        print(f"legion: warning: the relays couldn't be used this time ({str(e).strip()[-200:]})", file=sys.stderr)
    return read, out


def timed(what: str, since: float) -> None:
    """With LEGION_TIMING set, how long a step of the sync round took, on stderr: Instinct's agents measured
    claims of 60 to 120 s from the outside, and nothing said which step they went on."""
    if os.environ.get("LEGION_TIMING"):
        print(f"legion: timing: {what} {time.time() - since:.1f} s", file=sys.stderr, flush=True)


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
    on_radicle, relays = coordgit.is_radicle(c.repo), live.enabled(c.repo)
    read = relayed = False
    if relays:
        had = bool(getattr(c, "unpublished", None))
        read, out = live_round(c)
        relayed = had and out
        if relayed:
            print(f"legion: sent {', '.join(c.unpublished)} (the relays have it: the others see it now).",
                  file=sys.stderr)
        elif had and not on_radicle:
            print(f"legion: warning: no relay took {', '.join(c.unpublished)}; it stays in this clone and goes "
                  "out with your next command", file=sys.stderr)
        if (on_radicle and read and out and not c.strict and os.environ.get("LEGION_RADICLE", "").lower() != "always"
                and not daemon.alive() and daemon.short_lived()):
            # A sandbox: no process outlives a command here, so its Radicle node never stayed up to be fetched
            # from, and a node killed mid-write left locks, packs marked as kept and a forked chain of signed refs
            # (what Erdős #902's sandboxes spent their days on). With the relays carrying its events and the
            # commits they name, this command leaves the node alone: a node that stays (see live.Bridge) puts
            # what the relays bring into git for everyone.
            on_radicle = False
        if not on_radicle:
            c.unpublished = []
            if not c.strict and coordgit.is_relay(c.repo):
                daemon.serving(c.repo)  # where processes stay, one keeps this clone subscribed
    served = (not c.strict and on_radicle and daemon.serving(c.repo)
              and daemon.age() >= SETTLED)
    if served:
        if getattr(c, "unpublished", None):
            daemon.wake()
            if not relayed:
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
            every = radicle.SANDBOX_FETCH_EVERY * (LIVE_RELAX if read else 1)
            if (on_radicle and not getattr(c, "unpublished", None) and radicle.fetch_due(c.repo, every)
                    and not (daemon.alive() and daemon.age() >= SETTLED)):  # no sync process stays: fetch, waiting
                print("legion: fetching from the Radicle network (no sync process stays on this machine)",
                      file=sys.stderr, flush=True)
                t0 = time.time()
                try:
                    radicle.fetch_inline(c.repo)
                except radicle.RadError:
                    pass
                timed("fetch from the network", t0)
            t0 = time.time()
            with locks.held(daemon.repo_lock(c.repo), wait=wait) as got:
                timed("waiting for the clone's lock", t0)
                t0 = time.time()
                if relays and not on_radicle:
                    c.unreachable = []  # nothing comes by git here: the relays brought it above
                elif got or wait > LOCK_WAIT:
                    c.unreachable = coordgit.sync(c.repo, c.ident, publish=False)
                    timed("sync with the node's storage", t0)
                else:
                    print("legion: this clone is being synced by another process right now: reading what it "
                          "has", file=sys.stderr)
            done = True
            if on_radicle:
                t0 = time.time()
                # Its events already with the relays, the command doesn't wait for the push to the node: that
                # one is for the archive, and goes on in its own process, or with the next command.
                done = c._publish(wait=bool(getattr(c, "unpublished", None)) and not relayed)
                timed("publish" + (" (out)" if done else " (not out yet)"), t0)
                daemon.clear_stale_locks(c.repo, lambda m: print(f"legion: {m}", file=sys.stderr))
                if (done and getattr(c, "unpublished", None) and not (daemon.alive() and daemon.age() >= SETTLED)
                        and radicle.fetch_due(c.repo, radicle.SANDBOX_FETCH_EVERY * STARVED * (LIVE_RELAX if read else 1))):
                    # An agent that records something with every command never fetched above, and read nothing
                    # from the network at all: once its events are out, a fetch now and then, for the next command.
                    try:
                        radicle.fetch_inline(c.repo)
                    except radicle.RadError:
                        pass
            if getattr(c, "unpublished", None):
                if not relayed:
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
        # (with relays, a main this clone lacks comes from them now: see coordgit.need_commit)
        if coordgit.update_main(c.repo, c.st.main_tip, fetch=relays) and on_radicle:
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
        elif relays and not read and not on_radicle:
            print("legion: warning: no relay answered; working with what this clone has", file=sys.stderr)
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
