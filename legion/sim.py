"""Local simulation: an integrator and N agents, each with its own clone and identity.

Every clone stands in for a different machine; origin.git (or the peers, in p2p mode) is the transport.
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from . import coordgit
from . import state as S
from .ctx import Fail

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

PROMPT = ("You are {name}, an agent collaborating with other agents on the project in this directory. "
          "Read AGENTS.md and follow its protocol until it says you are done.")

# Non-interactive commands for each agent. {prompt} is replaced by the instruction above.
PRESETS = {
    "claude": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits",
               "--allowedTools", "Bash(python:*)", "Bash(git:*)", "Read", "Edit", "Write", "Glob", "Grep"],
    "codex": ["codex", "exec", "--full-auto", "{prompt}"],
    "opencode": ["opencode", "run", "{prompt}"],
}


def add_parser(sub) -> None:
    s = sub.add_parser("sim", help="local simulation with several agents")
    ssub = s.add_subparsers(dest="action", required=True)
    i = ssub.add_parser("init", help="create the integrator and the agents' clones")
    i.add_argument("dir")
    i.add_argument("--agents", type=int, default=3)
    i.add_argument("--example", default="textkit")
    i.add_argument("--force", action="store_true", help="replace a previous simulation in the same directory")
    i.add_argument("--remote", help="URL of an empty repository (on GitHub, for instance) to act as origin")
    i.add_argument("--people", default="ana,luis,marta",
                   help="people who own the agents, in turn: ana#1, luis#1, marta#1, ana#2…")
    i.add_argument("--p2p", action="store_true", help="no origin: every node reads from its peers")
    i.add_argument("--integrators", type=int, default=1,
                   help="how many integrators: the first leads, the others stand by to take over")
    i.add_argument("--topology", choices=["mesh", "ring"], default="mesh",
                   help="in p2p: mesh = everyone with everyone; ring = each node with its two neighbours")
    r = ssub.add_parser("run", help="start the integrator and the agents at once")
    r.add_argument("dir")
    r.add_argument("--agent", default="fake",
                   help="fake, claude, codex or opencode; separate with commas to mix them (claude,codex,fake)")
    r.add_argument("--agent-cmd", help="your own command for any other agent; {prompt} is the instruction")
    r.add_argument("--model", help="model for the claude agents")
    r.add_argument("--timeout", type=float, default=900)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--lease", type=int, default=15, help="seconds the integration lease lasts without renewal")
    r.add_argument("--kill-integrator", type=float, metavar="SECONDS",
                   help="kill the leading integrator after this many seconds, to watch a backup take over")
    r.add_argument("--as", dest="label_as", default="",
                   help="with fake agents: the kind they present as in events, in turn "
                        "(e.g. claude,codex,deepseek), to see the logos in legion viz")
    m = ssub.add_parser("summary", help="summary of a simulation")
    m.add_argument("dir")
    s.set_defaults(func=main)


def _env(home: Path, agent: str, model: str | None = None) -> dict:
    env = dict(os.environ)
    env["LEGION_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["LEGION_AGENT"] = agent
    if model:
        env["LEGION_MODEL"] = model
    return env


def _legion(cwd: Path, env: dict, *args: str) -> str:
    p = subprocess.run([sys.executable, "-m", "legion", *args], cwd=cwd, env=env,
                       capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise Fail(f"`legion {' '.join(args)}` failed: {p.stderr.strip()}")
    return p.stdout


def _git(cwd: Path, *args: str) -> None:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise Fail(f"`git {' '.join(args)}` failed: {p.stderr.strip()}")


def _clone(d: Path, origin, path: Path, name: str) -> None:
    _git(d, "clone", "-q", *coordgit.CLONE_CONFIG, str(origin), str(path))
    _git(path, "config", "user.name", name)
    _git(path, "config", "user.email", f"{name}@legion.local")


def _rmtree(path: Path) -> None:
    def unlock(func, p, _):  # git marks its objects read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=unlock)


def init(args) -> int:
    d = Path(args.dir).resolve()
    ex = EXAMPLES / args.example
    if not (ex / "project").is_dir():
        raise Fail(f"there is no example {args.example} in {EXAMPLES}")
    if d.exists() and any(d.iterdir()):
        if not (args.force and (d / "sim.json").exists()):
            raise Fail(f"{d} is not empty (--force replaces a previous simulation)")
        _rmtree(d)
    d.mkdir(parents=True, exist_ok=True)

    origin = None
    if args.p2p:
        if args.remote:
            raise Fail("--p2p and --remote don't go together: in p2p there is no common remote")
    elif args.remote:
        p = subprocess.run(["git", "ls-remote", args.remote], capture_output=True, encoding="utf-8", errors="replace")
        if p.returncode:
            raise Fail(f"can't reach {args.remote}: {p.stderr.strip()}")
        if p.stdout.strip():
            raise Fail(f"{args.remote} is not empty; the simulation needs a freshly created repository")
        origin = args.remote
    else:
        origin = d / "origin.git"
        _git(d, "init", "-q", "--bare", "-b", "main", str(origin))
        _git(origin, "config", "core.longpaths", "true")  # Windows: paths longer than 260 characters

    integ = d / "integrator"
    if args.p2p:
        _git(d, "init", "-q", "-b", "main", str(integ))
        for key, value in (("core.autocrlf", "false"), ("core.longpaths", "true"),
                           ("user.name", "integrator"), ("user.email", "integrator@legion.local")):
            _git(integ, "config", key, value)
    else:
        _clone(d, origin, integ, "integrator")
        _git(integ, "symbolic-ref", "HEAD", "refs/heads/main")
    shutil.copytree(ex / "project", integ, dirs_exist_ok=True)
    env = _env(d / "homes" / "integrator", "human")
    _legion(integ, env, "id", "new", "--name", "integrator", *(["--number", "1"] if args.integrators > 1 else []))
    _legion(integ, env, "init", "--project", args.example)
    _git(integ, "add", "-A")
    _git(integ, "commit", "-qm", "Initial project")
    if not args.p2p:
        _git(integ, "push", "-q", "-u", "origin", "main")
    _legion(integ, env, "main", "publish")
    _legion(integ, env, "task", "import", str(ex / "tasks.json"))
    if (ex / "solutions").is_dir():
        shutil.copytree(ex / "solutions", d / "_solutions")  # only the fake agents use them

    agents = []
    people = [p.strip() for p in args.people.split(",") if p.strip()] or ["ana"]
    for i in range(1, args.agents + 1):
        name = f"agent-{i}"  # folder name; the identity is person#number
        person, number = people[(i - 1) % len(people)], (i - 1) // len(people) + 1
        path = d / name
        _clone(d, integ if args.p2p else origin, path, name)
        if args.p2p:
            _git(path, "remote", "remove", "origin")  # the initial clone only copies the code
        _legion(path, _env(d / "homes" / name, "?"), "id", "new", "--name", person, "--number", str(number))
        agents.append({"name": name, "path": str(path), "home": str(d / "homes" / name),
                       "identity": f"{person}#{number}"})

    backups = []  # integrators 2..N: their own clone and identity, voted in by the first one
    for k in range(2, args.integrators + 1):
        name = f"integrator-{k}"
        path, home = d / name, d / "homes" / name
        _clone(d, integ if args.p2p else origin, path, name)
        if args.p2p:
            _git(path, "remote", "remove", "origin")
        _legion(path, _env(home, "human"), "id", "new", "--name", "integrator", "--number", str(k))
        backups.append({"name": name, "path": str(path), "home": str(home), "identity": f"integrator#{k}"})
    cfg = {"example": args.example, "mode": "p2p" if args.p2p else "origin", "origin": str(origin) if origin else None,
           "integrator": str(integ), "integrator_home": str(d / "homes" / "integrator"), "agents": agents,
           "integrators": [{"name": "integrator", "path": str(integ), "home": str(d / "homes" / "integrator")}] + backups}
    if args.p2p:
        nodes = [("integrator", integ, d / "homes" / "integrator")] + \
                [(b["name"], Path(b["path"]), Path(b["home"])) for b in backups] + \
                [(a["name"], Path(a["path"]), Path(a["home"])) for a in agents]
        links = _topology(len(nodes), args.topology)
        for i, j in links:
            _legion(nodes[i][1], _env(nodes[i][2], "?"), "peer", "add", nodes[j][0], str(nodes[j][1]))
        for _ in range(len(nodes)):  # a few gossip rounds so everyone has the tasks and main
            for _, path, home in nodes:
                _legion(path, _env(home, "?"), "status")
        cfg["topology"] = args.topology
        cfg["links"] = [[nodes[i][0], nodes[j][0]] for i, j in links]
    else:
        observer = d / "observer"  # read-only clone to follow progress without getting in the integrator's way
        _clone(d, origin, observer, "observer")
        cfg["observer"] = str(observer)
    if not args.p2p:
        for b in backups:  # so they can be found by name before the vote
            _legion(Path(b["path"]), _env(Path(b["home"]), "human"), "status")
    for b in backups:  # the first integrator votes the others in (with one integrator, one vote is a majority)
        key = json.loads(_legion(Path(b["path"]), _env(Path(b["home"]), "human"), "id", "show", "--json"))["author"]
        _legion(integ, env, "integrators", "add", key)
    if args.p2p and backups:
        for _ in range(2):  # gossip the votes around
            for _, path, home in nodes:
                _legion(path, _env(home, "?"), "status")
    (d / "sim.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    where = f"p2p ({args.topology})" if args.p2p else f"origin {origin}"
    print(f"Simulation created in {d} with {args.agents} agents, mode {where}.")
    print(f"Run it with: python -m legion sim run \"{d}\" --agent fake")
    return 0


def _topology(n: int, kind: str) -> list[tuple[int, int]]:
    """Who reads from whom. Node 0 is the integrator."""
    if kind == "mesh":
        return [(i, j) for i in range(n) for j in range(n) if i != j]
    if kind == "ring":  # each node only knows its two neighbours: the rest arrives by gossip
        return sorted({(i, (i + k) % n) for i in range(n) for k in (1, -1) if (i + k) % n != i})
    raise Fail(f"unknown topology: {kind}")


def _events(cfg: dict) -> list[dict]:
    if cfg.get("observer"):
        return coordgit.remote_events(cfg["observer"])
    # p2p: the integrator that has seen the most (the first one may have been killed)
    paths = [it["path"] for it in cfg.get("integrators") or [{"path": cfg["integrator"]}]]
    return max((coordgit.read_events(p) for p in paths), key=len)


def agent_command(kind: str, agent: dict, args, d: Path, index: int) -> list[str]:
    prompt = PROMPT.format(name=agent.get("identity", agent["name"]))
    if args.agent_cmd:
        return [part.replace("{prompt}", prompt) for part in shlex.split(args.agent_cmd)]
    if kind == "fake":
        if not (d / "_solutions").is_dir():  # kept out of the public repository, so agents can't copy them
            raise Fail(f"the fake agents need the example's prepared solutions ({EXAMPLES}/<example>/solutions), "
                       "which aren't in the repository; use a real agent or put them there")
        return [sys.executable, "-m", "legion.fake_agent", "--solutions", str(d / "_solutions"),
                "--seed", str(args.seed + index), "--timeout", str(args.timeout)]
    if kind not in PRESETS:
        raise Fail(f"unknown agent: {kind} (use fake, {', '.join(PRESETS)} or --agent-cmd)")
    preset = PRESETS[kind]
    exe = shutil.which(preset[0])
    if not exe:
        raise Fail(f"can't find `{preset[0]}` on the PATH")
    cmd = [exe] + [part.replace("{prompt}", prompt) for part in preset[1:]]
    if kind == "claude" and args.model:
        cmd += ["--model", args.model]
    return cmd


def run(args) -> int:
    d = Path(args.dir).resolve()
    cfg = json.loads((d / "sim.json").read_text(encoding="utf-8"))
    kinds = [k.strip() for k in args.agent.split(",") if k.strip()]
    labels = [x.strip() for x in args.label_as.split(",") if x.strip()]
    logs = d / "logs"
    logs.mkdir(exist_ok=True)
    start = time.time()

    open_files = []

    def spawn(cmd, cwd, env, name):
        f = open(logs / f"{name}.log", "w", encoding="utf-8")
        open_files.append(f)
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    integrators = cfg.get("integrators") or [{"name": "integrator", "path": cfg["integrator"], "home": cfg["integrator_home"]}]
    integ_procs = []
    for k, it in enumerate(integrators):
        ienv = _env(Path(it["home"]), "human")
        ienv["LEGION_LEASE"] = str(args.lease)
        proc = spawn([sys.executable, "-m", "legion", "integrate", "--loop", "2", "--timeout", str(args.timeout)],
                     it["path"], ienv, it["name"])
        integ_procs.append((it, ienv, proc))
        if k == 0:
            time.sleep(3)  # let the first one take the lease, so the order is predictable
    procs = {}
    for i, a in enumerate(cfg["agents"]):
        kind = "custom" if args.agent_cmd else kinds[i % len(kinds)]
        cmd = agent_command(kind, a, args, d, i)
        env = _env(Path(a["home"]), labels[i % len(labels)] if labels and kind == "fake" else kind, args.model)
        # Short claims: if an agent hangs, its task is open again soon.
        env.setdefault("LEGION_TTL", "90" if kind == "fake" else "900")
        procs[a["name"]] = (kind, spawn(cmd, a["path"], env, a["name"]))
        print(f"started {a.get('identity', a['name'])} ({kind})")
    print(f"{len(integ_procs)} integrator(s) running; logs in {logs}")
    killed = None

    last = 0.0
    try:
        while time.time() - start < args.timeout:
            alive = [n for n, (_, p) in procs.items() if p.poll() is None]
            if args.kill_integrator and not killed and time.time() - start > args.kill_integrator:
                killed = integ_procs[0]
                killed[2].kill()  # no clean exit: its lease is left to expire, like a machine that died
                print(f"[{int(time.time() - start):>4}s] killed {killed[0]['name']}; a backup should take over "
                      f"within {args.lease} s", flush=True)
            if time.time() - last > 10:
                st = S.build(_events(cfg))
                now = time.time()
                counts = Counter(st.status(t, now) for t in st.tasks)
                print(f"[{int(time.time() - start):>4}s] active agents: {len(alive)} · "
                      + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
                last = time.time()
            if not alive:
                break
            time.sleep(1)
    finally:
        for _, p in procs.values():
            if p.poll() is None:
                p.kill()
        for _, _, proc in integ_procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for f in open_files:
            f.close()
    # One last pass, by a surviving integrator, for patches that arrived at the end. Killed processes
    # leave their lease behind, so wait it out: that's the same takeover a real crash would need.
    survivor = next(x for x in integ_procs if x is not killed)
    if killed:
        time.sleep(args.lease + 1)
    with open(logs / f"{survivor[0]['name']}.log", "a", encoding="utf-8") as f:
        subprocess.run([sys.executable, "-m", "legion", "integrate", "--loop", "2", "--timeout", "8"],
                       cwd=survivor[0]["path"], env=survivor[1], stdout=f, stderr=subprocess.STDOUT, timeout=600)
    print(f"\nDuration: {time.time() - start:.0f} s")
    return summary(d)


def summary(d: Path) -> int:
    from . import trust
    from .cli import status_table
    cfg = json.loads((d / "sim.json").read_text(encoding="utf-8"))
    st = S.build(_events(cfg))
    now = time.time()
    print("\n" + status_table(st, now))

    per = {}
    for ev in st.events:
        who = ev["name"] + (f" ({ev['agent']})" if ev.get("agent") else "")
        row = per.setdefault(who, Counter())
        row[ev["type"]] += 1
    lost = Counter()
    by_author = {}
    for ev in st.events:
        if ev["type"] == "task.claimed":
            by_author[(ev["author"], ev["task"])] = ev
        elif ev["type"] == "task.released" and (ev["author"], ev["task"]) in by_author:
            claimed = by_author.pop((ev["author"], ev["task"]))
            if ev["ts"] - claimed["ts"] < 30:
                lost[ev["name"]] += 1
    by_agent, by_person = trust.compute(st, now)
    names = {e["author"]: e["name"] for e in st.events}
    score = {names.get(a, a): t.score for a, t in by_agent.items()}
    print("\nAgent                    claims      lost  patches  accepted  rejected  reports  trust")
    accepted = Counter(p.name for p in st.patches.values() if p.verdict == "accepted")
    rejected = Counter(p.name for p in st.patches.values() if p.verdict == "rejected")
    for who, row in sorted(per.items()):
        name = who.split(" (")[0]
        t = "-" if name not in score or name == "integrator" else score[name]
        print(f"{who:<24} {row['task.claimed']:>6}  {lost[name]:>8}  {row['patch.submitted']:>7}  "
              f"{accepted[name]:>8}  {rejected[name]:>8}  {row['attempt.reported']:>7}  {t:>5}")
    if by_person:
        print("\nTrust per person: " + ", ".join(f"{p} {t.score}" for p, t in
                                                 sorted(by_person.items(), key=lambda kv: -kv[1].score)))
    reasons = [p for p in st.patches.values() if p.verdict == "rejected"]
    if reasons:
        print("\nRejections:")
        for p in sorted(reasons, key=lambda p: p.lamport):
            print(f"  {st.tasks[p.task].key:<16} {p.name:<9} {p.reason.splitlines()[0]}")
    if st.ignored:
        print(f"\nIgnored events: {len(st.ignored)}")
    print(f"\nEvents: {len(st.events)} · exclusive tasks done: {'yes' if st.exclusive_done() else 'no'}")
    return 0 if st.exclusive_done() else 1


def main(args) -> int:
    if args.action == "init":
        return init(args)
    if args.action == "run":
        return run(args)
    return summary(Path(args.dir).resolve())
