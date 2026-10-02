"""Historique : liste, détail (avancement, relances, supports de cours), écoute, transcription, suppression."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from .. import config, db, recorder, subjects, supports
from ..markdown_utils import replace_title
from ..pipeline import BUSY_STATUSES, STEP_LABELS, pipeline
from ..publish import drive
from ..textutils import fmt_duration, parse_date
from ..web import redirect, render

router = APIRouter()

CHAINED = {"transcribe", "format", "publish"}
ACTIONS = {"finalize", "transcribe", "format", "state", "publish", "publish_drive", "publish_notion", "import_annotations"}

# Avancement affiché en 4 étapes (la mise à jour de l'état de matière est rattachée à « Mise en forme »).
STEPS = ["Audio", "Transcription", "Mise en forme", "Publication"]
RUNNING_STEP = {"recording": 0, "finalizing": 0, "transcribing": 1, "formatting": 2, "publishing": 3}
FAILED_STEP = {"finalisation audio": 0, "transcription": 1, "mise en forme": 2,
               "publication": 3, "publication Drive": 3, "publication Notion": 3}
RETRY_ACTION = {v: k for k, v in STEP_LABELS.items() if k != "publish"}  # libellé d'étape en échec → action


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


def _list_context() -> dict:
    recs = db.list_recordings()
    polling = any(r["status"] in BUSY_STATUSES or r["status"] == "recording" or pipeline.is_active(r["id"])
                  or r.get("auto_retry_at") for r in recs)
    return {"recs": recs, "polling": polling}


def progress_steps(rec: dict, files: dict) -> list[dict]:
    status = rec["status"]
    running = RUNNING_STEP.get(status)
    failed = FAILED_STEP.get(rec.get("error_step") or "") if status == "error" else None
    published = files["course"] and all(rec[f"{k}_status"] in ("done", "skipped") for k in ("drive", "notion"))
    done = [files["audio"], files["transcript"], files["course"], published]
    steps = []
    for i, label in enumerate(STEPS):
        if running is not None:
            state = "done" if i < running else "current" if i == running else "todo"
        elif failed is not None:
            state = "done" if i < failed else "error" if i == failed else "todo"
        else:
            state = "done" if done[i] else "todo"
        steps.append({"label": label, "state": state})
    if status == "interrupted":
        steps[0]["state"] = "warn"
    if files["audio"] and rec.get("duration_seconds"):
        steps[0]["detail"] = fmt_duration(rec["duration_seconds"])
    return steps


def retry_action(rec: dict) -> str | None:
    """Action du bouton « Réessayer » : relance l'étape en échec (ou termine un enregistrement interrompu)."""
    if rec["status"] == "interrupted":
        return "finalize"
    if rec["status"] != "error":
        return None
    step = rec.get("error_step") or ""
    if step == "publication":
        failed = [k for k in ("drive", "notion") if rec[f"{k}_status"] == "error"]
        return f"publish_{failed[0]}" if len(failed) == 1 else "publish"
    return RETRY_ACTION.get(step)


def status_context(rec: dict) -> dict:
    rid = rec["id"]
    files = _files(rid)
    current = pipeline.current if pipeline.current and pipeline.current.get("recording_id") == rid else None
    return {
        "rec": rec,
        "files": files,
        "busy": pipeline.is_active(rid) or rec["status"] in BUSY_STATUSES,
        "running_detail": pipeline.describe() if current else "",
        "steps": progress_steps(rec, files),
        "retry": retry_action(rec),
        "targets": [
            {"name": "Drive", "status": rec["drive_status"], "url": rec.get("drive_md_url") or rec.get("drive_transcription_url"),
             "error": rec.get("drive_error")},
            {"name": "Notion", "status": rec["notion_status"], "url": rec.get("notion_page_url"), "error": rec.get("notion_error")},
        ],
    }


def supports_context(rid: int) -> dict:
    info = supports.summary(rid)
    return {"supports": info["supports"], "supports_reading": info["reading"], "supports_unused": info["unused"],
            "support_accept": supports.ACCEPT}


