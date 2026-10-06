"""Données par matière sur le disque local : vocabulaire, cours de chaque séance (rédigés par la tâche Claude
et récupérés depuis Drive) et « cours complet »."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from . import config, db
from .recorder import recording_dir
from .textutils import fr_date, slugify

MAX_VOCABULARY = 100
COURSE_FILE = "course.md"
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


# --- Fichiers de séance ---------------------------------------------------------------------------

def course_path(recording_id: int) -> Path:
    return recording_dir(recording_id) / COURSE_FILE


def read_course(recording_id: int) -> str | None:
    path = course_path(recording_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def archive_versions(recording_id: int) -> None:
    """Avant de remplacer le cours : conserve l'ancienne version dans `versions/`."""
    path = course_path(recording_id)
    if path.exists():
        versions = recording_dir(recording_id) / "versions"
        versions.mkdir(exist_ok=True)
        path.replace(versions / f"{path.stem}_{datetime.now():%Y%m%d-%H%M%S}.md")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
    """Concaténation chronologique de toutes les séances, avec sommaire."""
    recs = sessions_with_course(subject["id"])
    lines = [
        f"# {subject['name']} – Cours complet",
        "",
        f"*Document généré automatiquement le {fr_date(datetime.now().date())} à partir de {len(recs)} séance(s).*",
        "",
        "## Table des matières",
        "",
    ]
    if not recs:
        lines.append("*(aucune séance pour l'instant)*")
    for i, r in enumerate(recs, 1):
        lines.append(f"{i}. {session_label(r)} — {fr_date(r['session_date'])}")
    for r in recs:
        lines += ["", "---", "", (read_course(r["id"]) or "").strip()]
    text = "\n".join(lines).rstrip() + "\n"
    folder = subject_dir(subject)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cours_complet.md").write_text(text, encoding="utf-8")
    return text
