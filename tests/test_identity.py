"""Agents sharing a computer keep their identities apart: each repository is bound to its own."""

import subprocess

import pytest

from legion import identity
from legion.cli import main
from legion.identity import Identity


def repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "home"))
    return tmp_path


def legion(where, monkeypatch, *args):
    monkeypatch.chdir(where)
    return main(list(args))


def test_an_agent_creating_its_identity_doesnt_touch_the_integrators(machine, monkeypatch):
    integ, agent = repo(machine / "integrator"), repo(machine / "agent-clone")
    assert legion(integ, monkeypatch, "id", "new", "--name", "jose") == 0
    jose = Identity.load(repo=integ)
    # What Codex did: create its own identity, from its own clone, on the same computer.
    assert legion(agent, monkeypatch, "id", "new", "--name", "codex-pepe") == 0
    assert Identity.load(repo=integ).author == jose.author
    assert Identity.load(repo=agent).name == "codex-pepe"
    assert Identity.load(identity.home() / "identity.json").author == jose.author  # the default isn't replaced


def test_a_bound_repository_keeps_its_identity_unless_forced(machine, monkeypatch, capsys):
    r = repo(machine / "r")
    legion(r, monkeypatch, "id", "new", "--name", "ana")
    first = Identity.load(repo=r)
    assert legion(r, monkeypatch, "id", "new", "--name", "intruder") == 1
    assert "already works as ana" in capsys.readouterr().err
    assert Identity.load(repo=r).author == first.author
    assert legion(r, monkeypatch, "id", "new", "--name", "ana", "--number", "2", "--force") == 0
    assert Identity.load(repo=r).name == "ana#2"
    assert any(i.author == first.author for i in identity.all_identities())  # the old key is still there


def test_use_and_import(machine, monkeypatch, tmp_path):
    a, b = repo(machine / "a"), repo(machine / "b")
    legion(a, monkeypatch, "id", "new", "--name", "luis")
    luis = Identity.load(repo=a)
    assert legion(b, monkeypatch, "id", "use", "luis") == 0
    assert Identity.load(repo=b).author == luis.author
    outside = Identity.generate("marta")
    outside.save(tmp_path / "marta.json")
    assert legion(b, monkeypatch, "id", "import", str(tmp_path / "marta.json")) == 0
    assert legion(b, monkeypatch, "id", "use", "marta", "--force") == 0
    assert Identity.load(repo=b).author == outside.author


def test_saving_never_replaces_another_key(machine):
    one = Identity.generate("one")
    path = one.save()
    other = Identity.generate("other")
    with pytest.raises(FileExistsError):
        other.save(path)
    assert Identity.load(path).author == one.author


def test_an_unbound_repository_uses_the_default(machine):
    default = Identity.generate("old-style")
    default.save(identity.home() / "identity.json")
    assert Identity.load(repo=repo(machine / "plain")).author == default.author
