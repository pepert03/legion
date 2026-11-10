"""The live plane: legion's events between agents in under a second, through relays (nostr.py).

Over git (Radicle) an event waited for a seed to come and fetch it from a node that had to stay up, reachable and
sound: in Erdős #902 the median event of an agent in a sandbox took 32 minutes to 5.6 hours to reach another
node, and two agents on one machine, 2 minutes (docs/diagnostico-2026-11-09.md). Here every event a command
records is sent to several relays at once, which push it to whoever is subscribed and keep it for whoever is not:
all it takes is an outbound connection on port 443, and no account. The events are the same signed events, checked
the same way; a relay can't read them (they go encrypted with the project's key) nor forge one.

What goes through the relays:
- the events (kind KIND, tagged with the project's tag), several to an envelope;
- commits an event names (a patch, saved work, a new main), as a git pack in chunks, each under its own tag,
  for whoever has no other way to get them (see put_commit, get_commit);
- signals nobody keeps (kind SIGNAL): a node asking for a commit the relays don't hold.

Git stays the archive: a node with Radicle merges what the relays bring into its coord, which goes out as before,
and offers the relays what came by git from nodes that don't use them yet (see Bridge).
"""

import atexit
import base64
import hashlib
import json
import os
import queue
import random
import subprocess
import threading
import time
import zlib
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from . import coordgit, nostr
from . import events as E
from .identity import verify
from .locks import held

KIND, SIGNAL = 9467, 29467  # a kind relays keep, and one they only pass on
# Relays that took an unknown key's events of these kinds, 44 KB ones too, and pushed them on in 140 to 430 ms
# (measured from Spain, 2026-11-09). Several, of different people: one down or refusing is not noticed.
DEFAULT_RELAYS = ["wss://relay.damus.io", "wss://relay.nostr.net", "wss://nostr.oxtr.dev",
                  "wss://relay.primal.net", "wss://relay.ditto.pub"]
SLACK = 900  # seconds a read goes back before the last one: an event carries its sender's clock, which may be behind
WINDOW = 6 * 3600  # how far back a first read goes, and how old an event may be to be offered to the relays
PAGE = 500  # events asked for at a time
MAX_PAGES = 400
MAX_PAYLOAD = 30_000  # bytes of events per envelope: relays refuse events over 64 KB
CHUNK = 40_000  # bytes of a pack per chunk
MAX_CHUNKS = 600  # 24 MB: a pack over this doesn't go through relays
WATCHED = 20  # seconds within which a watcher's beat says the clone is being kept up to date


def _cfg(repo, key: str) -> str | None:
    values = coordgit.setting(repo, key)
    return values[-1] if values else None


_folders: dict[str, Path] = {}


def folder(repo) -> Path:
    key = str(repo)
    if key not in _folders:
        _folders[key] = coordgit.common_dir(repo) / "legion-live"
        _folders[key].mkdir(parents=True, exist_ok=True)
    return _folders[key]


def enabled(repo) -> bool:
    """Whether this clone uses the relays: a project that crosses machines does (over Radicle, or relays alone)
    unless told not to (LEGION_LIVE=off, or git config legion.live false); a local one (an origin on this
    machine, the simulation) only when told to (legion.live true)."""
    said = (os.environ.get("LEGION_LIVE") or _cfg(repo, "legion.live") or "").lower()
    if said:
        return said in ("1", "true", "yes", "on")
    return _cfg(repo, "legion.transport") in ("radicle", "relay")


BENCHED = 300  # seconds a relay that failed (or was never in time: see nostr.LATE) is left out of the commands' rounds
# Once none of them answered, the commands leave the relays alone for RETRY_FIRST seconds, and twice as long each
# time in a row that none does, up to ALL_DOWN: a machine whose way out doesn't let them through (muse's proxy,
# before legion went through it) doesn't change in five minutes, and each try costs a command its whole wait. Not
# half an hour at the first: on a machine that was only busy for a moment the commands went back to the node, a
# minute each, for all of it.
RETRY_FIRST, ALL_DOWN = 60, 1800


def all_relays(repo) -> list[str]:
    # The way out git was given here (its proxy, the authorities it trusts) is the relays' way out too.
    nostr.PROXY = _cfg(repo, "https.proxy") or _cfg(repo, "http.proxy")
    nostr.CA_FILES = [p for p in (_cfg(repo, "http.sslCAInfo"),) if p]
    said = os.environ.get("LEGION_RELAYS") or _cfg(repo, "legion.relays")
    return [u for u in said.replace(",", " ").split() if u] if said else list(DEFAULT_RELAYS)


