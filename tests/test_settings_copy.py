"""A clone's legion settings survive a wrapper that deletes the [legion] section of its git config."""
import subprocess

from legion import coordgit


def test_settings_lost_from_the_config_come_from_the_copy_and_the_environment_overrides_both(tmp_path, monkeypatch,
                                                                                            capsys):
    monkeypatch.delenv("LEGION_SETTINGS", raising=False)
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("config", "legion.transport", "radicle")
    g("config", "legion.livekey", "abc123")
    assert coordgit.setting(tmp_path, "legion.transport") == ["radicle"]
    assert (coordgit.common_dir(tmp_path) / coordgit.SECTION_COPY).is_file()
    g("config", "--remove-section", "legion")  # what the wrapper did
    g("config", "user.name", "someone")  # and the config changed again since
    assert coordgit.setting(tmp_path, "legion.livekey") == ["abc123"]
    assert "lost its [legion] section" in capsys.readouterr().err
    monkeypatch.setenv("LEGION_SETTINGS", "legion.transport=relay; legion.relays=wss://a wss://b")
    assert coordgit.setting(tmp_path, "legion.transport") == ["relay"]
    assert coordgit.setting(tmp_path, "legion.relays") == ["wss://a", "wss://b"]
    assert coordgit.setting(tmp_path, "legion.livekey") == ["abc123"]
    # A config that has its section again is what counts, and refreshes the copy.
    monkeypatch.delenv("LEGION_SETTINGS")
    g("config", "legion.livekey", "def456")
    assert coordgit.setting(tmp_path, "legion.livekey") == ["def456"]
    assert coordgit.setting(tmp_path, "legion.transport") == []


def test_a_lost_event_object_neither_stops_the_log_nor_stays_lost_when_the_relays_bring_it(tmp_path):
    import os
    from legion import events as E
    from legion.identity import Identity
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    ana = Identity.generate("ana")
    first = E.create(ana, "agent.joined", 1)
    second = E.create(ana, "agent.joined", 2)
    assert coordgit.add_events(tmp_path, ana, [first]) == 1
    sha = coordgit.list_files(tmp_path, coordgit.rev(tmp_path, coordgit.REF))[E.filename(first)]
    obj = tmp_path / ".git" / "objects" / sha[:2] / sha[2:]
    os.chmod(obj, 0o644)
    obj.unlink()  # the clone lost it
    assert coordgit.add_events(tmp_path, ana, [second]) == 1  # the log goes on, hole and all
    assert coordgit.add_events(tmp_path, ana, [first, second]) == 0  # the relays bring it again: written back
    p = subprocess.run(["git", "cat-file", "-e", sha], cwd=tmp_path)
    assert p.returncode == 0
