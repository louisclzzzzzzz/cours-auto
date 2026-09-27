"""Traitements Markdown.

- `normalize_course_markdown` : nettoyage de la sortie du LLM (formules, tableaux…).
- `to_notion_markdown` : adaptation au « Markdown enrichi » de Notion (testé sur l'API 2026-03-11) :
  * un bloc `$$ … $$` sur une seule ligne est mal interprété par Notion → toujours sur 3 lignes ;
  * plusieurs lignes `>` deviennent des blocs citation séparés → les encadrés deviennent des callouts.
- `notion_to_standard` : conversion inverse de ce que renvoie `GET /v1/pages/:id/markdown`
  (maths `$`…`$`, `<callout>`, `<table>`, échappements…) pour l'import des annotations.
"""

from __future__ import annotations

import html
import re

FENCE_RE = re.compile(r"^\s*(```|~~~)")
H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
# Maths en ligne : $…$ sans espace juste après l'ouverture ni juste avant la fermeture.
INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?!\s|\$)((?:\\.|[^$\\\n])+?)(?<!\s)\$(?!\$)")
INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")


# --- Outils génériques ------------------------------------------------------------------------

def split_prefix(line: str) -> tuple[str, str]:
    """Sépare le préfixe (indentation + marqueurs de citation) du contenu."""
    m = re.match(r"^((?:[ \t]*>[ ]?)*[ \t]*)", line)
    prefix = m.group(1) if m else ""
    return prefix, line[len(prefix):]


def _segments_outside_code_math(text: str) -> list[tuple[bool, str]]:
    """Découpe une ligne en morceaux (protégé, texte) : code en ligne et maths sont protégés."""
    out: list[tuple[bool, str]] = []
    pos = 0
    pattern = re.compile(INLINE_CODE_RE.pattern + "|" + r"\$\$.+?\$\$" + "|" + INLINE_MATH_RE.pattern)
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def map_outside_code_math(text: str, fn) -> str:
    return "".join(seg if protected else fn(seg) for protected, seg in _segments_outside_code_math(text))


def iter_lines_with_code_state(md: str):
    """Itère (ligne, dans_un_bloc_de_code)."""
    in_code = False
    for line in md.split("\n"):
        if FENCE_RE.match(split_prefix(line)[1]):
            yield line, True
            in_code = not in_code
            continue
        yield line, in_code


# --- Normalisation de la sortie du LLM --------------------------------------------------------

def strip_wrapping_fence(md: str) -> str:
    text = md.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", text, re.S)
    return m.group(1) if m else text


def normalize_display_math(md: str) -> str:
    """Chaque bloc `$$` doit avoir ses délimiteurs seuls sur leur ligne."""
    out: list[str] = []
    in_math = False
    for line, in_code in iter_lines_with_code_state(md):
        if in_code and not in_math:
            out.append(line)
            continue
        prefix, body = split_prefix(line)
        b = body.strip()
        if not in_math:
            single = re.fullmatch(r"\$\$(.+?)\$\$", b)
            if single and "$$" not in single.group(1):
                out += [prefix + "$$", prefix + single.group(1).strip(), prefix + "$$"]
                continue
            if b.startswith("$$") and b.count("$$") == 1:
                in_math = True
                out.append(prefix + "$$")
                rest = b[2:].strip()
                if rest:
                    out.append(prefix + rest)
                continue
            out.append(line)
        else:
            if "$$" in b:
                before, after = b.split("$$", 1)
                if before.strip():
                    out.append(prefix + before.strip())
                out.append(prefix + "$$")
                if after.strip():
                    out.append(prefix + after.strip())
                in_math = False
            else:
                out.append(line)
    return "\n".join(out)


def fix_table_math_pipes(md: str) -> str:
    """Dans une ligne de tableau, `|` casse les cellules : on le remplace par \\vert dans les formules."""

    def fix_math(m: re.Match) -> str:
        return m.group(0).replace(r"\|", r"\Vert ").replace("|", r"\vert ")

    out = []
    for line, in_code in iter_lines_with_code_state(md):
        if not in_code and line.lstrip().startswith("|"):
            line = INLINE_MATH_RE.sub(fix_math, line)
        out.append(line)
    return "\n".join(out)


_MATHY = re.compile(r"[\\^_={}+<>]|\d\s*[-*/]\s*\d")


