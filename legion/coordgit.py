"""The `coord` branch: an events/ directory that files are only ever added to.

It is written with low-level git commands, without a checkout, so it never touches the agent's
working directory. Since every event is a file with a unique name, merging two copies is the
union of their files: there are never conflicts.
"""

import json
import os
import pickle
import random
import subprocess
import tempfile
import time
from pathlib import Path

from . import events as E
from .identity import Identity

REF = "refs/heads/coord"
TRACK = "refs/remotes/origin/coord"
ZERO = "0" * 40
# Set at clone time: changed after checkout, git sees phantom line-ending changes.
# longpaths avoids failures on Windows with paths longer than 260 characters.
CLONE_CONFIG = ["-c", "core.autocrlf=false", "-c", "core.longpaths=true"]


class GitError(RuntimeError):
    pass


def run(repo, *args, input: bytes | None = None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, input=input, capture_output=True, env=env)


def git(repo, *args, input: bytes | None = None, env=None, binary=False):
    p = run(repo, *args, input=input, env=env)
    if p.returncode:
        raise GitError(f"git {' '.join(args)}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace").strip()


def author_env(ident: Identity) -> dict:
    env = dict(os.environ)
    email = f"{ident.short}@legion.local"
    env.update(GIT_AUTHOR_NAME=ident.name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=ident.name, GIT_COMMITTER_EMAIL=email)
    return env


def rev(repo, ref: str) -> str | None:
    p = run(repo, "rev-parse", "--verify", "-q", ref + "^{commit}")
    if p.returncode:
        return None
    return p.stdout.decode().strip() or None


def is_ancestor(repo, a: str, b: str) -> bool:
    return run(repo, "merge-base", "--is-ancestor", a, b).returncode == 0


def list_files(repo, commit: str | None) -> dict[str, str]:
    if not commit:
        return {}
    files = {}
    for line in git(repo, "ls-tree", "-r", commit).splitlines():
        meta, path = line.split("\t", 1)
        if path.startswith("events/"):
            files[path[len("events/"):]] = meta.split()[2]
    return files


def read_blobs(repo, blobs: list[str]) -> dict[str, dict | None]:
    """The events stored in these blobs (None for a file that isn't valid JSON), in one git call."""
    if not blobs:
        return {}
    data = git(repo, "cat-file", "--batch", input=("\n".join(blobs) + "\n").encode(), binary=True)
    out, pos = {}, 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        sha, _, size = data[pos:nl].decode().split()
        size = int(size)
        try:
            out[sha] = json.loads(data[nl + 1:nl + 1 + size])
        except ValueError:
            out[sha] = None  # a corrupt file is ignored; the rest of the log still counts
        pos = nl + 1 + size + 1
    return out


# --- local cache -------------------------------------------------------------------------------
#
# Every command rebuilds the state from the whole log, and checking the events' signatures is nearly all
# of that cost. The cache, kept in the repository's .git, remembers which files have been read and which
# have passed the check, by their git blob id: a blob id names the exact bytes, so an event that was
# tampered with is a different blob and is checked again. When the log grows, only the new files are read.

