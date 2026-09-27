"""Page « Matières » : liste, correspondances ADE, vocabulaire, état de matière, termes proposés."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import calendar_ics, db, subjects
from ..transcribe import normalize_bias_terms
from ..web import redirect, render

router = APIRouter()


def _subject_or_404(sid: int) -> dict:
    subject = db.get_subject(sid)
    if not subject:
        raise HTTPException(404, "Matière introuvable.")
    return subject


def unmapped_summaries(days_back: int = 14, days_ahead: int = 60) -> list[dict]:
    today = calendar_ics.now_local().date()
    seen: dict[str, dict] = {}
    for s in calendar_ics.slots_between(today - timedelta(days=days_back), today + timedelta(days=days_ahead)):
        if s.subject_id is None and s.summary not in seen:
            seen[s.summary] = {"summary": s.summary, "suggested": s.suggested_name, "teacher": s.teacher,
                               "type": s.course_type, "count": 0}
        if s.subject_id is None:
            seen[s.summary]["count"] += 1
    return sorted(seen.values(), key=lambda x: x["summary"].casefold())


@router.get("/matieres", response_class=HTMLResponse)
def subjects_page(request: Request):
    rows = []
    for s in db.list_subjects():
        rows.append({
            **s,
            "counts": db.session_counts(s["id"]),
            "n_mappings": len(db.list_mappings(s["id"])),
            "n_vocab": len(subjects.get_vocabulary(s)),
            "n_proposed": len(subjects.get_proposed_terms(s)),
        })
    return render(request, "subjects.html", rows=rows, unmapped=unmapped_summaries(), subjects_list=db.list_subjects())


@router.post("/matieres")
def create(name: str = Form(...), teachers: str = Form("")):
    try:
        sid = subjects.create_subject(name, teachers)
    except ValueError as exc:
        return redirect("/matieres", str(exc), "err")
    return redirect(f"/matieres/{sid}", "Matière créée.")


@router.post("/matieres/associer")
def map_summary(summary: str = Form(...), subject_id: str = Form(...), new_name: str = Form(""), teacher: str = Form("")):
    try:
        sid = subjects.create_subject(new_name or calendar_ics.suggest_subject_name(summary), teacher) \
            if subject_id == "new" else int(subject_id)
        db.add_mapping(summary, False, sid)
    except (ValueError, TypeError) as exc:
        return redirect("/matieres", f"Association impossible : {exc}", "err")
    return redirect("/matieres", f"« {summary} » associé à « {db.get_subject(sid)['name']} ».")


@router.get("/matieres/{sid}", response_class=HTMLResponse)
def subject_page(request: Request, sid: int):
    subject = _subject_or_404(sid)
    vocab = subjects.get_vocabulary(subject)
    return render(
        request, "subject_detail.html",
        subject=subject,
        mappings=db.list_mappings(sid),
        vocab=vocab,
        bias_preview=normalize_bias_terms(vocab),
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
    return redirect("/matieres", "Matière supprimée.")


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
    if decision == "accept":
        added, warning = subjects.resolve_proposed_terms(sid, accepted, [])
        msg = warning or f"{len(added)} terme(s) ajouté(s) au vocabulaire."
    elif decision == "reject_all":
        subjects.resolve_proposed_terms(sid, [], proposed)
        msg = "Propositions ignorées."
    else:
        subjects.resolve_proposed_terms(sid, [], accepted)
        msg = f"{len(accepted)} proposition(s) ignorée(s)."
    return redirect(f"/matieres/{sid}", msg)


@router.post("/matieres/{sid}/etat")
def save_state(sid: int, state: str = Form("")):
    subject = _subject_or_404(sid)
    subjects.write_state(subject, state)
    return redirect(f"/matieres/{sid}", "État de la matière enregistré.")
