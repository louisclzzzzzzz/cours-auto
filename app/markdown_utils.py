"""Traitements Markdown : titre principal (`# CM 3 – Titre`) des cours récupérés depuis Drive."""

from __future__ import annotations

import re

FENCE_RE = re.compile(r"^\s*(```|~~~)")
H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")


def split_prefix(line: str) -> tuple[str, str]:
    """Sépare le préfixe (indentation + marqueurs de citation) du contenu."""
    m = re.match(r"^((?:[ \t]*>[ ]?)*[ \t]*)", line)
    prefix = m.group(1) if m else ""
    return prefix, line[len(prefix):]


def iter_lines_with_code_state(md: str):
    """Itère (ligne, dans_un_bloc_de_code)."""
    in_code = False
    for line in md.split("\n"):
        if FENCE_RE.match(split_prefix(line)[1]):
            yield line, True
            in_code = not in_code
            continue
        yield line, in_code


def extract_title(md: str) -> str | None:
    for line, in_code in iter_lines_with_code_state(md):
        if in_code:
            continue
        m = H1_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return None


def replace_title(md: str, new_h1: str) -> str:
    """Remplace (ou ajoute) le titre H1 principal."""
    lines = md.split("\n")
    for i, (line, in_code) in enumerate(iter_lines_with_code_state(md)):
        if not in_code and H1_RE.match(line.strip()):
            lines[i] = f"# {new_h1}"
            return "\n".join(lines)
        if line.strip() and not in_code:
            break
    return f"# {new_h1}\n\n" + md.lstrip()
