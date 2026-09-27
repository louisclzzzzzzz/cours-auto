"""Données par matière sur le disque local (source de vérité) : état `_etat.md`, vocabulaire,
fichiers de séance et « cours complet »."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from . import config, db
from .recorder import recording_dir
from .textutils import fr_date, safe_filename, slugify

MAX_VOCABULARY = 100
COURSE_FILE = "course.md"
ANNOTATED_FILE = "course_annote.md"
TYPE_ORDER = {"CM": 0, "TD": 1, "TP": 2}


# --- Matières ------------------------------------------------------------------------------------

def create_subject(name: str, teachers: str = "") -> int:
    name = " ".join(name.split())
    if not name:
        raise ValueError("Le nom de la matière est obligatoire.")
    existing = db.get_subject_by_name(name)
    if existing:
        return existing["id"]
    base = slugify(name, 50)
    slug, n = base, 2
    while db.q1("SELECT 1 FROM subjects WHERE slug = ?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    sid = db.create_subject(name, slug, teachers)
    subject_dir(db.get_subject(sid)).mkdir(parents=True, exist_ok=True)
    return sid


def subject_dir(subject: dict) -> Path:
    return config.SUBJECTS_DIR / subject["slug"]


def state_path(subject: dict) -> Path:
    return subject_dir(subject) / "_etat.md"


def read_state(subject: dict) -> str:
    path = state_path(subject)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_state(subject: dict, text: str) -> None:
    path = state_path(subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():  # historique des états précédents
        hist = path.parent / "etats"
        hist.mkdir(exist_ok=True)
        shutil.copy2(path, hist / f"_etat_{datetime.now():%Y%m%d-%H%M%S}.md")
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def get_vocabulary(subject: dict) -> list[str]:
    return [t for t in db.loads(subject.get("vocabulary"), []) if isinstance(t, str) and t.strip()]


def set_vocabulary(subject_id: int, terms: list[str]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for t in terms:
        t = " ".join(str(t).split())
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            clean.append(t)
    if len(clean) > MAX_VOCABULARY:
        raise ValueError(f"Le vocabulaire est limité à {MAX_VOCABULARY} termes ({len(clean)} fournis).")
    db.update_subject(subject_id, vocabulary=db.dumps(clean))
    return clean


def get_proposed_terms(subject: dict) -> list[str]:
    return db.loads(subject.get("proposed_terms"), [])


def add_proposed_terms(subject_id: int, terms: list[str]) -> None:
    subject = db.get_subject(subject_id)
    if not subject:
        return
    known = {t.casefold() for t in get_vocabulary(subject)} | {t.casefold() for t in get_proposed_terms(subject)}
    merged = get_proposed_terms(subject) + [t for t in terms if t.casefold() not in known]
    db.update_subject(subject_id, proposed_terms=db.dumps(merged[:60]))


def resolve_proposed_terms(subject_id: int, accepted: list[str], rejected: list[str]) -> tuple[list[str], str | None]:
    subject = db.get_subject(subject_id)
    vocab = get_vocabulary(subject)
    free = MAX_VOCABULARY - len(vocab)
    to_add = [t for t in accepted if t.casefold() not in {v.casefold() for v in vocab}]
    warning = None
    if len(to_add) > free:
        warning = f"Vocabulaire plein : seuls {max(free, 0)} terme(s) ont pu être ajoutés (limite {MAX_VOCABULARY})."
        to_add = to_add[: max(free, 0)]
    set_vocabulary(subject_id, vocab + to_add)
    handled = {t.casefold() for t in to_add} | {t.casefold() for t in rejected}
    remaining = [t for t in get_proposed_terms(subject) if t.casefold() not in handled]
    db.update_subject(subject_id, proposed_terms=db.dumps(remaining))
    return to_add, warning


# --- Fichiers de séance ---------------------------------------------------------------------------

def course_path(recording_id: int) -> Path:
    return recording_dir(recording_id) / COURSE_FILE


def annotated_path(recording_id: int) -> Path:
    return recording_dir(recording_id) / ANNOTATED_FILE


def read_course(recording_id: int, prefer_annotated: bool = False) -> str | None:
    if prefer_annotated and annotated_path(recording_id).exists():
        return annotated_path(recording_id).read_text(encoding="utf-8")
    path = course_path(recording_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def archive_versions(recording_id: int) -> None:
    """Avant une nouvelle mise en forme : conserve l'ancien cours (et l'éventuelle version annotée)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    versions = recording_dir(recording_id) / "versions"
    for path in (course_path(recording_id), annotated_path(recording_id)):
        if path.exists():
            versions.mkdir(exist_ok=True)
            path.replace(versions / f"{path.stem}_{stamp}.md")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def session_filename(rec: dict) -> str:
    """Ex. `2026-09-29_CM03_graphes-ponderes.md`."""
    num = f"{int(rec['session_number'] or 0):02d}"
    return f"{rec['session_date']}_{rec['course_type']}{num}_{slugify(rec.get('title') or 'seance', 50)}.md"


def annotated_filename(rec: dict) -> str:
    return session_filename(rec)[:-3] + "_annote.md"


def session_label(rec: dict) -> str:
    return f"{rec['course_type']} {rec['session_number']} – {rec.get('title') or 'sans titre'}"


def sessions_with_course(subject_id: int) -> list[dict]:
    recs = [r for r in db.list_recordings(subject_id) if course_path(r["id"]).exists()]
    recs.sort(
        key=lambda r: (
            r["session_date"] or "",
            r.get("event_start") or r["started_at"] or "",
            TYPE_ORDER.get(r["course_type"], 9),
            r["session_number"] or 0,
        )
    )
    return recs


def build_full_course(subject: dict) -> str:
    """Concaténation chronologique de toutes les séances (version annotée si importée), avec sommaire."""
    recs = sessions_with_course(subject["id"])
    annotated = sum(1 for r in recs if annotated_path(r["id"]).exists())
    lines = [
        f"# {subject['name']} – Cours complet",
        "",
        f"*Document généré automatiquement le {fr_date(datetime.now().date())} à partir de {len(recs)} séance(s)"
        + (f", dont {annotated} avec annotations importées de Notion" if annotated else "")
        + ".*",
        "",
        "## Table des matières",
        "",
    ]
    if not recs:
        lines.append("*(aucune séance pour l'instant)*")
    for i, r in enumerate(recs, 1):
        mark = " *(annotée)*" if annotated_path(r["id"]).exists() else ""
        lines.append(f"{i}. {session_label(r)} — {fr_date(r['session_date'])}{mark}")
    for r in recs:
        md = read_course(r["id"], prefer_annotated=True) or ""
        lines += ["", "---", "", md.strip()]
    text = "\n".join(lines).rstrip() + "\n"
    folder = subject_dir(subject)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cours_complet.md").write_text(text, encoding="utf-8")
    return text


def full_course_name(subject: dict) -> str:
    return safe_filename(f"{subject['name']} – Cours complet") + ".md"


def notebooklm_doc_name(subject: dict) -> str:
    return safe_filename(f"{subject['name']} – NotebookLM")
