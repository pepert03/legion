"""The document tree that the block parser builds and the renderer turns into HTML.

This file is shared by every part of mdlite: don't change it. Text inside blocks (heading text, paragraph
text, table cells) is kept as raw Markdown; the renderer passes it through the inline formatter.
"""

from dataclasses import dataclass, field


@dataclass
class Heading:
    level: int  # 1..6
    text: str
    id: str | None = None  # set by toc.add_ids; rendered as <hN id="...">


@dataclass
class Paragraph:
    text: str  # its lines joined with "\n"


@dataclass
class CodeBlock:
    code: str  # verbatim; every line ends with "\n"
    lang: str | None = None


@dataclass
class ThematicBreak:
    pass


@dataclass
class BlockQuote:
    children: list = field(default_factory=list)


@dataclass
class ListItem:
    children: list = field(default_factory=list)


@dataclass
class List:
    ordered: bool
    items: list = field(default_factory=list)  # of ListItem
    start: int = 1  # ordered lists: the first item's number
    tight: bool = True  # no blank line between or inside its items


@dataclass
class Table:
    header: list  # of str
    align: list  # of "left" | "center" | "right" | None, one per column
    rows: list = field(default_factory=list)  # of lists of str, each as long as the header


@dataclass
class Document:
    children: list = field(default_factory=list)
