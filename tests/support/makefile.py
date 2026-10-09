"""Read recipes out of the repository ``Makefile`` for static checks."""

import re


def recipe(text: str, target: str) -> list[str]:
    """Return the recipe lines of ``target`` in the Makefile ``text``.

    Lines are returned in order with their leading tab removed, one entry per physical line
    (a backslash continuation is not joined). Blank lines and comment lines inside the recipe
    are skipped; the recipe ends at the first other line that does not start with a tab.

    Raises:
        KeyError: ``text`` has no rule for ``target``.

    """
    rule = re.compile(rf"^{re.escape(target)}\s*:(?!=)")
    lines = text.splitlines()
    start = next((n for n, line in enumerate(lines) if rule.match(line)), None)
    if start is None:
        raise KeyError(target)
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("\t"):
            body.append(line[1:])
        elif line.strip() and not line.lstrip().startswith("#"):
            break
    return body