def relays(repo) -> list[str]:
    """The relays a command uses now: the project's, without the ones that failed or kept a command waiting in
    the last BENCHED seconds (remembered between commands: each is a process of its own), unless that's all of
    them. A dead relay cost every command its whole wait."""
    urls, kept, now = all_relays(repo), _found(repo), time.time()
    late = kept.get("late") if isinstance(kept.get("late"), dict) else {}
    for u in urls:
        if u in nostr._ok:
            continue  # it answered this process since
        if _num(kept.get(u)) > nostr._down.get(u, 0):
            nostr._down[u] = _num(kept[u])
        if u not in nostr._counted and isinstance(late.get(u), int):
            nostr._late[u] = max(late[u], nostr._late.get(u, 0))
    good = [u for u in urls if now - nostr._down.get(u, 0) > BENCHED]
    return good or urls


def _num(x) -> float:
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


def _found(repo) -> dict:
    """What the last commands found about the relays (each is a process of its own)."""
    try:
        kept = json.loads((folder(repo) / "down.json").read_text(encoding="utf-8"))
        return kept if isinstance(kept, dict) else {}
    except (OSError, ValueError):
        return {}


def retry_at(repo) -> float:
    """When the commands try the relays again, after none answered (0: they do now)."""
    kept = _found(repo)
    none = _num(kept.get("none answered"))
    return none + min(ALL_DOWN, RETRY_FIRST * 2 ** max(0, int(_num(kept.get("times")) or 1) - 1)) if none else 0.0


def reachable(repo) -> bool:
    """False for a while after no relay at all answered (see RETRY_FIRST): a command doesn't try until then. That
    some are left out isn't it: when every one is, a command tries them all."""
    return time.time() >= retry_at(repo)


_at_exit: set[str] = set()


def remember_relays(repo, answered: bool | None = None) -> None:
    """Keep for the next commands what this one found: which relays failed, which weren't in time, and whether
    any answered at all (None: this round doesn't say)."""
    urls, now, kept = all_relays(repo), time.time(), _found(repo)
    down: dict = {u: t for u, t in nostr._down.items() if u in urls and now - t <= BENCHED}
    late = {u: n for u, n in nostr._late.items() if u in urls and n > 0}
    if late:
        down["late"] = late
    if answered is False:
        down["none answered"], down["times"] = now, int(_num(kept.get("times"))) + 1
    elif answered is None and kept.get("none answered"):
        down["none answered"], down["times"] = kept["none answered"], kept.get("times", 1)
    f = folder(repo) / "down.json"
    try:
        if down or f.exists():
            f.write_text(json.dumps(down), encoding="utf-8")
    except OSError:
        pass
    if str(repo) not in _at_exit:  # a relay that answered after its round went on was only slow: said as it ends
        _at_exit.add(str(repo))
        atexit.register(_as_it_ends, repo)


def _as_it_ends(repo) -> None:
    try:
        if folder(repo).is_dir():
            remember_relays(repo)
    except Exception:  # noqa: BLE001 (the process is ending)
        pass


class Channel:
    """A project's place on the relays: the tag its events carry and the key they're sealed with, both from the
    project's secret. Whoever doesn't have the secret sees random bytes under a random tag."""

    def __init__(self, secret: bytes):
        self.secret = secret
        self.key = self._d(b"key")
        self.tag, self.signal = self._d(b"tag").hex()[:32], self._d(b"signal").hex()[:32]

    def _d(self, label: bytes) -> bytes:
        return hashlib.sha256(b"legion-live/" + label + b"/" + self.secret).digest()

    def blob(self, sha: str) -> str:
        return self._d(b"blob/" + sha.encode()).hex()[:32]

    def seal(self, data: bytes) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + ChaCha20Poly1305(self.key).encrypt(nonce, zlib.compress(data, 6), b"legion-live1")).decode()

    def open(self, content) -> bytes | None:
        try:
            raw = base64.b64decode(content, validate=True)
            data = ChaCha20Poly1305(self.key).decrypt(raw[:12], raw[12:], b"legion-live1")
            z = zlib.decompressobj()
            out = z.decompress(data, 64 * 1024 * 1024)
            return None if z.unconsumed_tail else out
        except (InvalidTag, ValueError, TypeError, zlib.error):
            return None


def project_secret(genesis: str) -> bytes:
    """The secret of a project anyone may read (its repository is public): from its first event, so every node
    that has the log is on the same channel without being told anything."""
    return hashlib.sha256(b"legion-live:" + genesis.encode()).digest()


