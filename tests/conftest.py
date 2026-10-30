import pytest


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch):
    """Commands sync by themselves in tests: no machine-wide sync process started behind their back."""
    monkeypatch.setenv("LEGION_NO_DAEMON", "1")
