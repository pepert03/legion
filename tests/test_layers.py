"""Each part does one thing: the core coordinates (events, state, the commands on tasks), the transport carries.
Only transport.py, the network commands (netcli.py) and coordgit's transport operations meet Radicle, its node,
the tunnels and this machine's sync process; a change there can't break how tasks are decided, and back."""

import ast
from pathlib import Path

import legion

NETWORK = {"radicle", "tunnel", "reach", "netcheck", "bridge", "daemon"}
CORE = ["state", "events", "identity", "trust", "ctx", "integrator", "viz", "locks"]


def imports(module: str) -> set[str]:
    tree = ast.parse((Path(legion.__file__).parent / f"{module}.py").read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            found |= {node.module.split(".")[0]} if node.module else {a.name for a in node.names}
    return found


def test_the_core_doesnt_reach_the_network_but_through_the_transport():
    for module in CORE:
        assert not imports(module) & NETWORK, (module, imports(module) & NETWORK)
    assert "transport" in imports("ctx")


def test_the_network_commands_run_on_their_own_too(capsys):
    from legion import netcli
    try:
        netcli.main(["daemon", "--help"])
    except SystemExit as e:
        assert e.code == 0
    assert "legion-net daemon" in capsys.readouterr().out
