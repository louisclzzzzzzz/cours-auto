"""Réglages d'une matière : termes proposés, vocabulaire, intitulés ADE, état de matière.

La liste des matières fait partie de la page « Cours »."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import calendar_ics, db, subjects
from ..web import redirect, render

router = APIRouter()


def _subject_or_404(sid: int) -> dict:
    subject = db.get_subject(sid)
    if not subject:
        raise HTTPException(404, "Matière introuvable.")
    return subject


@router.get("/matieres")
def subjects_page():
    return redirect("/cours")  # la liste des matières fait partie de la page « Cours »


@router.post("/matieres")
def create(name: str = Form(...), teachers: str = Form("")):
    try:
        sid = subjects.create_subject(name, teachers)
    except ValueError as exc:
        return redirect("/cours", str(exc), "err")
    return redirect(f"/matieres/{sid}", "Matière créée : ajoutez son vocabulaire ou ses intitulés d'emploi du temps.")


@router.post("/matieres/associer")
def map_summary(summary: str = Form(...), subject_id: str = Form(...), new_name: str = Form(""), teacher: str = Form("")):
    try:
        sid = subjects.create_subject(new_name or calendar_ics.suggest_subject_name(summary), teacher) \
            if subject_id == "new" else int(subject_id)
        db.add_mapping(summary, False, sid)
    except (ValueError, TypeError) as exc:
        return redirect("/cours#intitules", f"Association impossible : {exc}", "err")
    return redirect("/cours#intitules", f"« {summary} » associé à « {db.get_subject(sid)['name']} ».")


@router.get("/matieres/{sid}", response_class=HTMLResponse)
def subject_page(request: Request, sid: int):
    subject = _subject_or_404(sid)
    vocab = subjects.get_vocabulary(subject)
    return render(
        request, "subject_detail.html",
        subject=subject,
        mappings=db.list_mappings(sid),
        vocab=vocab,
        proposed=subjects.get_proposed_terms(subject),
        state=subjects.read_state(subject),
        counts=db.session_counts(sid),
        n_recordings=len(db.list_recordings(sid)),
        max_vocab=subjects.MAX_VOCABULARY,
    )


@router.post("/matieres/{sid}")
def update(sid: int, name: str = Form(...), teachers: str = Form("")):
    _subject_or_404(sid)
    name = " ".join(name.split())
    other = db.get_subject_by_name(name)
    if not name or (other and other["id"] != sid):
        return redirect(f"/matieres/{sid}", "Nom vide ou déjà utilisé par une autre matière.", "err")
    db.update_subject(sid, name=name, teachers=teachers.strip())
    return redirect(f"/matieres/{sid}", "Matière mise à jour.")


@router.post("/matieres/{sid}/delete")
def delete(sid: int):
    _subject_or_404(sid)
    if db.list_recordings(sid):
        return redirect(f"/matieres/{sid}", "Cette matière a des enregistrements : supprimez-les ou réaffectez-les d'abord.", "err")
    db.delete_subject(sid)
    return redirect("/cours", "Matière supprimée.")


@router.post("/matieres/{sid}/mappings")
def add_mapping(sid: int, pattern: str = Form(...), is_regex: str = Form("")):
    _subject_or_404(sid)
    pattern = pattern.strip()
    if not pattern:
        return redirect(f"/matieres/{sid}", "Motif vide.", "err")
    if is_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            return redirect(f"/matieres/{sid}", f"Expression régulière invalide : {exc}", "err")
    db.add_mapping(pattern, bool(is_regex), sid)
    return redirect(f"/matieres/{sid}", "Correspondance ajoutée.")


@router.post("/matieres/{sid}/mappings/{mid}/delete")
def delete_mapping(sid: int, mid: int):
    db.delete_mapping(mid)
    return redirect(f"/matieres/{sid}", "Correspondance supprimée.")


@router.post("/matieres/{sid}/vocabulaire")
def save_vocabulary(sid: int, terms: str = Form("")):
    _subject_or_404(sid)
    try:
        saved = subjects.set_vocabulary(sid, [t for t in terms.splitlines() if t.strip()])
    except ValueError as exc:
        return redirect(f"/matieres/{sid}", str(exc), "err")
    return redirect(f"/matieres/{sid}", f"Vocabulaire enregistré ({len(saved)} termes).")


@router.post("/matieres/{sid}/propositions", response_class=HTMLResponse)
def resolve_proposals(request: Request, sid: int, accepted: list[str] = Form(default=[]),
                      decision: str = Form("accept")):
    subject = _subject_or_404(sid)
    proposed = subjects.get_proposed_terms(subject)
    if decision == "reject_all":
        subjects.resolve_proposed_terms(sid, [], proposed)
        return redirect(f"/matieres/{sid}#vocabulaire", "Propositions ignorées.")
    # « Valider » : les termes cochés rejoignent le vocabulaire, les autres sont écartés.
    checked = {t.casefold() for t in accepted}
    rejected = [t for t in proposed if t.casefold() not in checked]
    added, warning = subjects.resolve_proposed_terms(sid, accepted, rejected)
    msg = warning or f"{len(added)} terme(s) ajouté(s) au vocabulaire" + (f", {len(rejected)} écarté(s)." if rejected else ".")
    return redirect(f"/matieres/{sid}#vocabulaire", msg)


@router.post("/matieres/{sid}/etat")
def save_state(sid: int, state: str = Form("")):
    subject = _subject_or_404(sid)
    subjects.write_state(subject, state)
    return redirect(f"/matieres/{sid}", "État de la matière enregistré.")
