"""legion viz: a local server that draws tasks, subtasks and agents as a live graph.

Read-only: it fetches what origin or the peers have, but never writes to the coord branch, so it can
stay open in the same repository an agent is working in.
"""

import json
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import coordgit
from . import state as S
from . import trust

PAGE = Path(__file__).parent / "templates" / "viz.html"
CHECKING_SECONDS = 1800  # a check announced longer ago than this, with no result, is taken as abandoned
ACTIVE_SECONDS = 600  # an agent counts as active if it published something in the last 10 minutes
GONE_SECONDS = 7200   # and it leaves the graph after 2 hours without publishing, holding nothing


class Source:
    """A repository's events. A thread refreshes them every `interval` seconds, so fetching from
    peers (which can be slow) never delays a response to the page."""

    def __init__(self, repo: Path, interval: float, fetch: bool):
        self.repo, self.interval, self.fetch = repo, interval, fetch
        self.events: list[dict] = []
        self.refresh()
        threading.Thread(target=self._loop, daemon=True).start()

    def refresh(self) -> None:
        try:
            self.events = coordgit.view_state(self.repo, fetch=self.fetch).events  # only valid ones, sorted
        except coordgit.GitError:
            pass  # keep showing the last thing that could be read

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            self.refresh()

    def get(self) -> list[dict]:
        return self.events


def snapshot(events: list[dict], upto: int | None) -> dict:
    """The state after the first `upto` events (or all of them, live), ready to draw."""
    from .cli import describe, event_text
    live = upto is None or upto >= len(events)
    evs = events if live else events[:max(upto, 0)]
    st = S.build(evs, trusted=True)  # already validated by the Source
    now = time.time() if live else (evs[-1]["ts"] if evs else 0)

    tasks = []
    for t in st.tasks.values():
        status = st.status(t.id, now)
        working = st.claimants(t.id, now) if status in ("claimed", "review", "blocked") else []
        h = working[0] if working else None
        kids = st.children(t.id)
        tasks.append({"id": t.id, "key": t.key, "title": t.title, "status": status, "mode": t.mode,
                      "depth": t.depth, "parent": t.parent, "deps": t.deps, "size": t.size,
                      "holder": h.author if h else None, "holder_name": h.name if h else None,
                      "working": [c.author for c in working],
                      "best": st.best(t.id), "zone": t.zone, "accept": t.accept, "body": t.body,
                      "children": len(kids), "children_done": sum(st.done(k.id) for k in kids)})

    agents = {}
    for ev in evs:
        a = agents.setdefault(ev["author"], {
            "id": agent_id(ev["author"]), "author": ev["author"], "name": ev["name"], "kinds": [], "models": [],
            "role": "integrator" if ev["author"] in st.integrators else "agent",
            "last_ts": 0, "holds": [], "done": [], "last": "",
            "stats": {"claims": 0, "patches": 0, "accepted": 0, "rejected": 0, "reports": 0, "sketches": 0}})
        a["name"] = ev["name"]
        a["last_ts"] = max(a["last_ts"], ev["ts"])
        if ev["type"] not in ROUTINE:
            a["last"] = event_text(ev, st)
        for field, bucket in (("agent", "kinds"), ("model", "models")):
            if ev.get(field) and ev[field] not in a[bucket]:
                a[bucket].append(ev[field])
        counter = {"task.claimed": "claims", "patch.submitted": "patches", "attempt.reported": "reports",
                   "sketch.proposed": "sketches"}.get(ev["type"])
        if counter:
            a["stats"][counter] += 1
    for p in st.patches.values():
        if p.verdict and p.author in agents:
            agents[p.author]["stats"][p.verdict] += 1
            if p.verdict == "accepted" and p.task not in agents[p.author]["done"]:
                agents[p.author]["done"].append(p.task)
    for t in tasks:
        for who in t["working"]:  # the holder, and whoever joined it
            if who in agents:
                agents[who]["holds"].append(t["id"])
    # Whoever is checking a patch (an agent verifying it, the integrator reviewing it) sits on its task too,
    # from patch.checking until its own verification, or the patch's verdict.
    checking = {}
    for ev in evs:
        t = ev["type"]
        if t == "patch.checking":
            checking[(ev["author"], ev.get("patch"))] = (ev.get("task"), ev["ts"])
        elif t == "patch.verified":
            checking.pop((ev["author"], ev.get("patch")), None)
        elif t in ("patch.accepted", "patch.rejected"):
            checking = {k: v for k, v in checking.items() if k[1] != ev.get("patch")}
    for (who, _), (tid, ts) in checking.items():
        if who in agents and now - ts < CHECKING_SECONDS:
            agents[who].setdefault("checking", []).append(tid)
            if tid not in agents[who]["holds"]:
                agents[who]["holds"].append(tid)
    # Competitive tasks aren't claimed: whoever has a patch there waiting for its verdict is working on it.
    for p in st.patches.values():
        if p.verdict is None and p.author in agents and st.tasks.get(p.task) and \
                st.tasks[p.task].mode != "exclusive" and p.task not in agents[p.author]["holds"]:
            agents[p.author]["holds"].append(p.task)
    by_agent, by_person = trust.compute(st, now)
    lead = st.leader(now)
    for a in agents.values():
        a["active"] = bool(a["holds"]) or now - a["last_ts"] < ACTIVE_SECONDS
        a["owner"], a["number"] = split_name(a["name"])
        a["role"] = "integrator" if a["author"] in st.integrators else "agent"  # membership can change
        a["leader"] = bool(lead and lead.author == a["author"])
        # Offline: no claim, no lease, and silent for a while. A working agent holds a claim, so it stays.
        holds_lease = any(l.author == a["author"] and l.active(now) for l in st.leases)
        a["gone"] = not a["holds"] and not holds_lease and now - a["last_ts"] > GONE_SECONDS
        a["trust"] = None if a["role"] == "integrator" else (by_agent.get(a["author"]) or trust.Trust()).as_dict()
    people = {name: t.as_dict() for name, t in by_person.items()}

    counts = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    recent = [item(e, st) for e in evs if e["type"] not in ROUTINE][-14:]
    return {"project": st.project.get("project") if st.project else None, "description": st.description,
            "index": len(evs), "total": len(events), "live": live, "ts": now,
            "event": describe(evs[-1], st) if evs else "", "counts": counts,
            "tasks": tasks, "agents": list(agents.values()), "people": people, "recent": recent}