def _map_outside_inline_code(text: str, fn) -> str:
    out, pos = [], 0
    for m in INLINE_CODE_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def convert_latex_delimiters(md: str) -> str:
    """`\\( … \\)` → `$…$` et `\\[ … \\]` → `$$…$$` (certains modèles ignorent la consigne)."""

    def fix(seg: str) -> str:
        seg = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1).strip() + "$", seg)
        # Bloc sur une ligne : seulement si le contenu ressemble à des maths (pas « \[passage peu clair\] »).
        seg = re.sub(r"\\\[(.+?)\\\]", lambda m: "$$" + m.group(1).strip() + "$$" if _MATHY.search(m.group(1)) else m.group(0), seg)
        return seg

    out = []
    for line, in_code in iter_lines_with_code_state(md):
        if in_code:
            out.append(line)
            continue
        prefix, body = split_prefix(line)
        if body.strip() in ("\\[", "\\]"):  # bloc sur plusieurs lignes
            out.append(prefix + "$$")
            continue
        out.append(_map_outside_inline_code(line, fix))
    return "\n".join(out)


_BOX_LABEL_RE = re.compile(r"^>\s*(?:\S+\s+)?\*\*[^*]+\*\*\s*:?\s*$")


def fix_lazy_quotes(md: str) -> str:
    """Encadré dont seul le libellé est préfixé par `>` (« > **Définition** : » puis texte nu) :
    on rattache les lignes suivantes à l'encadré jusqu'à la prochaine ligne vide."""
    out: list[str] = []
    extending = False
    for line, in_code in iter_lines_with_code_state(md):
        if extending:
            if not line.strip() or HEADING_RE.match(line.strip()):
                extending = False
            elif not line.lstrip().startswith(">"):
                out.append("> " + line)
                continue
        if not in_code and _BOX_LABEL_RE.match(line.strip()):
            extending = True
        out.append(line)
    return "\n".join(out)


_BOX_START_RE = re.compile(
    r"^\*\*(D[ée]finition|Th[ée]or[èe]me|Propri[ée]t[ée]|Proposition|Lemme|Corollaire|Exemple|Contre-exemple|"
    r"M[ée]thode|Remarque|D[ée]monstration|Preuve|Notation)s?\b[^*]*\*\*\s*:?",
    re.I,
)


def box_labeled_paragraphs(md: str) -> str:
    """Encadre (citation `>`) les blocs « **Définition (…)** : … » écrits en paragraphe simple,
    jusqu'à la prochaine ligne vide : les encadrés deviennent des callouts dans Notion."""
    out: list[str] = []
    boxing = False
    in_math = False
    for line, in_code in iter_lines_with_code_state(md):
        if boxing:
            if not line.strip() and not in_math:
                boxing = False
            else:
                if line.strip() == "$$":
                    in_math = not in_math
                out.append("> " + line if line.strip() else ">")
                continue
        if not in_code and _BOX_START_RE.match(line):
            boxing, in_math = True, False
            out.append("> " + line)
            continue
        out.append(line)
    return "\n".join(out)


def normalize_course_markdown(md: str) -> str:
    md = strip_wrapping_fence(md).replace("\r\n", "\n")
    md = convert_latex_delimiters(md)
    md = fix_lazy_quotes(md)
    md = box_labeled_paragraphs(md)
    md = normalize_display_math(md)
    md = fix_table_math_pipes(md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


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


def remove_first_h1(md: str) -> str:
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if H1_RE.match(line.strip()):
            del lines[i]
            if i < len(lines) and not lines[i].strip():
                del lines[i]
        break
    return "\n".join(lines)


def headings_outline(md: str, max_level: int = 4) -> list[str]:
    out = []
    for line, in_code in iter_lines_with_code_state(md):
        if in_code:
            continue
        m = HEADING_RE.match(line.strip())
        if m and len(m.group(1)) <= max_level:
            out.append(f"{'  ' * (len(m.group(1)) - 1)}{m.group(1)} {m.group(2)}")
    return out


def top_level_blocks(md: str) -> list[str]:
    """Blocs séparés par des lignes vides, sans couper un bloc de code, une formule ou un callout."""
    blocks: list[str] = []
    cur: list[str] = []
    in_code = in_math = False
    callout_depth = 0
    for line in md.split("\n"):
        s = split_prefix(line)[1].strip()
        if in_code:
            cur.append(line)
            if FENCE_RE.match(s):
                in_code = False
            continue
        if in_math:
            cur.append(line)
            if s == "$$":
                in_math = False
            continue
        if FENCE_RE.match(s):
            in_code = True
            cur.append(line)
            continue
        if s == "$$":
            in_math = True
            cur.append(line)
            continue
        if s.startswith("<callout"):
            callout_depth += 1
        if s == "</callout>":
            callout_depth = max(callout_depth - 1, 0)
        if not s and callout_depth == 0:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def split_markdown(md: str, max_chars: int) -> list[str]:
    """Découpe en parties ≤ max_chars (autant que possible), de préférence avant un titre."""
    blocks = top_level_blocks(md)
    parts: list[str] = []
    cur: list[str] = []
    size = 0
    for block in blocks:
        is_heading = bool(HEADING_RE.match(block.split("\n", 1)[0].strip()))
        if cur and (size + len(block) > max_chars or (is_heading and size > max_chars * 0.7)):
            parts.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block) + 2
    if cur:
        parts.append("\n\n".join(cur))
    return parts or [""]


