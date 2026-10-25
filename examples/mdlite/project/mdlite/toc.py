"""Heading ids and the table of contents. See "Table of contents" in SPEC.md."""

from .inline import render_inline
from .nodes import Document


def slugify(text: str) -> str:
    raise NotImplementedError


def add_ids(doc: Document) -> list:
    raise NotImplementedError


def toc_html(entries: list, inline=render_inline) -> str:
    raise NotImplementedError
