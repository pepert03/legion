import pytest


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch, tmp_path):
    """Commands sync by themselves in tests: no machine-wide sync process started behind their back, and the
    push they leave to a process of their own (radicle.kick_publish) runs at once, in this one, as the command's
    identity."""
    from legion import coordgit, ctx, radicle
    monkeypatch.setenv("LEGION_NO_DAEMON", "1")
    # A home of its own for every test: left at the person's, a test read the machine's real sync process from it
    # (and, playing Linux on Windows, killed it: see radicle.pid_alive) and kept its identities there.
    monkeypatch.setenv("LEGION_HOME", str(tmp_path / "legion-home"))
    # The machine's check slots are shared by every legion on it: a test's checks don't wait for an agent's.
    monkeypatch.setenv("LEGION_SLOTS", str(tmp_path / "check-slots"))
    monkeypatch.setenv("LEGION_LIVE", "off")  # nor do they publish to the public relays (test_live has its own)
    monkeypatch.setenv("LEGION_LAN", "0")  # nor do nodes of tests listen on the network (see tunnel.lan_allowed)
    # run_publish sets it for its process, which a test is too: it carried over to the tests after it.
    monkeypatch.setattr(radicle, "ANNOUNCE_INLINE", False)
    monkeypatch.setattr(ctx.Ctx, "_publish",
                        lambda self, wait=False: coordgit.publish_radicle(self.repo, self.ident) or True)
