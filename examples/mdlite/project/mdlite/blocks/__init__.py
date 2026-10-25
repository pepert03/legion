"""The block parser: Markdown text to the tree in mdlite/nodes.py. See "Blocks" in SPEC.md.

This package is yours to organise: split it into modules (headings, code, quotes, lists, tables…)
as you see fit, as long as parse_blocks stays here.
"""

from ..nodes import Document


def parse_blocks(text: str) -> Document:
    raise NotImplementedError
