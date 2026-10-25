"""Inline formatting: the text inside a block to HTML. See "Inline" in SPEC.md.

This package is yours to organise: split it into modules (escapes and code spans, emphasis, links…)
as you see fit, as long as render_inline stays here.
"""


def render_inline(text: str) -> str:
    raise NotImplementedError
