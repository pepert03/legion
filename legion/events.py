"""Signed, immutable events: the only way to change the shared state."""

import hashlib
import json
import os
import time

from .identity import Identity, verify

TYPES = {
    "project.init",
    "project.described",
    "agent.joined",
    "task.created",
    "task.claimed",
    "task.heartbeat",
    "task.released",
    "attempt.reported",
    "task.note",
    "patch.submitted",
    "patch.accepted",
    "patch.rejected",
    "digest.published",
    "main.updated",
    "sketch.proposed",
    "path.proposed",
    "path.accepted",
    "path.rejected",
    "sketch.accepted",
    "sketch.rejected",
    "sketch.failed",
    "task.resolved",
    "integrator.vote",
    "integrator.lease",
    "integrator.renew",
    "integrator.release",
}
INTEGRATOR_ONLY = {"project.described", "task.created", "patch.accepted", "patch.rejected", "digest.published", "main.updated",
                   "sketch.accepted", "sketch.rejected", "sketch.failed", "task.resolved",
                   "path.accepted", "path.rejected",
                   "integrator.vote", "integrator.lease", "integrator.renew", "integrator.release"}
MAX_NOTES = 2000
MAX_DESCRIPTION = 4000
RESERVED = {"type", "author", "name", "lamport", "ts", "agent", "model", "id", "sig"}


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _body(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in ("id", "sig")}


def detect_agent() -> str | None:
    """Which coding agent is running legion, from the variables it sets in its shells (when LEGION_AGENT isn't
    set: agents forget it)."""
    env = os.environ
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    for prefix, kind in (("CODEX_", "codex"), ("OPENCODE", "opencode"), ("GEMINI_CLI", "gemini"),
                         ("CURSOR_", "cursor"), ("CLINE_", "cline"), ("GOOSE_", "goose")):
        if any(k.startswith(prefix) for k in env):
            return kind
    return None


def create(ident: Identity, type: str, lamport: int, **payload) -> dict:
    ev = {"type": type, "author": ident.author, "name": ident.name, "lamport": lamport, "ts": round(time.time(), 3)}
    for env, field in (("LEGION_AGENT", "agent"), ("LEGION_MODEL", "model")):
        if os.environ.get(env):
            ev[field] = os.environ[env]
    if "agent" not in ev and (kind := detect_agent()):
        ev["agent"] = kind
    clash = RESERVED & set(payload)
    if clash:  # a payload field must never overwrite who signed or when
        raise ValueError(f"payload fields clash with the event header: {', '.join(sorted(clash))}")
    ev.update({k: v for k, v in payload.items() if v is not None})
    body = canonical(ev)
    ev["id"] = hashlib.sha256(body).hexdigest()[:16]
    ev["sig"] = ident.sign(body)
    return ev


def check(ev: dict) -> str | None:
    """Return why the event is invalid, or None."""
    if not isinstance(ev, dict) or ev.get("type") not in TYPES:
        return "unknown event type"
    if not isinstance(ev.get("lamport"), int) or not isinstance(ev.get("ts"), (int, float)):
        return "missing lamport or ts"
    body = canonical(_body(ev))
    if hashlib.sha256(body).hexdigest()[:16] != ev.get("id"):
        return "the id does not match the content"
    if not verify(ev.get("author"), body, ev.get("sig")):
        return "invalid signature"
    return None


def filename(ev: dict) -> str:
    return f"{ev['lamport']:08d}-{ev['id']}.json"


def encode(ev: dict) -> bytes:
    return canonical(ev)
