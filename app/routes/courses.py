"""Page « Cours » : séances par matière, aperçu rendu (Markdown + KaTeX), liens Notion / Drive,
import des annotations Notion."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import calendar_ics, db, subjects
from ..pipeline import pipeline
from ..web import redirect, render

router = APIRouter()


def _subject_or_404(sid: int) -> dict:
    subject = db.get_subject(sid)
    if not subject:
        raise HTTPException(404, "Matière introuvable.")
    return subject


@router.get("/cours", response_class=HTMLResponse)
def courses_page(request: Request):
    """Liste des matières (une ligne chacune) + intitulés de l'emploi du temps encore sans matière."""
    rows = []
    for s in db.list_subjects():
        sessions = subjects.sessions_with_course(s["id"])
        rows.append({
            **s,
            "n_sessions": len(sessions),
            "last": sessions[-1] if sessions else None,
            "n_proposed": len(subjects.get_proposed_terms(s)),
        })
    return render(request, "courses.html", rows=rows,
                  unmapped=calendar_ics.unmapped_summaries(),
                  subjects_list=db.list_subjects(),
                  notion_seances_url=db.get_setting("notion_seances_url"),
                  drive_root_url=db.get_setting("drive_root_url"))


@router.get("/cours/{sid}", response_class=HTMLResponse)
def subject_courses(request: Request, sid: int, seance: int | None = None):
    subject = _subject_or_404(sid)
    sessions = subjects.sessions_with_course(sid)
    for r in sessions:
        r["annotated"] = subjects.annotated_path(r["id"]).exists()
    others = [r for r in db.list_recordings(sid) if not subjects.course_path(r["id"]).exists()]
    selected = next((r for r in sessions if r["id"] == seance), sessions[-1] if sessions else None)
    return render(request, "course_subject.html", subject=subject, sessions=sessions, others=others,
                  selected=selected, n_proposed=len(subjects.get_proposed_terms(subject)))


@router.get("/fragments/cours/seance/{rid}", response_class=HTMLResponse)
def session_preview(request: Request, rid: int, version: str = "auto"):
    rec = db.get_recording(rid)
    if not rec:
        raise HTTPException(404, "Séance introuvable.")
    has_annot = subjects.annotated_path(rid).exists()
    use_annot = has_annot and version != "original"
    return render(request, "partials/session_preview.html", rec=rec, has_annot=has_annot, use_annot=use_annot,
                  md=subjects.read_course(rid, prefer_annotated=use_annot) or "",
                  old_pages=db.loads(rec.get("notion_old_pages"), []),
                  n_supports=len(db.list_supports(rid)))


@router.get("/cours/{sid}/complet", response_class=HTMLResponse)
def full_course(request: Request, sid: int):
    subject = _subject_or_404(sid)
    return render(request, "course_full.html", subject=subject, md=subjects.build_full_course(subject))


@router.post("/cours/{sid}/annotations")
def import_subject_annotations(sid: int):
    _subject_or_404(sid)
    pipeline.submit("subject", sid, "import_annotations")
    return redirect(f"/cours/{sid}", "Import des annotations Notion lancé pour toute la matière (suivi dans Enregistrements).")


@router.post("/cours/seance/{rid}/annotations")
def import_session_annotations(rid: int):
    rec = db.get_recording(rid)
    if not rec:
        raise HTTPException(404, "Séance introuvable.")
    if not rec.get("notion_page_id"):
        return redirect(f"/cours/{rec['subject_id']}?seance={rid}", "Cette séance n'a pas encore de page Notion.", "err")
    pipeline.submit("recording", rid, "import_annotations", chain=False)
    return redirect(f"/cours/{rec['subject_id']}?seance={rid}", "Import des annotations lancé.")


@router.post("/cours/{sid}/drive")
def republish_subject(sid: int):
    _subject_or_404(sid)
    pipeline.submit("subject", sid, "publish_drive")
    return redirect(f"/cours/{sid}", "Régénération du cours complet et du Google Doc NotebookLM lancée.")
