"""Appels au LLM Mistral : mise en forme du cours (appel n°1), mise à jour de l'état de matière
(appel n°2) et suggestions de vocabulaire. Les prompts système sont dans `prompts/`."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Callable

from mistralai.client import Mistral
from mistralai.client.errors import MistralError

from . import config, db
from .markdown_utils import (
    HEADING_RE,
    extract_title,
    headings_outline,
    iter_lines_with_code_state,
    normalize_course_markdown,
    remove_first_h1,
    replace_title,
    strip_wrapping_fence,
)
from .retry import with_retries
from .textutils import fmt_duration, fr_date

log = logging.getLogger(__name__)

LLM_TIMEOUT_MS = 20 * 60 * 1000
REASONING_BUDGET = 16000  # tokens ajoutés à max_tokens pour la réflexion quand reasoning_effort = high
FINAL_SECTIONS = ("points clés", "à retenir pour la suite")
FIDELITY_REMINDER = (
    "Rappel : tout ce que l'enseignant n'a pas dit (précision, hypothèse comme « fini non vide », synonyme, "
    "formule ou notation non dictée, correction de transcription) doit figurer dans un bloc « > 💡 **Complément** : … », "
    "jamais dans le texte du cours."
)
SUPPORT_REMINDER = (
    "Support de cours fourni (règle 9) : le cours ne contient que ce qui a été DIT. Exceptions au rappel "
    "précédent : ce que l'enseignant aborde s'écrit avec les termes, formules et énoncés exacts du support, et un "
    "terme mal transcrit que le support confirme se corrige sans bloc Complément (jamais de mention de page ou de "
    "diapositive). Ce qui n'est QUE sur le support ne s'écrit pas (ni section, ni phrase, ni parenthèse). Oral ≠ "
    "support : écris la version orale et ajoute « > ⚠️ **Écart avec le support** : le support indique … ; en cours, "
    "l'enseignant retient … »."
)
SUPPORT_GAPS_TITLE = "Sur le support, non abordé en cours"
SUPPORT_MAX_CHARS = 60_000  # texte de support transmis par appel ; au-delà, chaque document est raccourci


class LLMError(RuntimeError):
    pass


def get_client() -> Mistral:
    key = config.mistral_api_key()
    if not key:
        raise LLMError("Clé MISTRAL_API_KEY absente du fichier .env.")
    return Mistral(api_key=key)


def load_prompt(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for chunk in content:  # contenu structuré (ex. modèles de raisonnement)
        text = getattr(chunk, "text", None)
        if text is None and isinstance(chunk, dict):
            text = chunk.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def complete(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    json_mode: bool = False,
    max_continuations: int = 3,
    label: str = "Appel LLM",
) -> str:
    """Appel chat ; si la réponse est tronquée (finish_reason = length), on la prolonge avec `prefix`.

    Avec le raisonnement activé, la réflexion du modèle consomme aussi `max_tokens` : on ajoute une marge,
    et si le budget s'épuise avant toute réponse, on recommence avec un budget doublé."""
    client = get_client()
    model = model or db.get_setting("llm_model") or config.DEFAULT_SETTINGS["llm_model"]
    max_tokens = max_tokens or db.get_int_setting("llm_max_tokens") or 32000
    reasoning = db.get_setting("llm_reasoning_effort").strip()
    budget = max_tokens + (REASONING_BUDGET if reasoning == "high" else 0)
    out = ""
    for _ in range(max_continuations + 1):
        msgs = list(messages)
        if out:
            msgs.append({"role": "assistant", "content": out, "prefix": True})
        kwargs: dict = {
            "model": model,
            "messages": msgs,
            "max_tokens": budget,
            "temperature": temperature,
            "timeout_ms": LLM_TIMEOUT_MS,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning:
            kwargs["reasoning_effort"] = reasoning
        try:
            resp = with_retries(lambda: client.chat.complete(**kwargs), label=label)
        except MistralError as exc:
            # Modèle sans raisonnement réglable (ex. mistral-large) : on renvoie sans ce paramètre.
            if exc.status_code != 400 or "reasoning" not in (exc.body or "").lower() or "reasoning_effort" not in kwargs:
                raise
            log.warning("%s : reasoning_effort refusé par %s, nouvel essai sans.", label, model)
            reasoning = ""
            kwargs.pop("reasoning_effort")
            resp = with_retries(lambda: client.chat.complete(**kwargs), label=label)
        choice = resp.choices[0]
        text = _content_text(choice.message.content)  # les blocs de réflexion (« thinking ») sont ignorés
        if choice.finish_reason == "length" and len(text) <= len(out):
            # Budget épuisé pendant la réflexion, sans rien de nouveau : on recommence avec plus de marge.
            budget *= 2
            log.info("%s : budget épuisé pendant la réflexion, nouvel essai avec max_tokens=%d", label, budget)
            continue
        # Avec `prefix`, l'API renvoie le préfixe suivi de la suite.
        out = text if (not out or text.startswith(out)) else out + text
        if choice.finish_reason != "length":
            return out
        log.info("%s : réponse tronquée, continuation…", label)
    raise LLMError("Réponse du LLM encore tronquée après plusieurs continuations.")


def test_api_key() -> tuple[bool, str, list[str]]:
    try:
        models = get_client().models.list()
        ids = sorted({m.id for m in (models.data or [])})
        return True, f"Clé valide ({len(ids)} modèles disponibles).", ids
    except Exception as exc:  # noqa: BLE001
        return False, f"Échec : {exc}", []


# --- Métadonnées de séance -----------------------------------------------------------------------

def session_meta(rec: dict, subject: dict) -> dict:
    return {
        "matiere": subject["name"],
        "type": rec["course_type"],
        "numero": rec["session_number"] or 1,
        "date": fr_date(rec["session_date"]),
        "date_iso": rec["session_date"],
        "enseignant": rec.get("teacher") or subject.get("teachers") or "non renseigné",
        "duree": fmt_duration(rec.get("duration_seconds")),
        "intitule_ade": rec.get("event_summary") or "",
    }


def _meta_block(meta: dict) -> str:
    lines = [
        f"- Matière : {meta['matiere']}",
        f"- Séance : {meta['type']} {meta['numero']} (type {meta['type']}, n° {meta['numero']})",
        f"- Date : {meta['date']}",
        f"- Enseignant : {meta['enseignant']}",
        f"- Durée de l'enregistrement : {meta['duree']}",
    ]
    if meta.get("intitule_ade"):
        lines.append(f"- Intitulé dans l'emploi du temps : {meta['intitule_ade']}")
    if meta.get("supports"):
        lines.append(f"- Support de cours fourni : {', '.join(meta['supports'])}")
    return "\n".join(lines)


def heading_prefix(meta: dict) -> str:
    return f"{meta['type']} {meta['numero']}"


def empty_state(subject_name: str) -> str:
    return (
        f"# État de la matière : {subject_name}\n\n"
        "## Plan cumulé des séances\n\n(aucune séance enregistrée pour l'instant)\n\n"
        "## Notions introduites\n\n## Notations en vigueur\n\n## Où en est le cours\n\n## Pour la prochaine fois\n"
    )


# --- Appel n°1 : mise en forme -------------------------------------------------------------------

def chunk_transcript(paragraphs: list[dict], lines: list[str], max_chars: int) -> list[list[str]]:
    """Découpe en blocs cohérents : coupure de préférence sur une pause, jamais au milieu d'un paragraphe."""
    total = sum(len(l) + 2 for l in lines)
    if total <= max_chars or len(lines) < 2:
        return [lines]
    n = math.ceil(total / max_chars)
    target = total / n
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for p, line in zip(paragraphs, lines):
        if cur and len(chunks) < n - 1 and size >= target * 0.85:
            if p.get("gap_before", 0) >= 2.0 or size >= target * 1.1 or size + len(line) > max_chars:
                chunks.append(cur)
                cur, size = [], 0
        cur.append(line)
        size += len(line) + 2
    if cur:
        chunks.append(cur)
    return chunks


def remove_sections(md: str, titles: tuple[str, ...]) -> str:
    """Supprime les sections dont le titre (casse ignorée) figure dans `titles`."""
    out: list[str] = []
    skip_level: int | None = None
    for line, in_code in iter_lines_with_code_state(md):
        m = None if in_code else HEADING_RE.match(line.strip())
        if m:
            level = len(m.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and m.group(2).strip().lower().rstrip(" :") in titles:
                skip_level = level
                continue
        if skip_level is None:
            out.append(line)
    return "\n".join(out)


def _user_message(meta: dict, state: str, instructions: list[str], transcript: str, support: str = "") -> str:
    reminders = [FIDELITY_REMINDER, SUPPORT_REMINDER] if support else [FIDELITY_REMINDER]
    support_part = (
        "# Support de cours (diapositives / documents de l'enseignant, texte extrait automatiquement)\n"
        f"{support}\n\n" if support else ""
    )
    return (
        "# Métadonnées de la séance\n"
        f"{_meta_block(meta)}\n\n"
        "# État de la matière (mémoire des séances précédentes)\n"
        f"{state.strip() or '(vide : première séance enregistrée pour cette matière)'}\n\n"
        "# Consignes\n" + "\n".join(f"- {i}" for i in [*instructions, *reminders]) + "\n\n"
        f"{support_part}"
        "# Transcription\n"
        f"{transcript}"
    )


def finalize_course(md: str, meta: dict) -> tuple[str, str]:
    """Normalise le Markdown, impose le titre `# CM 3 – Titre` et ajoute la ligne de métadonnées."""
    md = normalize_course_markdown(md)
    raw_title = extract_title(md) or "Séance"
    prefix = heading_prefix(meta)
    short = re.sub(
        rf"^\s*(?:{re.escape(meta['type'])}\s*(?:n[°o]\s*)?{meta['numero']}\b|{re.escape(prefix)})\s*[–—:\-]*\s*",
        "",
        raw_title,
        flags=re.I,
    ).strip() or "Séance"
    md = replace_title(md, f"{prefix} – {short}")
    info = f"*{meta['matiere']} — {meta['type']} {meta['numero']} du {meta['date']} — Enseignant : {meta['enseignant']}"
    info += (f" — Support : {', '.join(meta['supports'])}*" if meta.get("supports") else "*")
    lines = md.split("\n")
    idx = next((i for i, l in enumerate(lines) if l.startswith("# ")), 0)
    if not any(l.startswith(f"*{meta['matiere']} — ") for l in lines[idx + 1 : idx + 4]):
        lines[idx + 1 : idx + 1] = ["", info]
    return normalize_course_markdown("\n".join(lines)), short


def _split_support(docs: list[dict] | None, transcript: str, meta: dict,
                   on_progress: Callable[[str], None] | None) -> tuple[str, str]:
    """(pages abordées à l'oral, pages utiles jamais abordées) : textes pour la mise en forme et la section finale."""
    if not docs or not any(doc["pages"] for doc in docs):
        return "", ""
    if on_progress:
        on_progress("Support : repérage des pages abordées à l'oral…")
    try:
        selection = align_support(docs, transcript, meta)
    except Exception as exc:  # noqa: BLE001 - repli : tout le support, sans section finale
        log.warning("Repérage des pages du support impossible (%s) : support transmis en entier", exc)
        return render_support(docs), ""
    spoken = render_support(docs, {i: sel["abordees"] for i, sel in selection.items()})
    unspoken = render_support(docs, {i: sel["non_abordees"] for i, sel in selection.items()})
    return spoken, unspoken


def format_course(
    transcript_data: dict,
    state: str,
    meta: dict,
    on_progress: Callable[[str], None] | None = None,
    support_docs: list[dict] | None = None,
) -> tuple[str, str]:
    """Renvoie (cours Markdown, titre court).

    `support_docs` : supports de cours découpés en pages (voir supports.documents). Seules les pages abordées à
    l'oral sont transmises à la mise en forme ; les autres, si elles sont utiles, sont résumées en fin de cours.
    """
    from .transcribe import build_paragraphs, paragraph_line, speaker_labels, speakers_header

    paragraphs = build_paragraphs(transcript_data)
    if not paragraphs:
        raise LLMError("La transcription est vide : rien à mettre en forme.")
    labels, shares = speaker_labels(paragraphs)
    lines = [paragraph_line(p, labels) for p in paragraphs]
    header = speakers_header(shares)
    max_chars = db.get_int_setting("llm_chunk_chars") or 60000
    chunks = chunk_transcript(paragraphs, lines, max_chars)
    support, unspoken = _split_support(support_docs, (header + "\n\n" if header else "") + "\n\n".join(lines),
                                       meta, on_progress)
    system = {"role": "system", "content": load_prompt("format_course")}
    title_rule = (
        f"La première ligne doit être exactement : `# {heading_prefix(meta)} – <titre court et explicite de la séance>`."
    )

    if len(chunks) == 1:
        user = _user_message(
            meta, state, [title_rule, "Rédige le cours complet de cette séance à partir de la transcription ci-dessous."],
            (header + "\n\n" if header else "") + "\n\n".join(chunks[0]),
            support,
        )
        md = complete([system, {"role": "user", "content": user}], label="Mise en forme du cours")
        if unspoken:
            if on_progress:
                on_progress("Mise en forme : notions du support non abordées")
            md = insert_before_final_sections(strip_wrapping_fence(md), support_gaps(md, unspoken, meta))
        return finalize_course(md, meta)

    n = len(chunks)
    parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        if on_progress:
            on_progress(f"Mise en forme : partie {i}/{n}")
        no_final = "N'écris PAS les sections « Points clés » ni « À retenir pour la suite » : elles seront rédigées à la fin."
        if i == 1:
            instructions = [
                title_rule,
                f"La transcription est longue et t'est fournie en {n} parties. Voici la partie 1/{n} : "
                "rédige le début du cours correspondant à cette partie uniquement.",
                no_final,
            ]
        else:
            so_far = "\n\n".join(parts)
            outline = "\n".join(headings_outline(so_far)) or "(aucun titre)"
            tail = so_far[-1500:]
            instructions = [
                f"Voici la partie {i}/{n} de la transcription. Le cours déjà rédigé pour les parties précédentes "
                f"a le plan suivant :\n{outline}\n\nIl se termine ainsi :\n\"\"\"\n{tail}\n\"\"\"",
                "Continue la rédaction à partir de là : ne répète ni le titre de la séance ni ce qui est déjà rédigé, "
                "et poursuis la numérotation des sections.",
                no_final,
            ]
        user = _user_message(meta, state, instructions, (header + "\n\n" if header else "") + "\n\n".join(chunk), support)
        part = complete([system, {"role": "user", "content": user}], label=f"Mise en forme (partie {i}/{n})")
        part = strip_wrapping_fence(part)
        if i > 1:
            part = remove_first_h1(part)
        parts.append(remove_sections(part, FINAL_SECTIONS).strip())

    body = "\n\n".join(parts)
    if unspoken:
        if on_progress:
            on_progress("Mise en forme : notions du support non abordées")
        body = insert_before_final_sections(body, support_gaps(body, unspoken, meta))
    if on_progress:
        on_progress("Mise en forme : points clés")
    key_points = complete(
        [
            {"role": "system", "content": load_prompt("key_points")},
            {"role": "user", "content": f"# Métadonnées\n{_meta_block(meta)}\n\n# Cours\n{body}"},
        ],
        label="Points clés",
    )
    return finalize_course(body + "\n\n" + strip_wrapping_fence(key_points).strip(), meta)


def render_support(docs: list[dict], selection: dict[int, set[int]] | None = None,
                   max_chars: int = SUPPORT_MAX_CHARS) -> str:
    """Texte des supports pour le LLM : toutes les pages, ou seulement `selection` (n° de document → n° de pages)."""
    chosen = []
    for i, doc in enumerate(docs):
        pages = [(label, text) for n, (label, text) in enumerate(doc["pages"], 1)
                 if selection is None or n in selection.get(i, set())]
        if pages:
            chosen.append((i, doc["name"], "\n\n".join(f"[{label}]\n{text or '(vide)'}" for label, text in pages)))
    total = sum(len(text) for *_, text in chosen) or 1
    blocks = []
    for i, name, text in chosen:
        budget = len(text) if total <= max_chars else max(2000, int(max_chars * len(text) / total))
        if len(text) > budget:
            text = text[:budget].rsplit("\n", 1)[0] + "\n[… fin du document non transmise : trop long …]"
        name = name.replace('"', "'")
        blocks.append(f'<support n="{i + 1}" fichier="{name}">\n{text}\n</support>')
    return "\n\n".join(blocks)


def align_support(docs: list[dict], transcript: str, meta: dict) -> dict[int, dict[str, set[int]]]:
    """Pages de chaque support abordées à l'oral / au contenu utile jamais abordé (titre, plan… : ni l'un ni l'autre)."""
    out = complete(
        [
            {"role": "system", "content": load_prompt("support_alignment")},
            {"role": "user", "content": f"# Métadonnées\n{_meta_block(meta)}\n\n# Support de cours\n"
                                        f"{render_support(docs)}\n\n# Transcription\n{transcript}"},
        ],
        json_mode=True,
        label="Support : pages abordées",
    )
    data = json.loads(strip_wrapping_fence(out))
    result: dict[int, dict[str, set[int]]] = {}
    for i, doc in enumerate(docs):
        entry = data.get(str(i + 1)) if isinstance(data, dict) else None
        entry = entry if isinstance(entry, dict) else {}

        def pages(key: str) -> set[int]:
            values = entry.get(key) if isinstance(entry.get(key), list) else []
            return {int(v) for v in values if str(v).strip().isdigit() and 1 <= int(v) <= len(doc["pages"])}

        spoken = pages("abordees")
        result[i] = {"abordees": spoken, "non_abordees": pages("non_abordees") - spoken}
    return result


def support_gaps(course_md: str, support: str, meta: dict) -> str:
    """Section « Sur le support, non abordé en cours » (vide s'il n'y a rien de substantiel)."""
    out = complete(
        [
            {"role": "system", "content": load_prompt("support_gaps")},
            {"role": "user", "content": f"# Métadonnées\n{_meta_block(meta)}\n\n# Support de cours\n{support}\n\n"
                                        f"# Cours rédigé\n{course_md}"},
        ],
        label="Support : notions non abordées",
    )
    out = strip_wrapping_fence(out).strip()
    if not out or out.upper().startswith("RIEN") or "- " not in out:
        return ""
    out = remove_first_h1(out).strip()
    lines = out.split("\n")
    if not HEADING_RE.match(lines[0].strip()):
        lines.insert(0, "")
    lines[0] = f"## {SUPPORT_GAPS_TITLE}"
    return "\n".join(lines).strip()


def insert_before_final_sections(md: str, section: str) -> str:
    """Insère une section juste avant « Points clés » / « À retenir pour la suite » (sinon à la fin)."""
    if not section:
        return md
    lines = md.split("\n")
    for i, (line, in_code) in enumerate(iter_lines_with_code_state(md)):
        m = None if in_code else HEADING_RE.match(line.strip())
        if m and len(m.group(1)) == 2 and m.group(2).strip().lower().rstrip(" :") in FINAL_SECTIONS:
            return "\n".join([*lines[:i], section, "", *lines[i:]])
    return md.rstrip() + "\n\n" + section + "\n"


# --- Appel n°2 : état de matière -----------------------------------------------------------------

def update_state(old_state: str, course_md: str, meta: dict) -> str:
    user = (
        f"Matière : {meta['matiere']}\n"
        f"Séance qui vient d'avoir lieu : {meta['type']} {meta['numero']} du {meta['date']} ({meta['date_iso']})\n\n"
        f"# Ancien état\n{old_state.strip() or '(vide : première séance enregistrée)'}\n\n"
        f"# Cours de la nouvelle séance\n{course_md}"
    )
    state = complete(
        [{"role": "system", "content": load_prompt("update_state")}, {"role": "user", "content": user}],
        max_tokens=8000,
        label="Mise à jour de l'état",
    )
    state = strip_wrapping_fence(state).strip()
    if len(state) > 16000:  # ≈ 4 000 tokens : on condense
        state = strip_wrapping_fence(
            complete(
                [
                    {"role": "system", "content": load_prompt("update_state")},
                    {
                        "role": "user",
                        "content": "Cet état est trop long. Condense-le à environ 1 500 mots en gardant exactement la "
                        f"même structure, les titres et la numérotation des séances :\n\n{state}",
                    },
                ],
                max_tokens=6000,
                label="Condensation de l'état",
            )
        ).strip()
    return state + "\n"


_NOT_A_TERM = re.compile(r"[=|{}\\^_$()\[\]<>∑∀∃∈ℝℕℤ⁺⁻ᵢⱼ₀-₉⁰-⁹]")


def plausible_term(term: str, exclude: set[str]) -> bool:
    """Écarte formules, symboles et expressions trop longues : seuls des mots prononçables sont utiles."""
    t = term.strip()
    return (
        2 < len(t) <= 40
        and len(t.split()) <= 4
        and not _NOT_A_TERM.search(t)
        and bool(re.search(r"[A-Za-zÀ-ÿ]{3,}|[A-Z]{2,}", t))
        and t.casefold() not in exclude
    )


def suggest_terms(course_md: str, vocabulary: list[str], exclude: list[str] | tuple = ()) -> list[str]:
    user = f"Vocabulaire actuel : {', '.join(vocabulary) or '(vide)'}\n\n# Cours\n{course_md}"
    raw = complete(
        [{"role": "system", "content": load_prompt("suggest_vocabulary")}, {"role": "user", "content": user}],
        json_mode=True,
        max_tokens=1500,
        temperature=0.1,
        label="Suggestions de vocabulaire",
    )
    try:
        data = json.loads(strip_wrapping_fence(raw))
    except ValueError:
        return []
    if isinstance(data, dict):
        terms = data.get("termes") or data.get("terms") or []
    else:
        terms = data if isinstance(data, list) else []
    banned = {v.casefold() for v in vocabulary} | {e.strip().casefold() for e in exclude if e.strip()}
    haystack = _squash(course_md)
    out: list[str] = []
    for t in terms if isinstance(terms, list) else []:
        t = str(t).strip()
        # Garde-fou : le terme doit figurer dans le cours (le modèle a tendance à proposer des termes « du domaine »).
        if plausible_term(t, banned) and _squash(t) in haystack and t.casefold() not in {o.casefold() for o in out}:
            out.append(t)
    return out[:15]


def _squash(text: str) -> str:
    return re.sub(r"[\s\-‐‑–_’']+", "", text).casefold()
