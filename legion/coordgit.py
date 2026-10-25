"""The `coord` branch: an events/ directory that files are only ever added to.

It is written with low-level git commands, without a checkout, so it never touches the agent's
working directory. Since every event is a file with a unique name, merging two copies is the
union of their files: there are never conflicts.
"""

import json
import os
import random
import subprocess
import time

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


def read_events(repo, ref: str = REF) -> list[dict]:
    files = list_files(repo, rev(repo, ref))
    if not files:
        return []
    data = git(repo, "cat-file", "--batch", input=("\n".join(files.values()) + "\n").encode(), binary=True)
    out, pos = [], 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        size = int(header[2])
        try:
            out.append(json.loads(data[nl + 1:nl + 1 + size]))
        except ValueError:
            pass  # a corrupt file is ignored; the rest of the log still counts
        pos = nl + 1 + size + 1
    return out


def _commit(repo, files: dict[str, str], parents: list[str], message: str, env) -> str:
    lines = "".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(files.items()))
    events_tree = git(repo, "mktree", input=lines.encode())
    root = git(repo, "mktree", input=f"040000 tree {events_tree}\tevents\n".encode())
    args = ["commit-tree", root]
    for p in parents:
        args += ["-p", p]
    return git(repo, *args, "-m", message, env=env)


def append(repo, ident: Identity, ev: dict) -> None:
    blob = git(repo, "hash-object", "-w", "--stdin", input=E.encode(ev))
    old = rev(repo, REF)
    files = list_files(repo, old)
    files[E.filename(ev)] = blob
    new = _commit(repo, files, [old] if old else [], f"{ev['type']} {ev.get('task', '')}".strip(), author_env(ident))
    git(repo, "update-ref", REF, new, old or ZERO)
    git(repo, "update-ref", PUB + "coord", new)


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


def is_radicle(repo) -> bool:
    p = run(repo, "config", "--get", "legion.transport")
    return p.returncode == 0 and p.stdout.decode().strip() == "radicle"


def peers(repo) -> list[str]:
    if is_radicle(repo):  # with Radicle, the peers are the publishers already copied from its storage
        out = run(repo, "for-each-ref", "--format=%(refname)", PEERS).stdout.decode()
        return sorted({line[len(PEERS):].split("/", 1)[0] for line in out.splitlines() if line.endswith("/coord")})
    p = run(repo, "config", "--get-all", "legion.peer")
    return p.stdout.decode().split() if p.returncode == 0 else []


def is_p2p(repo) -> bool:
    return is_radicle(repo) or bool(peers(repo))


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


def merge_coords(repo, ident: Identity, names: list[str]) -> None:
    """Union of our coord and every peer's: events are files with unique names, so it never conflicts."""
    local = rev(repo, REF)
    cur = local
    for peer in names:
        theirs = rev(repo, f"{PEERS}{peer}/coord")
        if not theirs or theirs == cur:
            continue
        if cur is None or is_ancestor(repo, cur, theirs):
            cur = theirs
        elif not is_ancestor(repo, theirs, cur):
            files = {**list_files(repo, theirs), **list_files(repo, cur)}
            cur = _commit(repo, files, [cur, theirs], "merge coord", author_env(ident))
    if cur and cur != local:
        git(repo, "update-ref", REF, cur, local or ZERO)
    if cur:
        git(repo, "update-ref", PUB + "coord", cur)


def radicle_who(ident: Identity) -> str:
    """This identity's prefix among the legion refs in a Radicle namespace."""
    return ident.author.split(":", 1)[1][:16]


def sync_radicle(repo, ident: Identity) -> list[str]:
    """Fetch from the Radicle network, copy every node's legion refs, merge the coords, publish ours."""
    from . import radicle
    failed = []
    try:
        radicle.keep_bridge(repo)
        if not radicle.network_sync(repo):
            failed.append("network")
    except radicle.RadError:
        failed.append("network")  # offline: carry on with what the local storage has
    try:
        radicle.fetch_all(repo)
        merge_coords(repo, ident, peers(repo))
        radicle.publish(repo, radicle_who(ident))
    except radicle.RadError as e:
        raise GitError(str(e))
    return failed


def has_commit(repo, sha: str | None) -> bool:
    return bool(sha) and run(repo, "cat-file", "-e", sha + "^{commit}").returncode == 0


def update_main(repo, tip: str | None) -> None:
    """Point legion/main at the main the integrator signed, if we already have that commit."""
    if has_commit(repo, tip):
        git(repo, "update-ref", MAIN, tip)
        git(repo, "update-ref", PUB + "main", tip)


def prune_patches(repo, judged: set[str]) -> None:
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


def publish_patch(repo, branch: str) -> None:
    if is_p2p(repo):
        git(repo, "update-ref", branch, "HEAD")
    else:
        git(repo, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")


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
    seen = {}
    for ref in refs:
        if rev(repo, ref):
            for ev in read_events(repo, ref):
                seen.setdefault(ev.get("id"), ev)
    return list(seen.values())


def remote_events(repo) -> list[dict]:
    """Events as they are on origin, publishing nothing (read-only)."""
    run(repo, "fetch", "-q", "origin", f"+{REF}:{TRACK}")
    return read_events(repo, TRACK)


def has_origin(repo) -> bool:
    return "origin" in git(repo, "remote").split()


def sync(repo, ident: Identity, attempts: int = 12) -> list[str]:
    """Merge the local coord with origin's (and publish it), or with every peer's in p2p mode."""
    if is_radicle(repo):
        return sync_radicle(repo, ident)
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
