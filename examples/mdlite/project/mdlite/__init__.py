"""mdlite: a small Markdown to HTML converter. See SPEC.md."""

from . import toc as _toc
from .blocks import parse_blocks
from .inline import render_inline
from .render import render


def to_html(text: str, toc: bool = False) -> str:
    doc = parse_blocks(text)
    entries = _toc.add_ids(doc) if toc else None
    body = render(doc, inline=render_inline)
    if toc:
        body = f'<nav class="toc">\n{_toc.toc_html(entries)}\n</nav>\n' + body
    return body
