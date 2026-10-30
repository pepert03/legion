import pytest


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch):
    """Commands sync by themselves in tests: no machine-wide sync process started behind their back, and the
    push they leave to a process of their own (radicle.kick_publish) runs at once, in this one, as the command's
    identity."""
    from legion import coordgit, ctx
    monkeypatch.setenv("LEGION_NO_DAEMON", "1")
    monkeypatch.setattr(ctx.Ctx, "_publish", lambda self: coordgit.publish_radicle(self.repo, self.ident))