# --- Vers Notion ------------------------------------------------------------------------------

KEYWORD_CALLOUTS = [
    (re.compile(r"^\*\*(D[ée]finition)", re.I), "📘", "blue_bg"),
    (re.compile(r"^\*\*(Th[ée]or[èe]me|Proposition|Lemme|Corollaire|Propri[ée]t[ée]|Formule|Loi)", re.I), "📐", "purple_bg"),
    (re.compile(r"^\*\*(Exemple|Application|Contre-exemple)", re.I), "🧪", "green_bg"),
    (re.compile(r"^\*\*(M[ée]thode|Algorithme)", re.I), "🛠️", "brown_bg"),
    (re.compile(r"^\*\*(D[ée]monstration|Preuve)", re.I), "✏️", "gray_bg"),
    (re.compile(r"^\*\*(Remarque|Note|Rappel|Notation)", re.I), "📝", "gray_bg"),
    (re.compile(r"^\*\*(Attention|Important|Pi[èe]ge|⚠️)", re.I), "⚠️", "red_bg"),
]
EMOJI_COLORS = {"💡": "yellow_bg", "⚠️": "orange_bg", "⚠": "orange_bg", "📌": "blue_bg", "❗": "red_bg", "✅": "green_bg"}
GENERIC_ICON = "💬"


def _icon_added_by_app(icon: str, first_line: str) -> bool:
    """Icône posée par l'app (encadré « **Définition** … » → 📘) : à ne pas réinjecter dans le texte.
    Une icône choisie par l'utilisateur dans Notion est, elle, conservée."""
    if icon == GENERIC_ICON:
        return True
    return any(icon == kw_icon and rx.match(first_line.strip()) for rx, kw_icon, _ in KEYWORD_CALLOUTS)


_EMOJI_RE = re.compile(
    "^([\U0001F000-\U0001FAFF\u2190-\u21ff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff]"
    "[\ufe0f\U0001F3FB-\U0001F3FF]*"
    "(?:\u200d[\U0001F000-\U0001FAFF\u2600-\u27bf]\ufe0f?)*)\\s*"
)


def _leading_emoji(text: str) -> tuple[str, str] | None:
    m = _EMOJI_RE.match(text or "")
    if not m:
        return None
    return m.group(1), text[m.end():]


def _escape_for_notion(text: str) -> str:
    # `<` suivi d'une lettre ressemble à une balise Notion (<callout>, <page>…) : on l'échappe.
    return re.sub(r"<(?=[A-Za-z/!])", r"\\<", text)


def _quote_to_notion(lines: list[str]) -> list[str]:
    # Retire un seul niveau de citation.
    inner = [re.sub(r"^[ \t]*>[ ]?", "", line, count=1) for line in lines]
    while inner and not inner[0].strip():
        inner.pop(0)
    while inner and not inner[-1].strip():
        inner.pop()
    if not inner:
        return []
    first = inner[0].strip()
    icon = color = None
    emoji = _leading_emoji(first)
    if emoji:
        icon, rest = emoji
        color = EMOJI_COLORS.get(icon, "gray_bg")
        inner[0] = rest
    else:
        for rx, kw_icon, kw_color in KEYWORD_CALLOUTS:
            if rx.match(first):
                icon, color = kw_icon, kw_color
                break
    content = [l for l in inner if l.strip()]
    has_blocks = any(
        l.strip() == "$$" or LIST_RE.match(l) or l.lstrip().startswith("|") or FENCE_RE.match(l) for l in content
    )
    if icon is None:
        if len(content) == 1:
            return ["> " + content[0].strip()]
        if not has_blocks:
            return ["> " + "<br>".join(l.strip() for l in content)]
        icon, color = GENERIC_ICON, "gray_bg"
    out = [f'<callout icon="{icon}" color="{color}">']
    out += ["\t" + l for l in content]
    out.append("</callout>")
    return out


