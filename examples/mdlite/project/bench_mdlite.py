"""How long mdlite takes to convert a large document (the best of 3 runs). Prints `seconds: N`."""

import time

from mdlite import to_html

SECTION = """\
## Section {n}

Some *emphasis*, some **strong text**, `inline code` and a [link](https://example.com/{n} "Title {n}").
A second line with ~~struck~~ words and an image ![alt {n}](/img/{n}.png).

- item one with _underscores_ and snake_case_names
- item two
  1. nested *one*
  2. nested **two**
- item three

> A quote with **bold** and a list:
> - a
> - b

```python
def f{n}(x):
    return x * {n}
```

| Name | Value | Note |
|:-----|------:|:----:|
| a{n} | {n} | *x* |
| b{n} | {n}0 | `y` |

---
"""

DOC = "# Benchmark\n\n" + "\n".join(SECTION.format(n=n) for n in range(400))

best = float("inf")
for _ in range(3):
    start = time.perf_counter()
    to_html(DOC, toc=True)
    best = min(best, time.perf_counter() - start)
print(f"seconds: {best:.4f}")