def agent_id(author: str) -> str:
    return "a-" + author[8:20]


def split_name(name: str) -> tuple[str, int | None]:
    """'ana#2' -> ('ana', 2): the person who owns the agent and that agent's number."""
    person, sep, num = name.rpartition("#")
    return (person, int(num)) if sep and person and num.isdigit() else (name, None)


TIMELINE_TYPES = {"task.claimed", "patch.submitted", "patch.accepted", "patch.rejected", "sketch.proposed",
                  "sketch.accepted", "sketch.failed", "task.resolved", "task.created", "project.init", "agent.joined",
                  "path.proposed", "path.accepted"}


ROUTINE = {"task.heartbeat", "integrator.renew"}  # renewals every few seconds: kept out of the activity feed


def timeline(events: list[dict]) -> list[dict]:
    """The events that deserve a mark on the timeline (position = their index)."""
    st = S.build(events, trusted=True)  # already validated by the Source
    return [{"i": i + 1, **item(e, st)} for i, e in enumerate(events) if e["type"] in TIMELINE_TYPES]


def item(ev: dict, st: S.State, full: bool = False) -> dict:
    """An event ready for a timeline."""
    from .cli import event_text
    return {"id": ev["id"], "lamport": ev["lamport"], "ts": ev["ts"], "type": ev["type"], "name": ev["name"],
            "agent": agent_id(ev["author"]), "kind": ev.get("agent"), "task": ev.get("task"),
            "text": event_text(ev, st, full),
            **({"note": ev.get("text", "")} if ev["type"] == "task.note" else {})}


def history(events: list[dict], node: str, upto: int | None) -> list[dict]:
    evs = events if upto is None else events[:max(upto, 0)]
    st = S.build(evs, trusted=True)  # already validated by the Source
    if node.startswith("a-"):
        mine = [e for e in evs if agent_id(e["author"]) == node and e["type"] not in ROUTINE]
    else:
        patches = {p.id for p in st.patches.values() if p.task == node}
        mine = [e for e in evs if e.get("task") == node or e.get("patch") in patches]
    # The latest 80 events (short versions: a test's whole output doesn't fit in a timeline), and every note: a
    # task's conversation is read whole, so it doesn't share that budget.
    keep = {e["id"] for e in mine[-80:]} | {e["id"] for e in mine if e["type"] == "task.note"}
    return [item(e, st) for e in mine if e["id"] in keep]


def logo_dirs(repo: Path) -> list[Path]:
    """Where to look for logos, by priority: <repo>/.legion/logos, ~/.legion/logos (or LEGION_HOME),
    and the ones bundled with legion for agents missing from Lobe Icons (Muse, Instinct)."""
    from .identity import home
    return [repo / ".legion" / "logos", home() / "logos", Path(__file__).parent / "static" / "logos"]


LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg"}


def make_handler(source: Source):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # no line per request in the terminal
            pass

        def send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            upto = int(q["upto"][0]) if q.get("upto") and q["upto"][0].lstrip("-").isdigit() else None
            if url.path == "/":
                self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/state.json":
                data = snapshot(source.get(), upto)
                self.send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json")
            elif url.path == "/history.json" and q.get("node"):
                data = history(source.get(), q["node"][0], upto)
                self.send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json")
            elif url.path == "/timeline.json":
                data = timeline(source.get())
                self.send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json")
            elif url.path == "/logos.json":  # which logo files exist, so missing ones aren't requested
                kinds = sorted({f.stem for d in logo_dirs(source.repo) if d.is_dir()
                                for f in d.iterdir() if f.suffix in LOGO_TYPES})
                self.send(200, json.dumps(kinds).encode(), "application/json")
            elif url.path.startswith("/logo/"):
                kind = url.path[len("/logo/"):]
                if not kind.replace("-", "").replace("_", "").isalnum():
                    return self.send(404, b"", "text/plain")
                for d in logo_dirs(source.repo):
                    for ext, ctype in LOGO_TYPES.items():
                        f = d / f"{kind}{ext}"
                        if f.is_file():
                            return self.send(200, f.read_bytes(), ctype)
                self.send(404, b"", "text/plain")
            else:
                self.send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR (Python's default here) lets a second viewer bind a port that's already in use,
    # and requests then land on either one. Ask for the port exclusively, so a busy one is detected.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(repo: Path, port: int, interval: float, fetch: bool, open_browser: bool) -> int:
    source = Source(repo, interval, fetch)
    try:
        server = Server(("127.0.0.1", port), make_handler(source))  # this machine only
    except OSError:  # the port is taken (another viewer, perhaps): any free one will do
        server = Server(("127.0.0.1", 0), make_handler(source))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Viewer for {repo} at {url} (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
