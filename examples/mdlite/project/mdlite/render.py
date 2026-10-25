"""The tree to HTML. See "Rendering" in SPEC.md."""

from .inline import render_inline
from .nodes import Document


def render(doc: Document, inline=render_inline) -> str:
    raise NotImplementedError