class _Store:
    """An append-only file of pickled frames ({blob: event} or {blob, ...}), read incrementally: each process
    reads what other processes appended since it last looked, and writes only what's new. A torn or corrupt
    frame (two processes appending at once, say) throws the file away: it's only a cache."""

    def __init__(self, path: Path, empty):
        self.path, self.empty, self.data, self.offset = path, empty, empty(), 0

    def refresh(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size < self.offset:  # replaced or truncated: start again
            self.data, self.offset = self.empty(), 0
        if size == self.offset:
            return self.data
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                while f.tell() < size:
                    self.data.update(pickle.load(f))
                self.offset = f.tell()
        except (OSError, EOFError, ValueError, pickle.UnpicklingError, AttributeError, TypeError):
            self.data, self.offset = self.empty(), 0
            self.path.unlink(missing_ok=True)
        return self.data

    def add(self, new) -> None:
        if not new:
            return
        self.data.update(new)
        frame = pickle.dumps(new, protocol=pickle.HIGHEST_PROTOCOL)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            try:
                os.write(fd, frame)
            finally:
                os.close(fd)
            if self.offset + len(frame) == self.path.stat().st_size:
                self.offset += len(frame)  # nobody else appended in between: no need to read our own frame
        except OSError:
            pass


_STORES: dict[str, tuple[_Store, _Store]] = {}


def _stores(repo) -> tuple[_Store, _Store]:
    """(events read so far, blobs whose signature checked out) for this repository."""
    key = str(Path(repo).resolve())
    if key not in _STORES:
        d = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        _STORES[key] = (_Store(d / "legion-events.cache", dict), _Store(d / "legion-verified.cache", set))
    events, verified = _STORES[key]
    return events, verified


def read_pairs(repo, ref: str = REF) -> list[tuple[str, dict]]:
    """(blob id, event) for every event on `ref`, reading only the files the cache hasn't seen."""
    files = list_files(repo, rev(repo, ref))
    if not files:
        return []
    store, _ = _stores(repo)
    events = store.refresh()
    missing = [b for b in set(files.values()) if b not in events]
    if missing:
        store.add(read_blobs(repo, missing))
    return [(b, events[b]) for b in files.values() if events.get(b) is not None]


def read_events(repo, ref: str = REF) -> list[dict]:
    return [ev for _, ev in read_pairs(repo, ref)]


def build_state(repo, pairs: list[tuple[str, dict]], extra: list[dict] = ()):
    """The state from these events, skipping the signature check of those already checked before."""
    from . import state as S
    _, store = _stores(repo)
    verified = store.refresh()
    trusted = {id(ev) for b, ev in pairs if b in verified}
    st = S.build([ev for _, ev in pairs] + list(extra), trusted)
    bad = {id(ev) for ev, _ in st.ignored}
    # Ignored for breaking a rule of the state isn't the same as a bad signature: only remember the latter.
    store.add({b for b, ev in pairs if b not in verified and (id(ev) not in bad or E.check(ev) is None)})
    return st


# The state, kept between commands. Every command rebuilt it from every event (20,000 in Erdős #902: half a
# second reading and folding them, twice for a command that records one), and a project's every command grew
# slower with its history. Kept per coord commit, with the events it holds: the same commit reads it back in a
# fraction of that; a later one applies only its new events, when they all come after the last it applied (the
# usual case: new events carry later clocks); anything else (an event from a node long cut off, ordered among
# the old ones) is rebuilt whole, as before. Each caller gets a copy of its own, so none can change the kept one.
STATE_CACHE = "legion-state.cache"
_STATE: dict[str, tuple] = {}  # repo -> (stamp, head, blobs, last key, pickled state)


def _code_stamp() -> float:
    here = Path(__file__).parent
    return max((p.stat().st_mtime for p in here.glob("*.py")), default=0.0)


def _state_file(repo) -> Path:
    return common_dir(repo) / STATE_CACHE


def _kept(repo, key: str):
    kept = _STATE.get(key)
    if kept is None:
        try:
            kept = pickle.loads(_state_file(repo).read_bytes())
        except (OSError, pickle.PickleError, EOFError, ValueError, TypeError, AttributeError, ImportError):
            kept = None
    return kept


def _keep(repo, key: str, kept: tuple) -> None:
    _STATE[key] = kept
    path = _state_file(repo)
    tmp = path.with_name(f"{STATE_CACHE}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(pickle.dumps(kept, protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(tmp, path)
    except OSError:  # another process holds it open (Windows): this one keeps it in memory only
        tmp.unlink(missing_ok=True)


def state(repo, ref: str = REF, extra: list[dict] = ()):
    from . import state as S
    if ref != REF:
        return build_state(repo, read_pairs(repo, ref), extra)
    key, stamp, head = str(Path(repo).resolve()), _code_stamp(), rev(repo, REF)
    kept = _kept(repo, key)
    if kept is None or kept[0] != stamp or kept[1] != head:
        files = list_files(repo, head)
        blobs = frozenset(files.values())
        built = None
        if kept is not None and kept[0] == stamp and kept[2] <= blobs:
            store, _ = _stores(repo)
            events = store.refresh()
            missing = [b for b in blobs - kept[2] if b not in events]
            if missing:
                store.add(read_blobs(repo, missing))
            new = sorted(((b, events[b]) for b in blobs - kept[2] if events.get(b) is not None),
                         key=lambda pair: S.event_key(pair[1]))
            if not new or S.event_key(new[0][1]) > kept[3]:
                built = _extended(repo, pickle.loads(kept[4]), new)
                last = S.event_key(new[-1][1]) if new else kept[3]
        if built is None:
            pairs = read_pairs(repo, REF)
            built = build_state(repo, pairs)
            last = max((S.event_key(ev) for _, ev in pairs), default=(0, ""))
        kept = (stamp, head, blobs, last, pickle.dumps(built, protocol=pickle.HIGHEST_PROTOCOL))
        _keep(repo, key, kept)
    st = pickle.loads(kept[4])
    if extra:
        extra = sorted(extra, key=S.event_key)
        if S.event_key(extra[0]) > kept[3]:
            return S.extend(st, list(extra))
        return build_state(repo, read_pairs(repo, REF), extra)
    return st


def _extended(repo, st, pairs: list[tuple[str, dict]]):
    """`st` with these new events applied, checking the signatures not checked before (as build_state does)."""
    from . import state as S
    _, store = _stores(repo)
    verified = store.refresh()
    S.extend(st, [ev for _, ev in pairs], {id(ev) for b, ev in pairs if b in verified})
    bad = {id(ev) for ev, _ in st.ignored}
    store.add({b for b, ev in pairs if b not in verified and (id(ev) not in bad or E.check(ev) is None)})
    return st


def _commit(repo, files: dict[str, str], parents: list[str], message: str, env) -> str:
    lines = "".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(files.items()))
    events_tree = git(repo, "mktree", input=lines.encode())
    root = git(repo, "mktree", input=f"040000 tree {events_tree}\tevents\n".encode())
    args = ["commit-tree", root]
    for p in parents:
        args += ["-p", p]
    return git(repo, *args, "-m", message, env=env)


def add_events(repo, ident: Identity, events: list[dict]) -> int:
    """Events that didn't come by git (the relays brought them: see live.py), into this clone's log, in one
    commit. How many it didn't have."""
    old = rev(repo, REF)
    files = list_files(repo, old)
    new = [ev for ev in events if E.filename(ev) not in files]
    if not new:
        return 0
    if len(new) <= 3:
        blobs = [git(repo, "hash-object", "-w", "--stdin", input=E.encode(ev)) for ev in new]
    else:  # one git call for all of them
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, ev in enumerate(new):
                paths.append(str(Path(tmp) / str(i)))
                Path(paths[-1]).write_bytes(E.encode(ev))
            listed = "".join(p + os.linesep for p in paths)
            blobs = git(repo, "hash-object", "-w", "--stdin-paths", input=listed.encode()).split()
    files.update({E.filename(ev): blob for ev, blob in zip(new, blobs)})
    commit = _commit(repo, files, [old] if old else [], f"{len(new)} events from the relays", author_env(ident))
    git(repo, "update-ref", REF, commit, old or ZERO)
    git(repo, "update-ref", PUB + "coord", commit)
    return len(new)


def take_tree(repo, ident: Identity, commit: str, ok) -> list[dict]:
    """The events of another copy of the log (a commit holding events/, already in this repository) that this
    clone lacks and `ok(event)` accepts, into its log in one commit: their blobs are here, nothing is written
    again. The events it took."""
    old = rev(repo, REF)
    mine = list_files(repo, old)
    missing = {name: blob for name, blob in list_files(repo, commit).items() if name not in mine}
    events = read_blobs(repo, sorted(set(missing.values())))
    good = {name: blob for name, blob in missing.items()
            if isinstance(events.get(blob), dict) and ok(events[blob]) and E.filename(events[blob]) == name}
    if good:
        new = _commit(repo, {**mine, **good}, [old] if old else [], f"{len(good)} events from a copy of the log",
                      author_env(ident))
        git(repo, "update-ref", REF, new, old or ZERO)
        git(repo, "update-ref", PUB + "coord", new)
    return [events[blob] for blob in good.values()]


def append(repo, ident: Identity, ev: dict) -> None:
    blob = git(repo, "hash-object", "-w", "--stdin", input=E.encode(ev))
    old = rev(repo, REF)
    files = list_files(repo, old)
    files[E.filename(ev)] = blob
    new = _commit(repo, files, [old] if old else [], f"{ev['type']} {ev.get('task', '')}".strip(), author_env(ident))
    # The three refs in one git call (OWN: this node's own latest event, what makes a publish worth its time).
    git(repo, "update-ref", "--stdin",
        input=f"update {REF} {new} {old or ZERO}\nupdate {PUB}coord {new}\nupdate {OWN} {new}\n".encode())


OWN = "refs/legion/own"


# --- p2p mode ------------------------------------------------------------------------------
#
# No origin: every node only writes to its own repository and reads from its peers (git fetch).
# What a node offers others lives in refs/legion/pub/* and refs/legion/patches/*, so whatever it
# fetches from one peer is exposed again and reaches, by gossip, nodes that aren't directly connected.

PUB = "refs/legion/pub/"
PEERS = "refs/legion/peers/"
PATCHES = "refs/legion/patches/"
MAIN = "refs/legion/main"  # `git checkout legion/main` resolves to it
FETCH_TIMEOUT = 30


_dirs: dict[str, Path] = {}
_settings: dict[tuple, tuple] = {}


def common_dir(repo) -> Path:
    """The repository's .git (the one its worktrees share), asked of git once per process."""
    key = str(repo)
    if key not in _dirs:
        _dirs[key] = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return _dirs[key]


def setting(repo, key: str) -> list[str]:
    """The values of a git setting of this clone. Asked of git again only when the clone's config file changes:
    how a clone is carried (legion.transport) was asked a dozen times a command, a process each, which on
    Windows was a third of a second of every command."""
    try:
        s = (common_dir(repo) / "config").stat()
        stamp = (s.st_mtime_ns, s.st_size)
    except (OSError, GitError):
        stamp = None
    kept = _settings.get((str(repo), key))
    if kept and stamp is not None and kept[0] == stamp:
        return kept[1]
    p = run(repo, "config", "--get-all", key)
    values = p.stdout.decode().split() if p.returncode == 0 else []
    _settings[(str(repo), key)] = (stamp, values)
    return values


def is_radicle(repo) -> bool:
    return setting(repo, "legion.transport")[-1:] == ["radicle"]


def is_relay(repo) -> bool:
    """A clone whose only transport is the relays (live.py): no Radicle node, no remote. Its refs are laid out as
    in p2p, and the commits its events name come from the relays (see need_commit)."""
    return setting(repo, "legion.transport")[-1:] == ["relay"]


def peers(repo) -> list[str]:
    if is_radicle(repo):  # with Radicle, the peers are the publishers already copied from its storage
        out = run(repo, "for-each-ref", "--format=%(refname)", PEERS).stdout.decode()
        return sorted({line[len(PEERS):].split("/", 1)[0] for line in out.splitlines() if line.endswith("/coord")})
    return list(setting(repo, "legion.peer"))


def is_p2p(repo) -> bool:
    return is_radicle(repo) or is_relay(repo) or bool(peers(repo))


def add_peer(repo, name: str, url: str) -> None:
    if name in git(repo, "remote").split():
        git(repo, "remote", "set-url", name, url)
    else:
        git(repo, "remote", "add", name, url)
    if name not in peers(repo):
        git(repo, "config", "--add", "legion.peer", name)


def remove_peer(repo, name: str) -> None:
    run(repo, "config", "--unset", "legion.peer", f"^{name}$")
    run(repo, "remote", "remove", name)
    for ref in git(repo, "for-each-ref", "--format=%(refname)", f"{PEERS}{name}/").splitlines():
        run(repo, "update-ref", "-d", ref)


def sync_peers(repo, ident: Identity) -> list[str]:
    """Fetch each peer's coord, patches and main, and merge the coords. Returns the peers that failed."""
    failed = []
    for peer in peers(repo):
        try:
            p = subprocess.run(["git", "fetch", "-q", "--no-tags", peer,
                                f"+{PUB}*:{PEERS}{peer}/*", f"+{PATCHES}*:{PATCHES}*"],
                               cwd=repo, capture_output=True, timeout=FETCH_TIMEOUT)
            if p.returncode:
                failed.append(peer)
        except subprocess.TimeoutExpired:
            failed.append(peer)  # a peer that is off or unreachable doesn't block the rest
    merge_coords(repo, ident, peers(repo))
    return failed


def event_trees(repo, commits: list[str]) -> dict[str, dict[str, str]]:
    """{commit: {event file: blob}} for several commits, reading all their trees in one git call."""
    if not commits:
        return {}
    data = run(repo, "cat-file", "--batch", input="".join(f"{c}:events\n" for c in commits).encode()).stdout
    out, pos = {}, 0
    for c in commits:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        if len(header) < 3 or header[1] != b"tree":  # "missing": a commit with no events/ yet
            out[c], pos = {}, nl + 1
            continue
        size = int(header[2])
        body, files, i = data[nl + 1:nl + 1 + size], {}, 0
        while i < len(body):  # a tree's entries: "<mode> <name>\0<20-byte id>"
            sp, nul = body.index(b" ", i), body.index(b"\0", i)
            files[body[sp + 1:nul].decode()] = body[nul + 1:nul + 21].hex()
            i = nul + 21
        out[c], pos = files, nl + 1 + size + 1
    return out


def merge_coords(repo, ident: Identity, names: list[str]) -> None:
    """Union of our coord and every peer's: events are files with unique names, so it never conflicts.

    In one pass, whatever the number of peers: their tips in one git call, the trees of those that differ
    from ours in another, and at most one merge commit."""
    local = rev(repo, REF)
    wanted = {f"{PEERS}{n}/coord" for n in names}
    out = run(repo, "for-each-ref", "--format=%(objectname) %(refname)", PEERS).stdout.decode()
    tips = sorted({sha for sha, ref in (l.split(" ", 1) for l in out.splitlines()) if ref in wanted} - {local})
    cur = local
    if tips:
        trees = event_trees(repo, ([local] if local else []) + tips)
        mine = trees.get(local, {})
        union = dict(mine)
        adding = []
        for t in tips:
            new = {k: v for k, v in trees[t].items() if k not in union}
            if new:
                union.update(new)
                adding.append(t)
        if adding:
            # Just behind one peer that has everything: move to its commit instead of merging.
            whole = [t for t in adding if len(trees[t]) == len(union)]
            if whole and (local is None or is_ancestor(repo, local, whole[0])):
                cur = whole[0]
            else:
                cur = _commit(repo, union, ([local] if local else []) + adding, "merge coord", author_env(ident))
    if cur and cur != local:
        git(repo, "update-ref", REF, cur, local or ZERO)
    if cur:
        git(repo, "update-ref", PUB + "coord", cur)


def radicle_who(ident: Identity) -> str:
    """This identity's prefix among the legion refs in a Radicle namespace."""
    return ident.author.split(":", 1)[1][:16]


def sync_radicle(repo, ident: Identity, publish: bool = True) -> list[str]:
    """Fetch from the Radicle network, copy every node's legion refs, merge the coords, publish ours."""
    from . import radicle
    failed = []
    try:
        radicle.keep_bridge(repo)
        if not radicle.kick_network_sync(repo):  # in the background: see radicle.kick_network_sync
            failed.append("network")
    except radicle.RadError:
        failed.append("network")  # offline: carry on with what the local storage has
    try:
        radicle.fetch_all(repo)
        merge_coords(repo, ident, peers(repo))
    except radicle.RadError as e:
        raise GitError(str(e))
    if publish:
        publish_radicle(repo, ident)
    return failed


def publish_radicle(repo, ident: Identity) -> None:
    from . import radicle
    try:
        radicle.publish(repo, radicle_who(ident))
    except radicle.RadError as e:
        raise GitError(str(e))


def has_commit(repo, sha: str | None) -> bool:
    return bool(sha) and run(repo, "cat-file", "-e", sha + "^{commit}").returncode == 0


fetch_commit = None  # set by the transport: (repo, sha, ref, wait) -> whether it brought the commit


def need_commit(repo, sha: str | None, ref: str | None = None, wait: float = 0.0) -> bool:
    """has_commit, and when this clone lacks it, ask the transport for it: with relays, a commit an event names
    isn't here until someone needs it (a verifier its patch, the next agent the saved work)."""
    if has_commit(repo, sha):
        return True
    if not sha or fetch_commit is None:
        return False
    try:
        return bool(fetch_commit(repo, sha, ref, wait))
    except Exception:  # noqa: BLE001 (the network's trouble: the commit isn't here, as before)
        return False


def update_main(repo, tip: str | None, fetch: bool = False) -> bool:
    """Point legion/main at the main the integrator signed, if we already have that commit (`fetch`: or the
    transport can bring it now, see need_commit). True if what this node offers as main moved."""
    if not tip:
        return False
    out = run(repo, "for-each-ref", "--format=%(refname) %(objectname)", MAIN, PUB + "main").stdout.decode()
    at = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    if at.get(MAIN) == tip and at.get(PUB + "main") == tip:
        return False  # where it was: nothing to move (every command asks)
    if not (need_commit(repo, tip) if fetch else has_commit(repo, tip)):
        return False
    moved = at.get(PUB + "main") != tip
    git(repo, "update-ref", MAIN, tip)
    git(repo, "update-ref", PUB + "main", tip)
    return moved


def superseded(pairs) -> set[str]:
    """The refs of earlier saves: each author's latest save on a task is what carries over, as a round carries over
    the best and forgets the rest. They went only once the latest held their commit, and in Erdős #902 a thousand
    stayed, s-four's among them with SAT certificates of 50 to 200 MB each, copied into every node's storage."""
    return {old["branch"] for old, new in pairs if old["branch"] != new["branch"]}


def prune_patches(repo, judged: set[str]) -> None:
    from . import radicle
    radicle.forget_judged(repo, {r for r in judged if r.startswith(PATCHES)})  # first, so no sync brings them back
    for ref in git(repo, "for-each-ref", "--format=%(refname)", PATCHES).splitlines():
        if ref in judged:
            run(repo, "update-ref", "-d", ref)


# --- operations that depend on the transport -----------------------------------------------


def base_ref(repo) -> str:
    return "legion/main" if is_p2p(repo) else "origin/main"


def refresh_base(repo) -> None:
    if not is_p2p(repo):
        git(repo, "fetch", "-q", "origin", "main")
    elif not rev(repo, MAIN):
        raise GitError("you don't have the integrator's main yet: sync with a peer that has it")


def patch_branch(repo, tid: str, who: str, n: int) -> str:
    return f"{PATCHES}{tid}/{who}-{n}" if is_p2p(repo) else f"legion/{tid}/{who}-{n}"


def publish_patch(repo, branch: str, commit: str = "HEAD") -> None:
    if is_p2p(repo):
        git(repo, "update-ref", branch, commit)
    else:
        git(repo, "push", "-q", "-f", "origin", f"{commit}:refs/heads/{branch}")


def fetch_patch(repo, branch: str) -> bool:
    """In p2p the commit arrives with the sync; with origin its branch must be fetched."""
    if is_p2p(repo):
        return True
    return run(repo, "fetch", "-q", "origin", f"refs/heads/{branch}").returncode == 0


def publish_main(repo, commit: str) -> bool:
    """Make `commit` the published main. The state decides which main is official, so origin follows
    it (a forced push), even if another integrator pushed something the state then discarded."""
    update_main(repo, commit)
    if is_p2p(repo) or not has_origin(repo):  # no origin (and no peers yet): it stays local
        return True
    return run(repo, "push", "-q", "origin", f"+{commit}:refs/heads/main").returncode == 0


def drop_patch(repo, branch: str) -> None:
    # The name comes from someone else's event: only delete it inside the patch namespace.
    if is_p2p(repo):
        if branch.startswith(PATCHES):
            run(repo, "update-ref", "-d", branch)
    elif branch.startswith("legion/"):
        run(repo, "push", "-q", "origin", "--delete", branch)


def view_events(repo, fetch: bool = True) -> list[dict]:
    """Every event this node can see (its own, origin's and its peers') without modifying its coord
    branch. Useful for watching while agents work in the same repository."""
    return [ev for _, ev in view_pairs(repo, fetch)]


def view_state(repo, fetch: bool = True):
    return build_state(repo, view_pairs(repo, fetch))


def view_pairs(repo, fetch: bool = True, memo: dict | None = None) -> list[tuple[str, dict]]:
    """memo, kept by a caller that reads again and again (the viewer): each coord's commit and its events, so a
    coord whose commit hasn't moved isn't read again. The viewer of Erdős #902 read 93 of them, 20,000 events each,
    on every refresh: 15 s of its 21."""
    if fetch and is_radicle(repo):
        from . import radicle
        try:
            radicle.fetch_all(repo)  # reads the local Radicle storage; publishes nothing
        except radicle.RadError:
            pass
    elif fetch:
        if is_p2p(repo):
            for peer in peers(repo):
                try:
                    subprocess.run(["git", "fetch", "-q", "--no-tags", peer, f"+{PUB}*:{PEERS}{peer}/*"],
                                   cwd=repo, capture_output=True, timeout=FETCH_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        elif has_origin(repo):
            run(repo, "fetch", "-q", "origin", f"+{REF}:{TRACK}")
    refs = [REF, TRACK] + [f"{PEERS}{p}/coord" for p in peers(repo)]
    out = run(repo, "for-each-ref", "--format=%(refname) %(objectname)", REF, TRACK, PEERS).stdout.decode()
    heads = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    memo = {} if memo is None else memo
    for ref in set(memo) - set(refs):
        del memo[ref]
    seen, blobs = {}, set()
    for ref in refs:
        head = heads.get(ref)
        if not head:
            memo.pop(ref, None)
            continue
        if memo.get(ref, (None,))[0] != head:
            memo[ref] = (head, dict(read_pairs(repo, head)))
        held = memo[ref][1]
        for blob in held.keys() - blobs:  # most coords hold the same events: only the ones not seen yet
            seen.setdefault(held[blob].get("id"), (blob, held[blob]))
        blobs |= held.keys()
    return list(seen.values())


def remote_events(repo) -> list[dict]:
    """Events as they are on origin, publishing nothing (read-only)."""
    run(repo, "fetch", "-q", "origin", f"+{REF}:{TRACK}")
    return read_events(repo, TRACK)


def has_origin(repo) -> bool:
    return "origin" in git(repo, "remote").split()


def sync(repo, ident: Identity, attempts: int = 12, publish: bool = True) -> list[str]:
    """Merge the local coord with origin's (and publish it), or with every peer's in p2p mode. With Radicle,
    publish=False leaves the push to the caller (see daemon.sync_one)."""
    if is_radicle(repo):
        return sync_radicle(repo, ident, publish)
    if is_p2p(repo):
        return sync_peers(repo, ident)
    if not has_origin(repo):
        return []
    last_error = ""
    for i in range(attempts):
        f = run(repo, "fetch", "-q", "origin", f"+{REF}:{TRACK}")
        missing = f.returncode != 0 and b"couldn't find remote ref" in f.stderr
        if f.returncode and not missing:
            last_error = f.stderr.decode("utf-8", "replace").strip()
            time.sleep(random.uniform(0.1, 0.4) * (i + 1))
            continue
        local = rev(repo, REF)
        theirs = None if missing else rev(repo, TRACK)
        if theirs and local != theirs:
            if local is None or is_ancestor(repo, local, theirs):
                git(repo, "update-ref", REF, theirs, local or ZERO)
            elif not is_ancestor(repo, theirs, local):
                files = {**list_files(repo, theirs), **list_files(repo, local)}
                merged = _commit(repo, files, [local, theirs], "unir coord", author_env(ident))
                git(repo, "update-ref", REF, merged, local)
            local = rev(repo, REF)
        if local is None or local == theirs:
            return []
        p = run(repo, "push", "-q", "origin", f"{REF}:{REF}")
        if p.returncode == 0:
            git(repo, "update-ref", TRACK, local)
            return []
        last_error = p.stderr.decode("utf-8", "replace").strip()
        time.sleep(random.uniform(0.05, 0.3) * (i + 1))  # another node published first: retry
    raise GitError(f"couldn't sync the coord branch. Last git error:\n{last_error}")
