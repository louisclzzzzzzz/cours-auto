"""Page « Cours » : séances par matière et aperçu rendu (Markdown + KaTeX) des cours rédigés par la tâche Claude,
récupérés depuis Drive."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import calendar_ics, db, subjects
from ..publish import PublishSkipped, drive
from ..web import redirect, render

router = APIRouter()


def _subject_or_404(sid: int) -> dict:
    subject = db.get_subject(sid)
    if not subject:
        raise HTTPException(404, "Matière introuvable.")
    return subject


def _sync_info() -> dict:
    """Dernière récupération des cours depuis Drive (automatique toutes les 10 min ou par le bouton)."""
    at = drive.sync_state["at"]
    return {"at": datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds") if at else "",
            "error": drive.sync_state["error"]}


@router.get("/cours", response_class=HTMLResponse)
def courses_page(request: Request):
    """Liste des matières (une ligne chacune) + intitulés de l'emploi du temps encore sans matière."""
    rows = []
    for s in db.list_subjects():
        sessions = subjects.sessions_with_course(s["id"])
        rows.append({**s, "n_sessions": len(sessions), "last": sessions[-1] if sessions else None})
    return render(request, "courses.html", rows=rows,
                  unmapped=calendar_ics.unmapped_summaries(),
                  subjects_list=db.list_subjects(),
                  drive_root_url=db.get_setting("drive_root_url"),
                  sync=_sync_info())


@router.get("/cours/{sid}", response_class=HTMLResponse)
def subject_courses(request: Request, sid: int, seance: int | None = None):
    subject = _subject_or_404(sid)
    sessions = subjects.sessions_with_course(sid)
    others = [r for r in db.list_recordings(sid) if not subjects.course_path(r["id"]).exists()]
    selected = next((r for r in sessions if r["id"] == seance), sessions[-1] if sessions else None)
    return render(request, "course_subject.html", subject=subject, sessions=sessions, others=others,
                  selected=selected, sync=_sync_info())


@router.get("/fragments/cours/seance/{rid}", response_class=HTMLResponse)
def session_preview(request: Request, rid: int):
    rec = db.get_recording(rid)
    if not rec:
        raise HTTPException(404, "Séance introuvable.")
    return render(request, "partials/session_preview.html", rec=rec, md=subjects.read_course(rid) or "",
                  n_supports=len(db.list_supports(rid)))


@router.get("/cours/{sid}/complet", response_class=HTMLResponse)
def full_course(request: Request, sid: int):
    subject = _subject_or_404(sid)
    return render(request, "course_full.html", subject=subject, md=subjects.build_full_course(subject))


@router.post("/cours/drive")
def fetch_from_drive(subject_id: str = Form(""), back: str = Form("/cours")):
    """Récupère tout de suite les cours rédigés par la tâche Claude (une matière ou toutes)."""
    target = back if back.startswith("/") else "/cours"
    try:
        fetched = drive.fetch_courses(int(subject_id) if subject_id.isdigit() else None)
    except PublishSkipped as exc:
        return redirect(target, str(exc), "err")
    except Exception as exc:  # noqa: BLE001 - message affiché tel quel (droits, réseau…)
        return redirect(target, f"Récupération impossible : {exc}", "err")
    if not fetched:
        return redirect(target, "Aucun cours nouveau ou modifié dans Drive.")
    names = ", ".join(f"{r['subject_name']} {subjects.session_label(r)}" for r in fetched)
    return redirect(target, f"{len(fetched)} cours récupéré{'s' if len(fetched) > 1 else ''} depuis Drive : {names}.")
