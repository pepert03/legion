import pytest


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch):
    """Commands sync by themselves in tests: no machine-wide sync process started behind their back, and the
    push they leave to a process of their own (radicle.kick_publish) runs at once, in this one."""
    from legion import radicle
    monkeypatch.setenv("LEGION_NO_DAEMON", "1")
    monkeypatch.setattr(radicle, "kick_publish", run_publish_now)


def run_publish_now(repo):
    from legion import coordgit
    from legion.identity import Identity
    coordgit.publish_radicle(repo, Identity.load(repo=repo))