def secret(repo, st=None) -> bytes | None:
    """The project's secret: the one this clone was given (an invitation's, legion.livekey), or the public one."""
    own = _cfg(repo, "legion.livekey")
    if own:
        return bytes.fromhex(own)
    kept = folder(repo) / "genesis"
    try:
        genesis = kept.read_text(encoding="utf-8").strip()
    except OSError:
        st = st or coordgit.state(repo)
        if not st.project:
            return None
        genesis = st.project["id"]
        kept.write_text(genesis, encoding="utf-8")
    return project_secret(genesis)


def channel(repo, st=None) -> Channel | None:
    s = secret(repo, st)
    return Channel(s) if s else None


def nsec(repo) -> bytes:
    """This clone's key on the relays: random, made here, and nobody's account. It says nothing of the agent, whose
    own key signs the events inside."""
    f = folder(repo) / "nsec"
    try:
        return bytes.fromhex(f.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        while True:
            key = os.urandom(32)
            if 1 <= int.from_bytes(key, "big") < nostr.N:
                break
        f.write_text(key.hex(), encoding="utf-8")
        return key


# --- what this clone knows to be on the relays ------------------------------------------------------------

_sent: dict[str, set] = {}


def sent(repo) -> set[str]:
    """The ids of the events known to be on the relays: sent there by this clone, or read from them."""
    key = str(repo)
    if key not in _sent:
        try:
            _sent[key] = set((folder(repo) / "sent").read_text(encoding="utf-8").split())
        except OSError:
            _sent[key] = set()
    return _sent[key]


def mark(repo, ids) -> None:
    known = sent(repo)
    new = [i for i in dict.fromkeys(ids) if i not in known]
    if new:
        known.update(new)
        with open(folder(repo) / "sent", "a", encoding="utf-8") as f:
            f.write("".join(i + "\n" for i in new))


def cursor(repo) -> float:
    try:
        return float((folder(repo) / "cursor").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0


def set_cursor(repo, at: float) -> None:
    if at > cursor(repo):
        (folder(repo) / "cursor").write_text(str(at), encoding="utf-8")


def read_at(repo) -> float:
    """When this clone last read from the relays."""
    try:
        return (folder(repo) / "cursor").stat().st_mtime
    except OSError:
        return 0.0


def watched(repo) -> bool:
    """Whether a process stays subscribed for this clone right now (see Watcher): a command needn't read then."""
    try:
        return time.time() - (folder(repo) / "watching").stat().st_mtime < WATCHED
    except OSError:
        return False


# --- envelopes --------------------------------------------------------------------------------------------


def wrap(ch: Channel, key: bytes, events: list[dict]) -> list[dict]:
    """The events, sealed, as few relay events as they fit in."""
    out, batch, size = [], [], 0

    def flush():
        if batch:
            body = json.dumps({"e": batch}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            out.append(nostr.event(key, KIND, [["t", ch.tag]], ch.seal(body)))

    for ev in events:
        n = len(E.encode(ev))
        if batch and size + n > MAX_PAYLOAD:
            flush()
            batch, size = [], 0
        batch.append(ev)
        size += n
    flush()
    return out


def unwrap(ch: Channel, envelope: dict) -> list[dict]:
    data = ch.open(envelope.get("content"))
    if data is None:
        return []
    try:
        got = json.loads(data).get("e")
    except (ValueError, AttributeError):
        return []
    return [e for e in got if isinstance(e, dict)] if isinstance(got, list) else []


def signed(ev: dict) -> bool:
    """Whether the event is what its author signed. Not whether this legion knows its type: one a newer legion
    made is kept too, as git keeps it, for when this one is updated."""
    try:
        if not (isinstance(ev.get("lamport"), int) and 0 < ev["lamport"] < 10 ** 8
                and isinstance(ev.get("ts"), (int, float)) and isinstance(ev.get("type"), str)
                and isinstance(ev.get("name"), str)):
            return False
        body = E.canonical(E._body(ev))
        return hashlib.sha256(body).hexdigest()[:16] == ev.get("id") and verify(ev.get("author"), body, ev.get("sig"))
    except (TypeError, ValueError):
        return False


def local_ids(repo) -> set[str]:
    return {name[9:25] for name in coordgit.list_files(repo, coordgit.rev(repo, coordgit.REF))}


def take(repo, ident, events: list[dict]) -> list[dict]:
    """Add to this clone's log the events it lacks, each checked against its author's signature. The new ones."""
    if not events:
        return []
    from .daemon import repo_lock  # here: the daemon uses this module
    new, known = [], []
    with held(repo_lock(repo), wait=30, what="events from the relays going into the log"):
        have, seen = local_ids(repo), set()
        for ev in events:
            i = ev.get("id")
            if not isinstance(i, str):
                continue
            if i in have:
                known.append(i)
            elif i not in seen and signed(ev):
                seen.add(i)
                new.append(ev)
        if new:
            coordgit.add_events(repo, ident, new)
    mark(repo, known + [e["id"] for e in new])
    return new


# --- a command's round ------------------------------------------------------------------------------------


def pull(repo, ident, timeout: float = 5.0, deep: bool = False, since: float | None = None) -> tuple[list[dict], bool]:
    """Read from the relays what was published since the last read, or since `since` (`deep`: everything they
    hold), in pages, however much it is. (The events that were new here, whether a relay answered.)"""
    ch = channel(repo)
    if ch is None:
        return [], False
    urls, last, started = relays(repo), since or cursor(repo), time.time()
    since = 0 if deep else max(0, int((last or started - WINDOW) - SLACK))
    got, seen, until, answered_any = [], set(), None, False
    for _ in range(MAX_PAGES):
        f = {"kinds": [KIND], "#t": [ch.tag], "since": since, "limit": PAGE}
        if until is not None:
            f["until"] = until
        page, answered = nostr.query(urls, [f], timeout)
        if not answered and not answered_any:
            # None of the relays in use: the ones that had been left out, before going without. A command's
            # fastest relay having a bad moment sent it back to the node.
            rest = [u for u in all_relays(repo) if u not in urls]
            if rest:
                urls = rest
                page, answered = nostr.query(urls, [f], timeout)
        if not answered:
            break
        answered_any = True
        fresh = [e for e in page if e.get("id") not in seen]
        seen.update(e.get("id") for e in fresh)
        got += fresh
        if len(fresh) < 50:  # a relay with more to give sent a full page
            break
        until = min(int(e.get("created_at", 0)) for e in page)
    remember_relays(repo, answered_any)
    new = take(repo, ident, [x for e in got for x in unwrap(ch, e)])
    if answered_any:
        stamps = [e["created_at"] for e in got if isinstance(e.get("created_at"), int) and e["created_at"] <= started + SLACK]
        set_cursor(repo, max([started - 5] + stamps))
    return new, answered_any


def push(repo, events: list[dict], timeout: float = 6.0) -> bool:
    """Send these events to the relays. True when every one of them was taken by a relay at least."""
    ch = channel(repo)
    if ch is None or not events:
        return False
    envelopes = wrap(ch, nsec(repo), events)
    urls = relays(repo)
    answers = nostr.publish(urls, envelopes, timeout)
    rest = [u for u in all_relays(repo) if u not in urls]
    if not answers and rest:  # (as pull does)
        answers = nostr.publish(rest, envelopes, timeout)
    remember_relays(repo, bool(answers))
    out = all(any(a.get(env["id"], "") is None for a in answers.values()) for env in envelopes)
    if out:
        mark(repo, [e["id"] for e in events])
    return out


def own_pending(repo) -> bool:
    """Whether this clone recorded something since its events last went to the relays: its own latest event
    (refs/legion/own) is not the one that went."""
    own = coordgit.rev(repo, coordgit.OWN)
    try:
        return bool(own) and (folder(repo) / "own").read_text(encoding="utf-8").strip() != own
    except OSError:
        return bool(own)


def own_sent(repo, own: str | None) -> None:
    if own:
        (folder(repo) / "own").write_text(own, encoding="utf-8")


def outbox(repo, events, author: str | None = None) -> list[dict]:
    """Of these events, the ones to send: not known to be on the relays, and recent (an old one travels by git,
    or with the log a newcomer downloads). `author`: only its own."""
    known, floor = sent(repo), time.time() - WINDOW
    return [e for e in events if e.get("id") not in known and (author is None or e.get("author") == author)
            and isinstance(e.get("ts"), (int, float)) and e["ts"] >= floor]


# --- commits ----------------------------------------------------------------------------------------------
#
# An event may name a commit: a patch, saved work, the new main. Over Radicle the commit came with the node's
# refs. Through relays it goes as a git pack of what the commit adds to the main everyone has, cut in chunks
# under a tag made from the commit's id: whoever needs the commit asks the relays for that tag. Git names objects
# by their content, so a pack that isn't the commit's simply doesn't give it.


def put_commit(repo, sha: str, have: list[str], timeout: float = 30.0) -> bool:
    """Publish commit `sha` for whoever holds the commits in `have` (the main it was made on)."""
    ch = channel(repo)
    if ch is None or not coordgit.has_commit(repo, sha):
        return False
    bases = [h for h in dict.fromkeys(have) if h and h != sha and coordgit.has_commit(repo, h)]
    return bool(_put_pack(repo, ch, ch.blob(sha), sha, bases, timeout))


def _put_pack(repo, ch: Channel, tag: str, sha: str, bases: list[str], timeout: float) -> list[str]:
    """Commit `sha` without what `bases` hold, as a pack in chunks under `tag`. The chunks' ids on the relays
    (none when no relay took them all)."""
    revs = sha + "\n" + "".join(f"^{h}\n" for h in bases)
    p = subprocess.run(["git", "pack-objects", "--revs", "--stdout", "-q"], cwd=repo, input=revs.encode(),
                       capture_output=True)
    if p.returncode or len(p.stdout) > MAX_CHUNKS * CHUNK:
        return []
    parts = [p.stdout[i:i + CHUNK] for i in range(0, len(p.stdout), CHUNK)]
    head = {"blob": os.urandom(8).hex(), "n": len(parts), "commit": sha, "have": bases, "at": round(time.time())}
    key = nsec(repo)
    chunks = [nostr.event(key, KIND, [["t", tag]], ch.seal(json.dumps(dict(head, i=i)).encode() + b"\n" + part))
              for i, part in enumerate(parts)]
    urls = relays(repo)
    answers = nostr.publish(urls if len(chunks) <= 4 else urls[:3], chunks, max(timeout, len(chunks) * 1.5), grace=3.0)
    if any(all(a.get(c["id"], "") is None for c in chunks) for a in answers.values()):
        return [c["id"] for c in chunks]
    return []


def on_relays(repo, sha: str, timeout: float = 5.0) -> bool:
    ch = channel(repo)
    if ch is None:
        return False
    got, _ = nostr.query(relays(repo), [{"kinds": [KIND], "#t": [ch.blob(sha)], "limit": 1}], timeout)
    return bool(got)


ASK_AGAIN = 60  # seconds before a commit nobody had is asked for again: every command would wait for it anew


def _missed(repo) -> dict:
    try:
        kept = json.loads((folder(repo) / "missing.json").read_text(encoding="utf-8"))
        return {k: v for k, v in kept.items() if isinstance(v, (int, float)) and time.time() - v < ASK_AGAIN}
    except (OSError, ValueError, AttributeError):
        return {}


def get_commit(repo, sha: str, ref: str | None = None, wait: float = 0.0, depth: int = 0) -> bool:
    """Bring commit `sha` from the relays, and the commits it was made on if this clone lacks them too. `wait`:
    seconds to give another node to publish it when the relays don't hold it (see want). `ref`: a ref to leave
    on it, so git keeps it."""
    if not sha or depth > 30:
        return False
    if not coordgit.has_commit(repo, sha):
        ch = channel(repo)
        missed = _missed(repo)
        if ch is None or sha in missed:
            return False
        asked, end = False, time.time() + wait
        while not _fetch_pack(repo, ch, ch.blob(sha), sha, depth):
            if time.time() >= end:
                if wait:  # nobody who listens had it: not asked again, by this command or the next, for a while
                    try:
                        (folder(repo) / "missing.json").write_text(json.dumps({**missed, sha: time.time()}),
                                                                   encoding="utf-8")
                    except OSError:
                        pass
                return False
            if not asked:
                want(repo, sha)
                asked = True
            time.sleep(min(1.0, max(0.1, end - time.time())))
        if not coordgit.has_commit(repo, sha):
            return False
    if ref and ref.startswith(coordgit.PATCHES):  # the name comes from someone's event: only where patches live
        coordgit.run(repo, "update-ref", ref, sha)
    return True


def _fetch_pack(repo, ch: Channel, tag: str, sha: str | None, depth: int = 0) -> dict | None:
    """Take from the relays the pack published under `tag` (for commit `sha`; any commit, the latest published,
    when None) and put its objects in this repository. Its header ({commit, at, ...}), or None."""
    got, _ = nostr.query(relays(repo), [{"kinds": [KIND], "#t": [tag], "limit": PAGE}], 20.0, grace=2.5)
    blobs: dict[str, dict] = {}
    for e in got:
        data = ch.open(e.get("content"))
        if data is None:
            continue
        head, _, part = data.partition(b"\n")
        try:
            h = json.loads(head)
            n, i, commit = int(h["n"]), int(h["i"]), str(h["commit"])
        except (ValueError, KeyError, TypeError):
            continue
        if (sha and commit != sha) or len(commit) != 40 or not 0 < n <= MAX_CHUNKS or not 0 <= i < n:
            continue
        b = blobs.setdefault(str(h.get("blob")), {"n": n, "have": h.get("have") or [], "parts": {}, "commit": commit,
                                                  "at": h.get("at") if isinstance(h.get("at"), (int, float)) else 0})
        b["parts"][i] = part
    for b in sorted(blobs.values(), key=lambda b: -b["at"]):
        if len(b["parts"]) != b["n"]:
            continue
        pack = b"".join(b["parts"][i] for i in range(b["n"]))
        p = subprocess.run(["git", "index-pack", "--stdin"], cwd=repo, input=pack, capture_output=True)
        if p.returncode or not coordgit.has_commit(repo, b["commit"]):
            continue
        # The commits it was made on, if this clone doesn't have them either (it was away while main moved).
        for base in b["have"]:
            if isinstance(base, str) and not coordgit.has_commit(repo, base):
                get_commit(repo, base, depth=depth + 1)
        return b
    return None


# --- the whole log, for whoever arrives ----------------------------------------------------------------------
#
# A newcomer with relays alone needs every event there has been. Asking the relays for them all is a request per
# 500 envelopes, and counts on every relay having kept every one. So a node that stays publishes, every so often,
# the log as it has it: one pack (a commit with every event file), under one tag. A newcomer takes the latest,
# checks each event's signature as ever, and reads from the relays only what came after it.

SNAPSHOT_EVERY = 6 * 3600


def put_log(repo, timeout: float = 60.0) -> bool:
    """Publish this clone's log whole, for newcomers, and ask the relays to drop the copy it published before."""
    ch = channel(repo)
    head = coordgit.rev(repo, coordgit.REF)
    if ch is None or not head:
        return False
    tree = coordgit.git(repo, "rev-parse", head + "^{tree}")
    env = dict(os.environ, GIT_AUTHOR_NAME="legion", GIT_AUTHOR_EMAIL="legion@legion.local",
               GIT_COMMITTER_NAME="legion", GIT_COMMITTER_EMAIL="legion@legion.local")
    snap = coordgit.git(repo, "commit-tree", tree, "-m", "the log", env=env)  # no history: only the events
    ids = _put_pack(repo, ch, ch.blob("log"), snap, [], timeout)
    if not ids:
        return False
    kept = folder(repo) / "log-chunks"
    try:
        old = kept.read_text(encoding="utf-8").split()
    except OSError:
        old = []
    if old:  # NIP-09: a relay that honours it deletes them; one that doesn't keeps a copy nobody picks
        gone = nostr.event(nsec(repo), 5, [["e", i] for i in old[:500]], "")
        nostr.publish(relays(repo), [gone], 5.0)
    kept.write_text("\n".join(ids), encoding="utf-8")
    (folder(repo) / "log-at").write_text(str(time.time()), encoding="utf-8")
    return True


def get_log(repo, ident) -> list[dict]:
    """Take the latest copy of the log the relays hold. The events that were new here."""
    from .daemon import repo_lock
    ch = channel(repo)
    if ch is None:
        return []
    found = _fetch_pack(repo, ch, ch.blob("log"), None)
    if not found:
        return []
    with held(repo_lock(repo), wait=30):
        new = coordgit.take_tree(repo, ident, found["commit"], signed)
    mark(repo, [e["id"] for e in new])
    if found["at"]:
        set_cursor(repo, min(float(found["at"]), time.time()))
    return new


def keep_log(repo) -> bool:
    """For a process that stays, in a project of relays alone (with Radicle, a newcomer takes the log from it):
    publish the log when nobody has for SNAPSHOT_EVERY. True if it did."""
    ch = channel(repo)
    if ch is None or not coordgit.is_relay(repo):
        return False
    mine = folder(repo) / "log-at"
    try:
        if time.time() - float(mine.read_text(encoding="utf-8")) < SNAPSHOT_EVERY:
            return False
    except (OSError, ValueError):
        pass
    newest, _ = nostr.query(relays(repo), [{"kinds": [KIND], "#t": [ch.blob("log")], "limit": 1}], 6.0)
    if any(time.time() - e.get("created_at", 0) < SNAPSHOT_EVERY for e in newest):
        mine.write_text(str(time.time()), encoding="utf-8")  # someone else just did
        return False
    return put_log(repo)


# --- invitations --------------------------------------------------------------------------------------------


def invitation(repo, st) -> str:
    """What another agent needs to join through the relays alone, in one word: the project's name, its secret
    and its relays when they aren't the usual ones. `legion join legion:...` takes it."""
    s = secret(repo, st)
    if s is None:
        raise ValueError("no project here yet")
    said = os.environ.get("LEGION_RELAYS") or _cfg(repo, "legion.relays")
    data = {"v": 1, "name": (st.project or {}).get("project") or "project", "key": s.hex()}
    if said:
        data["relays"] = relays(repo)
    raw = zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9)
    return "legion:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_invitation(token: str) -> dict:
    try:
        raw = token.split(":", 1)[1]
        data = json.loads(zlib.decompress(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))))
        bytes.fromhex(data["key"])
        if len(data["key"]) != 64 or not isinstance(data.get("name"), str):
            raise ValueError
        if "relays" in data and not (isinstance(data["relays"], list)
                                     and all(isinstance(u, str) and u.startswith(("ws://", "wss://")) for u in data["relays"])):
            raise ValueError
        return data
    except (ValueError, KeyError, IndexError, TypeError, zlib.error):
        raise ValueError("not a legion invitation") from None