def _support_or_404(rid: int, sid: int) -> dict:
    sup = db.get_support(sid)
    if not sup or sup["recording_id"] != rid:
        raise HTTPException(404, "Support introuvable.")
    return sup


@router.get("/enregistrements", response_class=HTMLResponse)
def list_page(request: Request):
    return render(request, "recordings.html", **_list_context())


@router.get("/fragments/recordings", response_class=HTMLResponse)
def list_fragment(request: Request):
    return render(request, "partials/recordings_rows.html", **_list_context())


@router.get("/enregistrements/{rid}", response_class=HTMLResponse)
def detail_page(request: Request, rid: int):
    rec = _get(rid)
    folder = recorder.recording_dir(rid)
    transcript = (folder / "transcript.txt").read_text(encoding="utf-8") if (folder / "transcript.txt").exists() else None
    return render(
        request, "recording_detail.html",
        **status_context(rec),
        **supports_context(rid),
        transcript=transcript,
        subjects_list=db.list_subjects(),
        logs=db.list_logs(rid, 60),
    )


@router.get("/fragments/recording/{rid}/status", response_class=HTMLResponse)
def status_fragment(request: Request, rid: int):
    return render(request, "partials/recording_status.html", **status_context(_get(rid)))


@router.get("/fragments/recording/{rid}/supports", response_class=HTMLResponse)
def supports_fragment(request: Request, rid: int):
    return render(request, "partials/supports.html", **status_context(_get(rid)), **supports_context(rid))


@router.post("/enregistrements/{rid}/supports")
async def add_supports(rid: int, files: list[UploadFile] = File(...)):
    _get(rid)
    added, errors = await run_in_threadpool(supports.add_many, rid, [(f.filename, f.file) for f in files])
    target = f"/enregistrements/{rid}#supports"
    if not added:
        return redirect(target, " ".join(errors) or "Aucun fichier reçu.", "err")
    drive.deposit_in_background(rid)
    message = ("Support ajouté" if len(added) == 1 else f"{len(added)} supports ajoutés") + " : lecture en cours."
    return redirect(target, " ".join([message, *errors]), "warn" if errors else "ok")


@router.get("/enregistrements/{rid}/supports/{sid}")
def support_file(rid: int, sid: int):
    sup = _support_or_404(rid, sid)
    return FileResponse(supports.file_path(sup), filename=sup["filename"], content_disposition_type="inline")


@router.get("/enregistrements/{rid}/supports/{sid}/texte")
def support_text(rid: int, sid: int):
    path = supports.text_path(_support_or_404(rid, sid))
    if not path.exists():
        raise HTTPException(404, "Le texte de ce support n'a pas encore été lu.")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@router.post("/enregistrements/{rid}/supports/{sid}/relire")
def reread_support(rid: int, sid: int):
    sup = _support_or_404(rid, sid)
    if sup["status"] in ("pending", "extracting"):
        return redirect(f"/enregistrements/{rid}#supports", "La lecture de ce support est déjà en cours.")
    supports.text_path(sup).unlink(missing_ok=True)
    db.update_support(sid, status="pending", error=None)
    supports.extract_in_background(sid)
    return redirect(f"/enregistrements/{rid}#supports", "Nouvelle lecture du support lancée.")


@router.post("/enregistrements/{rid}/supports/{sid}/delete")
def delete_support(rid: int, sid: int):
    sup = _support_or_404(rid, sid)
    supports.remove(sid)
    return redirect(f"/enregistrements/{rid}#supports", f"Support « {sup['filename']} » retiré.")


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
    db.update_recording(rid, auto_retry_at=None, auto_retry_count=0)  # relance manuelle : les essais auto repartent de zéro
    pipeline.submit("recording", rid, action, chain=action in CHAINED)
    if action == "transcribe" and rec["status"] == "uploaded":
        return redirect(target, "Traitement lancé : transcription, mise en forme puis publication.")
    if action == "finalize":
        return redirect(target, "Préparation de l'audio mise en file : lancez ensuite le traitement.")
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
