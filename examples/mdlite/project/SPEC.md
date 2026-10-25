# mdlite: the Markdown it understands

mdlite turns a subset of Markdown (close to CommonMark, with GitHub's tables and strikethrough) into
HTML. The tests in `tests/` are the contract; this page explains them. When this page and a test
disagree, the test wins.

```
text --blocks.parse_blocks--> Document --render.render--> HTML
                                           \--inline.render_inline (for the text inside blocks)
```

The pieces only meet through the tree in `mdlite/nodes.py`, which nobody changes.

## Blocks: `mdlite.blocks.parse_blocks(text) -> Document`

Before anything else, `\r\n` and `\r` become `\n`, and each tab becomes 4 spaces. A *blank line* is
empty or only spaces. Up to 3 spaces of indentation are allowed before a block marker.

- **ATX heading**: 1 to 6 `#`, then a space (or the end of the line), then the text. A closing run of
  `#` preceded by a space is removed. The text is stripped. `####### x` (7) is a paragraph.
  `#hashtag` (no space) is a paragraph.
- **Thematic break**: 3 or more `-`, `*` or `_` (all the same character), optionally with spaces
  between them, and nothing else on the line. It can interrupt a paragraph.
- **Fenced code**: a line starting with 3 or more backticks or 3 or more `~`. The rest of the line,
  stripped, is the info string; its first word is `lang` (None if empty). Content lines are taken
  verbatim, except that as many spaces as the opening fence was indented are removed from each. The
  block ends at a line that is only a fence of the same character, at least as long as the opening
  one (up to 3 spaces before it), or at the end of the input. It can interrupt a paragraph.
- **Indented code**: lines indented by 4 or more spaces, **not** in the middle of a paragraph.
  4 spaces are removed from each line. Blank lines inside are kept (as empty lines) if more indented
  lines follow; blank lines at the end are dropped. `lang` is None.
- In a `CodeBlock`, `code` is the lines joined, each followed by `"\n"`.
- **Block quote**: consecutive lines starting with `>` (after up to 3 spaces). The `>` and one space
  after it, if there is one, are removed, and what remains is parsed again as blocks (so quotes can
  hold lists, code or other quotes). A line without `>` ends the quote: there is no lazy continuation.
  The quote can interrupt a paragraph.
- **Lists**: an item starts with a marker followed by at least one space: `-`, `+` or `*` (bullet), or
  1 to 9 digits followed by `.` or `)` (ordered). The item's content column is where the text after
  the marker and its spaces begins. Following lines indented by at least that column belong to the
  item, with that indentation removed; together with the first line they are parsed again as blocks
  (so items nest lists, code or quotes). Blank lines inside an item are fine if an indented line
  follows.
  - Consecutive items of the same kind form one list. A different bullet character, or switching
    between `.` and `)`, starts a new list.
  - `start` is the first item's number (`007.` → 7).
  - The list is **tight** if no blank line separates two items and no item contains a blank line
    between two of its blocks; otherwise it is loose.
  - A list item can interrupt a paragraph.
  - A line indented less than the content column that isn't a new item ends the list (no lazy
    continuation).
- **Table**: a line with at least one `|`, followed by a delimiter row: cells made of `-` (at least
  one), with an optional `:` at either end, separated by `|`. Leading and trailing `|` are optional on
  every row. The header and the delimiter row must have the same number of cells, otherwise these
  lines are a paragraph. Alignment: `:--` left, `:-:` center, `--:` right, `---` None. Body rows
  follow until a blank line or a line without `|`. Cells are stripped; `\|` is a literal `|` inside a
  cell. Body rows are padded with `""` or cut to the header's length. A table can't interrupt a
  paragraph.
- **Paragraph**: anything else. Consecutive lines that don't start another block (one that can
  interrupt a paragraph) belong to it. Each line has its leading spaces removed; the last line is
  also stripped at the end. `text` is the lines joined with `"\n"`.

## Inline: `mdlite.inline.render_inline(text) -> str`

All text is HTML-escaped (`&`, `<`, `>`, `"`) with `mdlite.html.escape`. Raw HTML is not supported:
it comes out escaped. In order of precedence:

- **Backslash escapes**: `\` before an ASCII punctuation character makes it literal. Before anything
  else, the backslash stays.
- **Code spans**: a run of N backticks up to the next run of exactly N backticks. Inside, line
  endings become spaces; if the content starts and ends with a space and isn't only spaces, one space
  is removed from each end. Nothing is formatted inside; it's escaped. `<code>...</code>`. A run
  without a closing run is literal backticks.
- **Autolinks**: `<http://...>` or `<https://...>` (no spaces inside) → `<a href="URL">URL</a>`.
- **Images**: `![alt](src)` or `![alt](src "title")` → `<img src="src" alt="alt" />`, with
  ` title="title"` before ` />` if there is a title. `alt` is the bracket text without formatting
  (backslash escapes are applied, then it's escaped).
- **Links**: `[text](url)` or `[text](url "title")` → `<a href="url">text</a>` (with ` title="..."`
  after href if there is a title). The text is formatted (emphasis, code…) but can't contain another
  link; brackets in it must be balanced. The url has no spaces; `<` and `>` around it are removed.
  Attribute values are escaped. Without `(...)` right after `]`, brackets are literal.
- **Strikethrough**: `~~text~~` → `<del>text</del>`.
- **Emphasis**: `**text**` and `__text__` → `<strong>`; `*text*` and `_text_` → `<em>`.
  - An opening delimiter must be followed by a non-space character, a closing one preceded by one.
  - `_` doesn't open or close in the middle of a word (`snake_case_name` stays as it is); `*` does.
  - They nest: `**a *b* c**`, `*a **b** c*`, and `***x***` → `<em><strong>x</strong></em>`.
  - Unmatched delimiters are literal.
- **Line breaks**: two or more spaces before a line ending, or a backslash right before it, →
  `<br />` followed by the line ending. Otherwise a line ending stays `\n`, and the spaces before and
  after it are removed.

## Rendering: `mdlite.render.render(doc, inline=render_inline) -> str`

`inline` turns block text into HTML; tests pass their own to check the renderer alone.

| Node | HTML |
|---|---|
| Heading | `<h2>TEXT</h2>`, `<h2 id="ID">TEXT</h2>` if it has an id |
| Paragraph | `<p>TEXT</p>` |
| CodeBlock | `<pre><code>CODE</code></pre>`, `<pre><code class="language-LANG">CODE</code></pre>` with a lang; the code is escaped, not formatted |
| ThematicBreak | `<hr />` |
| BlockQuote | `<blockquote>\nCHILDREN\n</blockquote>` |
| List | `<ul>\nITEMS\n</ul>`; `<ol>\nITEMS\n</ol>`; `<ol start="3">` when start isn't 1 |
| ListItem | `<li>CHILDREN</li>` |
| Table | see below |

- Sibling blocks, and the children of quotes and list items, are joined with `"\n"`.
- In a **tight** list, an item's paragraphs are rendered as their bare text, without `<p>`. In a loose
  one, with `<p>`. So a tight item with a nested list is `<li>text\n<ul>\n...\n</ul></li>`.
- An empty list item is `<li></li>`; an empty quote is `<blockquote>\n</blockquote>`.
- A table:
  ```
  <table>
  <thead>
  <tr>
  <th>A</th>
  <th style="text-align: center">B</th>
  </tr>
  </thead>
  <tbody>
  <tr>
  <td>1</td>
  <td style="text-align: center">2</td>
  </tr>
  </tbody>
  </table>
  ```
  Without body rows there is no `<tbody>` part.
- The whole document is its blocks joined with `"\n"`, plus a final `"\n"` if there is any block.

## Table of contents: `mdlite.toc`

- `slugify(text) -> str`: lowercase; formatting characters (`*`, `_`, `` ` ``, `~`) removed; anything
  that isn't a letter, digit, space or `-` removed; spaces become `-`. `"Hello, *World*!"` →
  `"hello-world"`. An empty result becomes `"section"`.
- `add_ids(doc) -> list[tuple[int, str, str]]`: gives every **top-level** heading (not those inside
  quotes or lists) an id from its text, and returns `(level, text, id)` for each, in order. Repeated
  ids get `-1`, `-2`… (`intro`, `intro-1`, `intro-2`); a suffixed id must not clash with an existing one.
- `toc_html(entries, inline=render_inline) -> str`: nested `<ul>` lists, one `<li><a href="#ID">TEXT</a>`
  per heading, where a heading deeper than the previous one opens a nested list inside the previous
  item (one level of nesting however big the jump), and a shallower one closes lists until its level.
  Lines are joined with `"\n"`; no entries → `""`. See `tests/test_toc.py` for the exact shape.

## Command line: `python -m mdlite FILE [-o OUT] [--toc] [--title TITLE]`

- Reads FILE, or standard input if FILE is `-`, and writes the HTML to OUT or to standard output.
  Always UTF-8, whatever the system's default encoding (on Windows it usually isn't), and with `\n`
  line endings.
- `--toc`: gives headings ids and puts `<nav class="toc">\nTOC\n</nav>\n` before the body.
- `--title`: wraps everything in a full page:
  `<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n<title>TITLE</title>\n</head>\n<body>\nBODY</body>\n</html>\n`
  (the title escaped).
- A FILE that doesn't exist: a message on standard error and exit code 2.
- `mdlite.to_html(text, toc=False)` does the same as the command without `--title`.
