"""Page « Enregistrements » : liste, détail, relances d'étapes, écoute, transcription, suppression."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from .. import config, db, recorder, subjects
from ..markdown_utils import replace_title
from ..pipeline import BUSY_STATUSES, STEP_LABELS, pipeline
from ..textutils import parse_date
from ..web import redirect, render

router = APIRouter()

CHAINED = {"finalize", "transcribe", "format", "publish"}
ACTIONS = {"finalize", "transcribe", "format", "state", "publish", "publish_drive", "publish_notion", "import_annotations"}


def _get(rid: int) -> dict:
    rec = db.get_recording(rid)
    if not rec:
        raise HTTPException(404, "Enregistrement introuvable.")
    return rec


def _files(rid: int) -> dict:
    folder = recorder.recording_dir(rid)
    source = recorder.source_path(rid)
    return {
        "source": source.name if source else None,
        "audio": recorder.audio_path(rid).exists(),
        "transcript": (folder / "transcript.txt").exists(),
        "course": subjects.course_path(rid).exists(),
        "annotated": subjects.annotated_path(rid).exists(),
        "chunks": db.chunk_stats(rid),
    }


@router.get("/enregistrements", response_class=HTMLResponse)
def list_page(request: Request):
    return render(request, "recordings.html", recs=db.list_recordings())


@router.get("/fragments/recordings", response_class=HTMLResponse)
def list_fragment(request: Request):
    return render(request, "partials/recordings_rows.html", recs=db.list_recordings())


@router.get("/enregistrements/{rid}", response_class=HTMLResponse)
def detail_page(request: Request, rid: int):
    rec = _get(rid)
    folder = recorder.recording_dir(rid)
    transcript = (folder / "transcript.txt").read_text(encoding="utf-8") if (folder / "transcript.txt").exists() else None
    return render(
        request, "recording_detail.html",
        rec=rec,
        files=_files(rid),
        transcript=transcript,
        course=subjects.read_course(rid),
        annotated=subjects.read_course(rid, prefer_annotated=True) if subjects.annotated_path(rid).exists() else None,
        old_pages=db.loads(rec.get("notion_old_pages"), []),
        subjects_list=db.list_subjects(),
        logs=db.list_logs(rid, 60),
        busy=pipeline.is_active(rid) or rec["status"] in BUSY_STATUSES,
    )


@router.get("/fragments/recording/{rid}/status", response_class=HTMLResponse)
def status_fragment(request: Request, rid: int):
    rec = _get(rid)
    return render(request, "partials/recording_status.html", rec=rec,
                  busy=pipeline.is_active(rid) or rec["status"] in BUSY_STATUSES)


@router.get("/enregistrements/{rid}/audio")
def audio(rid: int):
    path = recorder.audio_path(rid)
    if not path.exists():
        raise HTTPException(404, "Audio final pas encore disponible.")
    return FileResponse(path, media_type="audio/mpeg", filename=f"enregistrement_{rid}.mp3")


@router.get("/enregistrements/{rid}/transcription.txt")
def transcript_file(rid: int):
    path = recorder.recording_dir(rid) / "transcript.txt"
    if not path.exists():
        raise HTTPException(404, "Transcription absente.")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@router.get("/enregistrements/{rid}/cours.md")
def course_file(rid: int, annote: bool = False):
    md = subjects.read_course(rid, prefer_annotated=annote)
    if md is None:
        raise HTTPException(404, "Cours absent.")
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")


@router.post("/enregistrements/{rid}/action")
def run_action(rid: int, action: str = Form(...), notion_mode: str = Form("new_version"),
               back: str = Form("")):
    rec = _get(rid)
    target = back if back.startswith("/") else f"/enregistrements/{rid}"
    if action not in ACTIONS:
        return redirect(target, "Action inconnue.", "err")
    if pipeline.is_active(rid):
        return redirect(target, "Un traitement est déjà en cours pour cet enregistrement.", "err")
    if rec["status"] == "recording" and action != "finalize":
        return redirect(target, "Enregistrement en cours : arrêtez-le ou finalisez-le d'abord.", "err")
    if action == "finalize" and rec["status"] == "recording" and rec.get("client_state") != "stopped":
        db.log(rid, "Finalisation forcée d'un enregistrement encore marqué actif.", "warning")
    if action == "format" and rec.get("notion_page_id"):
        db.update_recording(rid, notion_pending_action="overwrite" if notion_mode == "overwrite" else "new_version")
    if action == "finalize":
        db.update_recording(rid, status="finalizing", ended_at=rec.get("ended_at") or db.now_iso())
    pipeline.submit("recording", rid, action, chain=action in CHAINED)
    return redirect(target, f"Relance mise en file : {STEP_LABELS.get(action, action)}.")


@router.post("/enregistrements/{rid}/metadata")
def update_metadata(
    rid: int,
    subject_id: int = Form(...),
    course_type: str = Form(...),
    session_number: int = Form(...),
    session_date: str = Form(...),
    teacher: str = Form(""),
    title: str = Form(""),
):
    rec = _get(rid)
    if not db.get_subject(subject_id):
        return redirect(f"/enregistrements/{rid}", "Matière inconnue.", "err")
    day = parse_date(session_date)
    if not day:
        return redirect(f"/enregistrements/{rid}", "Date invalide.", "err")
    db.update_recording(
        rid,
        subject_id=subject_id,
        course_type=course_type if course_type in config.COURSE_TYPES else rec["course_type"],
        session_number=max(session_number, 1),
        session_date=day.isoformat(),
        teacher=teacher.strip(),
        title=title.strip() or rec.get("title"),
    )
    _refresh_course_header(rid, rec)
    recorder.write_meta(rid)
    return redirect(f"/enregistrements/{rid}", "Informations mises à jour. Republiez pour mettre à jour Drive/Notion.")


def _refresh_course_header(rid: int, before: dict) -> None:
    """Répercute type/numéro/titre dans le titre du cours, sans déclencher de nouvelle version Notion."""
    rec = db.get_recording(rid)
    md = subjects.read_course(rid)
    if md is None or not rec.get("title"):
        return
    old_hash = subjects.content_hash(md)
    new_md = replace_title(md, f"{rec['course_type']} {rec['session_number']} – {rec['title']}")
    if new_md != md:
        subjects.course_path(rid).write_text(new_md, encoding="utf-8")
        if before.get("notion_content_hash") == old_hash:
            db.update_recording(rid, notion_content_hash=subjects.content_hash(new_md))


@router.post("/enregistrements/{rid}/delete")
def delete(rid: int):
    rec = _get(rid)
    if pipeline.is_active(rid) or rec["status"] in BUSY_STATUSES:
        return redirect(f"/enregistrements/{rid}", "Impossible de supprimer pendant un traitement.", "err")
    recorder.delete_files(rid)
    db.delete_recording(rid)
    if rec.get("subject_id") and db.get_subject(rec["subject_id"]):
        subjects.build_full_course(db.get_subject(rec["subject_id"]))
    return redirect("/enregistrements", "Enregistrement supprimé (fichiers locaux). Les pages Notion / fichiers Drive "
                                        "déjà publiés ne sont pas supprimés.")