def want(repo, sha: str) -> None:
    """Ask whoever is listening for a commit the relays don't hold: any node that has it publishes it (see
    Watcher). A signal: relays pass it on and don't keep it."""
    ch = channel(repo)
    if ch is None:
        return
    have = [h for h in (coordgit.rev(repo, coordgit.MAIN),) if h]
    ev = nostr.event(nsec(repo), SIGNAL, [["t", ch.signal]], ch.seal(json.dumps({"want": sha, "have": have}).encode()))
    nostr.publish(relays(repo), [ev], 4.0)


def offer(repo, st, events: list[dict]) -> None:
    """Before these events go out, publish the commits they name: whoever reads the event can get them."""
    for ev in events:
        sha = ev.get("commit") or ev.get("merge_commit")
        if not (isinstance(sha, str) and len(sha) == 40 and coordgit.has_commit(repo, sha)):
            continue
        tip = st.main_tip if st else None
        bases = [b for b in (ev.get("base"), tip) if isinstance(b, str) and b != sha]
        bases = [b for b in bases if coordgit.has_commit(repo, b) and not coordgit.is_ancestor(repo, sha, b)]
        try:
            put_commit(repo, sha, bases)
        except (OSError, coordgit.GitError):
            pass


# --- staying subscribed -----------------------------------------------------------------------------------


