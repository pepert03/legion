"""How each coding agent takes up its last session again with a new message, and how it's told of one mid-session.

Within one harness, a subagent that stopped is started again with a message and still knows what it was doing. For
that across harnesses, `legion run` starts the agent's command again so that it continues its last session in this
clone (one agent works in a clone, so its last session there is its own), with the message as the prompt:

    claude -p "..."            ->  claude -p "<message>" --continue
    codex exec "..."           ->  codex exec resume --last "<message>"
    opencode run "..."         ->  opencode run "<message>" --continue
    gemini -p "..."            ->  gemini -p "<message>" -r latest
    agent -p "..." (Cursor)    ->  agent -p "<message>" --continue

Any other command, or one whose prompt can't be told from its flags: `legion run --resume "<command with
{message}>"` says how; without it the agent is started as ever, and reads the message in its inbox.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


def kind(exe: str) -> str:
    return Path(exe).stem.lower()


def prompt_index(argv: list[str]) -> int | None:
    """Where the prompt is among the arguments: the one with the most words, three at least (a flag's value is
    a word or two)."""
    best, at = 2, None
    for i, a in enumerate(argv[1:], 1):
        if not a.startswith("-") and len(a.split()) > best:
            best, at = len(a.split()), i
    return at


def resume(argv: list[str], message: str, template: str | None = None) -> list[str] | None:
    """The command that continues this agent's last session with `message`, or None when there's no telling how."""
    if template:
        return [part.replace("{message}", message) for part in shlex.split(template, posix=os.name != "nt")]
    i = prompt_index(argv)
    if i is None:
        return None
    said = argv[:i] + [message] + argv[i + 1:]
    name = kind(argv[0])
    if name == "claude":
        return said if {"--continue", "-c", "--resume", "-r"} & set(said) else said + ["--continue"]
    if name == "codex":
        if "exec" not in said[:i]:
            return None
        e = said.index("exec")
        return said if "resume" in said else said[:e + 1] + ["resume", "--last"] + said[e + 1:]
    if name == "opencode":
        return said if {"--continue", "-c", "--session", "-s"} & set(said) else said + ["--continue"]
    if name == "gemini":
        return said if {"-r", "--resume"} & set(said) else said + ["-r", "latest"]
    if name in ("agent", "cursor-agent"):
        return said if {"--continue", "--resume"} & set(said) else said + ["--continue"]
    return None


# Claude Code runs these commands at points of a session (its hooks) and hands the agent what they print: after
# each tool call what just came for it, and when it's about to stop, what came meanwhile, so it goes on instead.
# `inbox --hook` answers in the shape each of those points expects, and nothing at all when nothing came.
CLAUDE_HOOKS = ("PostToolUse", "UserPromptSubmit", "Stop")


def claude_hooks(command: str | None = None) -> dict:
    command = command or f'"{Path(sys.executable).as_posix()}" -m legion inbox --hook'
    return {event: [{"hooks": [{"type": "command", "command": command, "timeout": 20}]}] for event in CLAUDE_HOOKS}


WAKE_FOR = 540  # seconds an idle session's hook waits for something to come (a hook may run ten minutes)


def install_claude(repo, wake: bool = False) -> Path:
    """Add legion's hooks to this clone's own Claude Code settings (.claude/settings.local.json, which git
    doesn't track), keeping whatever else is there. The file. `wake`: also a hook that stays in the background
    after the session stops and wakes it when something comes (Claude Code's asyncRewake: a hook that ends with
    exit code 2 starts the idle session again with what it printed)."""
    path = Path(repo) / ".claude" / "settings.local.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    hooks = settings.setdefault("hooks", {})
    for event, entries in claude_hooks().items():
        kept = [e for e in hooks.get(event) or []
                if not any("legion inbox --hook" in str(h.get("command")) or "legion wait --max" in str(h.get("command"))
                           for h in e.get("hooks") or [])]
        hooks[event] = kept + entries
    if wake:
        command = f'"{Path(sys.executable).as_posix()}" -m legion wait --max {WAKE_FOR} --rewake'
        hooks["Stop"] = hooks["Stop"] + [{"hooks": [{"type": "command", "command": command, "asyncRewake": True,
                                                     "timeout": WAKE_FOR + 60}]}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    # Never part of a patch: a file outside the task's zone gets one rejected.
    p = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo,
                       capture_output=True, text=True)
    if p.returncode == 0:
        exclude = Path(p.stdout.strip()) / "info" / "exclude"
        try:
            have = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if ".claude/settings.local.json" not in have:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text(have + ("" if have.endswith("\n") or not have else "\n")
                                   + ".claude/settings.local.json\n", encoding="utf-8")
        except OSError:
            pass
    return path


def hook_answer(event: str, text: str) -> str:
    """What a hook prints for Claude Code (and the harnesses that took its format) to hand `text` to the agent."""
    if event in ("Stop", "SubagentStop"):
        return json.dumps({"decision": "block", "reason": text})
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})