def to_notion_markdown(md: str) -> str:
    md = normalize_display_math(md)
    md = remove_first_h1(md)
    escaped = []
    for line, in_code in iter_lines_with_code_state(md):
        escaped.append(line if in_code else map_outside_code_math(line, _escape_for_notion))
    lines = escaped
    out: list[str] = []
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if FENCE_RE.match(split_prefix(line)[1]):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if not in_code and line.lstrip().startswith(">"):
            group = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                group.append(lines[i])
                i += 1
            out += _quote_to_notion(group)
            continue
        out.append(line)
        i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


# --- Depuis Notion (import des annotations) ---------------------------------------------------

_ATTR_SUFFIX_RE = re.compile(r'\s*\{(?:(?:color|toggle)="[^"]*"\s*)+\}\s*$')
# Échappements ajoutés par Notion, inutiles en Markdown standard (`\<` gardé devant une lettre).
_UNESCAPE_RE = re.compile(r"\\([~^{}\[\]]|<(?![A-Za-z/!]))")


def _convert_inline(text: str) -> str:
    text = re.sub(r"\$``\$", "", text)  # équation vide
    text = re.sub(r"\$`(.+?)`\$", r"$\1$", text)
    text = re.sub(r'<mention-user[^>]*>(.*?)</mention-user>', r"@\1", text)
    text = re.sub(r'<mention-(?:page|database|data-source|agent) url="([^"]*)"[^>]*>(.*?)</mention-[a-z-]+>', r"[\2](\1)", text)
    text = re.sub(r'<mention-date start="([^"]*)"(?: end="([^"]*)")?[^>]*/>',
                  lambda m: m.group(1) + (f" → {m.group(2)}" if m.group(2) else ""), text)
    text = re.sub(r"<mention-[a-z-]+[^>]*/>", "", text)
    for _ in range(3):  # spans imbriqués
        text = re.sub(r"<span[^>]*>(.*?)</span>", r"\1", text)
    text = _ATTR_SUFFIX_RE.sub("", text)

    def unescape(seg: str) -> str:
        seg = _UNESCAPE_RE.sub(r"\1", seg)
        return re.sub(r"(?<=\S)\\>", ">", seg)

    return map_outside_code_math(text, unescape)


def _dedent_one_tab(lines: list[str]) -> list[str]:
    return [l[1:] if l.startswith("\t") else re.sub(r"^ {1,4}", "", l) for l in lines]