CHECK = 300  # seconds between the reads in pages of a process that stays subscribed (see Watcher._catch_up)


class Watcher:
    """Stays subscribed for a clone: every event published reaches its log within a moment, and `on_new(events)`
    is told. With `serve`, it also answers other nodes' requests for commits this clone holds (see want)."""

    def __init__(self, repo, ident, on_new=None, serve: bool = False):
        self.repo, self.ident, self.on_new, self.serve = repo, ident, on_new, serve
        self.ch = channel(repo)
        self.stop, self.q, self.up = threading.Event(), queue.Queue(), set()
        # Up to when this clone surely has everything, which only a read in pages moves on (see _catch_up).
        self.whole, self.caught_up, self.looked = cursor(repo) or 0.0, False, 0.0
        self.asked: dict[str, float] = {}
        self.threads = nostr.listen(all_relays(repo), self._filters, lambda ev, url: self.q.put(ev), self.stop,
                                    self._state)
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _filters(self) -> list[dict]:
        since = max(0, int((cursor(self.repo) or time.time() - WINDOW) - SLACK))
        return [{"kinds": [KIND], "#t": [self.ch.tag], "since": since},
                {"kinds": [SIGNAL], "#t": [self.ch.signal], "since": int(time.time()) - 30}]

    def _state(self, url: str, connected: bool) -> None:
        (self.up.add if connected else self.up.discard)(url)
        if not self.up:
            self.caught_up = False  # every relay went (the network, or the machine slept): see _catch_up

    def _catch_up(self) -> None:
        """Read in pages everything published since this clone last had it all. A subscription brings what's
        published from now on and, of what was before, as much as a relay gives at once (500 events, or fewer):
        a machine that was off for the night came back to the last 500, moved its mark to now and never asked for
        the rest. jose's is a laptop. Done as it starts, whenever every relay had gone, and every CHECK seconds:
        whatever a subscription missed, for whatever reason, is here by the next one."""
        started = self.looked = time.time()
        try:
            new, answered = pull(self.repo, self.ident, 15, since=self.whole or None)
        except Exception as e:  # noqa: BLE001
            print(f"legion: live: {str(e).strip()[-200:]}", flush=True)
            return
        if answered:
            self.whole, self.caught_up = started, True
            if new and self.on_new:
                self.on_new(new)
            if new and self.serve:
                self._into_git(new)

    def connected(self) -> bool:
        return bool(self.up)

    def _work(self) -> None:
        beat, seen = 0.0, set()
        while not self.stop.is_set():
            batch = []
            try:
                batch.append(self.q.get(timeout=0.5))
                time.sleep(0.05)  # a burst comes together
                while True:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if self.up and (not self.caught_up or time.time() - self.looked > CHECK):
                if self.caught_up or time.time() - self.looked > 5:  # (not at every turn while no relay answers)
                    self._catch_up()
            if self.up and time.time() - beat > 5:
                beat = time.time()
                try:
                    (folder(self.repo) / "watching").write_text(str(os.getpid()), encoding="utf-8")
                except OSError:
                    pass
            batch = [e for e in batch if e.get("id") not in seen]
            if not batch:
                continue
            seen.update(e.get("id") for e in batch)
            if len(seen) > 50_000:
                seen.clear()
            try:
                stored = [e for e in batch if e.get("kind") == KIND]
                new = take(self.repo, self.ident, [x for e in stored for x in unwrap(self.ch, e)])
                stamps = [e["created_at"] for e in stored
                          if isinstance(e.get("created_at"), int) and e["created_at"] <= time.time() + SLACK]
                if stamps and self.caught_up:  # (until then the mark stays where the read in pages starts)
                    set_cursor(self.repo, max(stamps))
                if new and self.on_new:
                    self.on_new(new)
                if self.serve:
                    for e in batch:
                        if e.get("kind") == SIGNAL:
                            self._signal(e)
                    self._into_git(new)
            except Exception as e:  # noqa: BLE001 (one bad batch mustn't end the subscription)
                print(f"legion: live: {str(e).strip()[-200:]}", flush=True)

    def _into_git(self, events: list[dict]) -> None:
        """On a clone that has git too (Radicle): bring the commits these events name, with their refs, so they go
        out by git with the rest. A patch sent by a node with relays alone named a commit that nodes on an older
        legion, with git alone, would never get: they'd reject it as missing."""
        if not coordgit.is_radicle(self.repo):
            return
        named = [(ev.get("commit") or ev.get("merge_commit"), ev.get("branch")) for ev in events]
        named = [(sha, ref) for sha, ref in named if isinstance(sha, str) and len(sha) == 40
                 and not coordgit.has_commit(self.repo, sha)]
        if not named:
            return

        def bring():
            for sha, ref in named:
                try:
                    get_commit(self.repo, sha, ref if isinstance(ref, str) else None)
                except Exception:  # noqa: BLE001 (whoever needs it asks again)
                    pass

        threading.Thread(target=bring, daemon=True).start()

    def _signal(self, envelope: dict) -> None:
        data = self.ch.open(envelope.get("content"))
        try:
            msg = json.loads(data) if data else {}
        except ValueError:
            return
        sha = msg.get("want")
        if not (isinstance(sha, str) and len(sha) == 40) or time.time() - self.asked.get(sha, 0) < 120:
            return
        if not coordgit.has_commit(self.repo, sha):
            return
        self.asked[sha] = time.time()

        def answer():
            time.sleep(random.uniform(0.2, 2.0))  # several nodes hold it: whoever is first, the others see it there
            if not on_relays(self.repo, sha, 4.0):
                put_commit(self.repo, sha, [h for h in msg.get("have") or [] if isinstance(h, str)])

        threading.Thread(target=answer, daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        try:
            (folder(self.repo) / "watching").unlink(missing_ok=True)
        except OSError:
            pass


# --- git and the relays, each with what the other has ------------------------------------------------------

BRIDGE_DELAY = (20, 60)  # seconds an event that came by git waits before this node offers it to the relays
BRIDGE_MOST = 200  # events a round offers at most


class Bridge:
    """What reached this clone by git (from a node that doesn't use the relays yet) and isn't on them, offered to
    them, by a process that stays (the machine's sync process): so a node that only has the relays sees the nodes
    that only have git, and the other way round (what the relays bring is merged into the coord, which goes out
    by git as ever). Every such process sees the same event arrive: each waits a while of its own first, and
    whoever is first, the others read it from the relays and leave it."""

    def __init__(self, repo):
        self.repo, self.first_seen, self.delay = repo, {}, random.uniform(*BRIDGE_DELAY)

    def round(self) -> int:
        now = time.time()
        pending = outbox(self.repo, coordgit.read_events(self.repo))
        ids = {e["id"] for e in pending}
        self.first_seen = {i: t for i, t in self.first_seen.items() if i in ids}
        for i in ids:
            self.first_seen.setdefault(i, now)
        due = [e for e in pending if now - self.first_seen[e["id"]] >= self.delay][:BRIDGE_MOST]
        try:
            keep_log(self.repo)
        except (OSError, coordgit.GitError):
            pass
        if not due or not push(self.repo, due):
            return 0
        return len(due)
