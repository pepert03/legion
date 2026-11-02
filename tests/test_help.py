"""AGENTS.md is the work loop; the rest of the protocol is in topics agents read when they need one."""

import re

from legion import cli


def test_every_topic_agents_are_sent_to_exists_and_agents_md_stays_one_page(capsys):
    topics = dict(cli.help_topics())
    agents = (cli.TEMPLATES / "AGENTS.md").read_text(encoding="utf-8")
    code = (cli.TEMPLATES.parent / "cli.py").read_text(encoding="utf-8")
    named = set(re.findall(r"help (\w+)`", agents + code)) - {"topic"}
    assert named and named <= set(topics), named - set(topics)
    assert all((cli.HELP / f"{t}.md").exists() for t in topics)
    assert "{topics}" in agents and len(agents.split()) < 1200  # every agent reads it in every session
    assert cli.main(["help", "competitive"]) == 0 and "pool" in capsys.readouterr().out
