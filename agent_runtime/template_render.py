"""Strict ``{{VAR}}`` template renderer for bootstrap-time file generation.

Why double-brace and not :class:`string.Template` / :meth:`str.format`:

The shipped templates contain literal single-brace tokens that are part of the
spec verbatim content (e.g. ``{auto · compile-wiki}`` in
``templates/meta/EVERGREEN.md.template``). A naive single-brace substituter
would either replace those tokens or raise ``KeyError``. We therefore match
ONLY ``{{NAME}}`` (double braces, ASCII letters/digits/underscore in NAME) and
pass any single-brace literal through verbatim.

Whitespace inside ``{{ }}`` is NOT permitted; ``{{ FOO }}`` is left as a
literal, not treated as a placeholder.
"""

from __future__ import annotations

import pathlib
import re
from typing import Mapping, Union

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

PathLike = Union[str, pathlib.Path]


class TemplateError(Exception):
    """Raised on template rendering failures (missing variable, etc.).

    Carries structured fields so callers (e.g. ``scripts/bootstrap.sh``)
    can react programmatically instead of grepping the message text.
    """

    def __init__(self, missing_vars: list[str], path: pathlib.Path) -> None:
        self.missing_vars = list(missing_vars)
        self.path = path
        super().__init__(
            f"missing variable: {', '.join(self.missing_vars)} in {path}"
        )


def render_template(path: PathLike, context: Mapping[str, object]) -> str:
    """Render ``path`` by substituting ``{{NAME}}`` with ``context[NAME]``.

    - Strictly matches ``{{NAME}}`` (double braces, ASCII letters/digits/underscore).
    - Single-brace ``{literal}`` content is preserved verbatim.
    - If any ``{{NAME}}`` placeholder remains unfilled (i.e. ``NAME`` not in
      ``context``), raises :class:`TemplateError` with ``missing_vars`` and
      ``path`` populated.
    - File is read as UTF-8.

    ``context`` values are coerced to ``str`` at substitution time so callers
    may pass ints, paths, etc. without manual conversion.
    """
    p = pathlib.Path(path)
    raw = p.read_text(encoding="utf-8")
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            if name not in missing:
                missing.append(name)
            return match.group(0)  # keep placeholder so caller can see it
        return str(context[name])

    rendered = _PLACEHOLDER.sub(_replace, raw)
    if missing:
        raise TemplateError(missing, p)
    return rendered
