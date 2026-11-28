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