def _html_table_to_pipe(block: str) -> list[str]:
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
        cells = [
            _convert_inline(html.unescape(c.strip()).replace("<br>", " ").replace("\n", " ")).replace("|", r"\|")
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out


def _collect_until(lines: list[str], i: int, end_re: re.Pattern, start_re: re.Pattern | None = None) -> tuple[list[str], int]:
    """Renvoie les lignes internes (sans les balises) jusqu'à la balise fermante correspondante."""
    depth = 1
    body = []
    i += 1
    while i < len(lines):
        s = lines[i].strip()
        if start_re is not None and start_re.match(s):
            depth += 1
        elif end_re.match(s):
            depth -= 1
            if depth == 0:
                return body, i + 1
        body.append(lines[i])
        i += 1
    return body, i


_CALLOUT_START = re.compile(r'^<callout(?:\s+icon="([^"]*)")?(?:\s+color="([^"]*)")?[^>]*>$')
_CALLOUT_END = re.compile(r"^</callout>$")
_DETAILS_START = re.compile(r"^<details[^>]*>$")
_DETAILS_END = re.compile(r"^</details>$")
_WRAPPER_START = re.compile(r"^<(synced_block|synced_block_reference|columns|column)(\s[^>]*)?>$")
_WRAPPER_END = re.compile(r"^</(synced_block|synced_block_reference|columns|column)>$")


def _convert_blocks(lines: list[str]) -> list[list[str]]:
    """Convertit une suite de lignes Notion en blocs Markdown standard (listes de lignes)."""
    blocks: list[list[str]] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s == "<empty-block/>":
            i += 1
            continue
        if FENCE_RE.match(s):
            j = i + 1
            while j < len(lines) and not FENCE_RE.match(lines[j].strip()):
                j += 1
            blocks.append([l.lstrip("\t") if raw.startswith("\t") else l for l in lines[i:j + 1]])
            i = j + 1
            continue
        if s == "$$":
            j = i + 1
            while j < len(lines) and lines[j].strip() != "$$":
                j += 1
            blocks.append([l.strip() for l in lines[i:j + 1]])
            i = j + 1
            continue
        m = _CALLOUT_START.match(s)
        if m:
            icon = html.unescape(m.group(1) or "")
            body, i = _collect_until(lines, i, _CALLOUT_END, _CALLOUT_START)
            inner = _convert_blocks(_dedent_one_tab(body))
            if inner and icon and not _icon_added_by_app(icon, inner[0][0]):
                inner[0][0] = f"{icon} {inner[0][0]}"
            quoted: list[str] = []
            for k, blk in enumerate(inner):
                if k:
                    quoted.append(">")
                quoted += [("> " + l) if l else ">" for l in blk]
            if quoted:
                blocks.append(quoted)
            continue
        if s.startswith("<table"):
            j = i
            while j < len(lines) and "</table>" not in lines[j]:
                j += 1
            blocks.append(_html_table_to_pipe("\n".join(lines[i:j + 1])))
            i = j + 1
            continue
        if _DETAILS_START.match(s):
            body, i = _collect_until(lines, i, _DETAILS_END, _DETAILS_START)
            summary = ""
            if body and body[0].strip().startswith("<summary>"):
                summary = re.sub(r"</?summary>", "", body.pop(0).strip())
            if summary:
                blocks.append([f"**{_convert_inline(summary)}**"])
            blocks += _convert_blocks(_dedent_one_tab(body))
            continue
        if _WRAPPER_START.match(s):
            body, i = _collect_until(lines, i, _WRAPPER_END, _WRAPPER_START)
            blocks += _convert_blocks(_dedent_one_tab(body))
            continue
        if re.match(r"^<(unknown|table_of_contents)\b[^>]*/>$", s):
            i += 1
            continue
        media = re.match(r'^<(audio|video|file|pdf)\s+src="([^"]*)"[^>]*>(.*?)</\1>$', s)
        if media:
            label = _convert_inline(media.group(3)) or {"audio": "audio", "video": "vidéo", "pdf": "PDF"}.get(media.group(1), "fichier")
            blocks.append([f"*[{label} dans Notion]({media.group(2)})*"])
            i += 1
            continue
        ref = re.match(r'^<(page|database)\s+url="([^"]*)"[^>]*>(.*?)</\1>$', s)
        if ref:
            blocks.append([f"[{_convert_inline(ref.group(3))}]({ref.group(2)})"])
            i += 1
            continue
        image = re.match(r"^!\[(.*?)\]\((.*?)\)", s)
        if image:
            blocks.append([f"*[Image ajoutée dans Notion : {image.group(1) or 'sans légende'}]*"])
            i += 1
            continue
        if LIST_RE.match(raw) and not raw.startswith("\t"):
            # Liste : items consécutifs + enfants indentés (tabulations Notion → 4 espaces) en un bloc.
            items = []
            ordered = LIST_RE.match(raw).group(2)[0].isdigit()
            while i < len(lines) and lines[i].strip() and (LIST_RE.match(lines[i]) or lines[i].startswith("\t")):
                line = lines[i]
                top = LIST_RE.match(line) if not line.startswith("\t") else None
                if items and top and top.group(2)[0].isdigit() != ordered:
                    break  # une liste numérotée qui suit une liste à puces : nouveau bloc
                depth = len(line) - len(line.lstrip("\t"))
                items.append("    " * depth + _convert_inline(line.lstrip("\t")))
                i += 1
            blocks.append(items)
            continue
        if s.startswith(">"):
            text = _convert_inline(re.sub(r"^>\s?", "", s))
            blocks.append(["> " + part if part else ">" for part in text.split("<br>")])
            i += 1
            continue
        heading = HEADING_RE.match(s)
        if heading:
            blocks.append([f"{heading.group(1)} {_convert_inline(heading.group(2))}"])
            i += 1
            continue
        # Paragraphe (éventuellement avec des enfants indentés).
        text = _convert_inline(s).replace("<br>", "\n")
        blocks.append(text.split("\n"))
        i += 1
        children = []
        while i < len(lines) and lines[i].startswith("\t"):
            children.append(lines[i])
            i += 1
        if children:
            blocks += _convert_blocks(_dedent_one_tab(children))
    return blocks


def notion_to_standard(md: str, title: str | None = None) -> str:
    blocks = _convert_blocks(md.replace("\r\n", "\n").split("\n"))
    body = "\n\n".join("\n".join(b) for b in blocks if b)
    if title:
        body = f"# {title}\n\n{body}"
    return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
