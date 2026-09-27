"""Page « Enregistrer » (accueil) : créneaux du jour, sélection du cours, API de l'enregistreur."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import calendar_ics, db, recorder, subjects
from ..pipeline import pipeline
from ..textutils import parse_date
from ..web import consent_reminder_visible, redirect, render

router = APIRouter()

ALLOWED_EXT = {"webm", "ogg", "m4a", "mp4", "aac", "mp3", "wav"}
MAX_CHUNK_BYTES = 50 * 1024 * 1024


def _ics_info() -> dict:
    return {
        "has_cache": calendar_ics.load_calendar() is not None,
        "last_refresh": db.get_setting("ics_last_refresh"),
        "source": db.get_setting("ics_source"),
        "error": db.get_setting("ics_last_error"),
        "url_set": bool(db.get_setting("ics_url").strip()),
    }


def slots_context(day: str | date | None = None, selected: str | None = None, notice: str | None = None) -> dict:
    today = calendar_ics.now_local().date()
    d = parse_date(day) or today
    slots = calendar_ics.slots_for_day(d)
    if selected:
        for s in slots:
            s.is_current = s.key == selected
    return {
        "day": d,
        "today": today,
        "prev_day": d - timedelta(days=1),
        "next_day": d + timedelta(days=1),
        "slots": slots,
        "subjects": db.list_subjects(),
        "ics": _ics_info(),
        "notice": notice,
        "selected": selected,
    }


def _active_recordings() -> list[dict]:
    now = datetime.now().astimezone()
    out = []
    for rec in db.list_recordings_by_status("recording", "interrupted"):
        try:
            idle = (now - datetime.fromisoformat(rec["last_seen_at"] or rec["started_at"])).total_seconds()
        except ValueError:
            idle = None
        out.append({**rec, "idle_seconds": idle, "chunks": db.chunk_stats(rec["id"])["n"]})
    return out


@router.get("/", response_class=HTMLResponse)
def home(request: Request, day: str | None = None, state: str | None = None, code: str | None = None,
         error: str | None = None):
    # Ancien lien de connexion Google (versions précédentes : retour sur la racine de l'app).
    if state and (code or error):
        return redirect("/parametres", "Ce lien de connexion Google n'est plus valable : cliquez sur « Se connecter à "
                                       "Google Drive » (la connexion s'ouvre dans votre navigateur).", "err")
    calendar_ics.refresh_in_background(min_age_minutes=30)
    return render(
        request, "record.html",
        **slots_context(day),
        active=_active_recordings(),
        consent=consent_reminder_visible(),
    )


@router.get("/fragments/slots", response_class=HTMLResponse)
def slots_fragment(request: Request, day: str | None = None, selected: str | None = None):
    return render(request, "partials/slots.html", **slots_context(day, selected))


@router.post("/calendar/refresh", response_class=HTMLResponse)
def calendar_refresh(request: Request, day: str = Form(""), selected: str = Form("")):
    ok, message = calendar_ics.refresh_from_url()
    return render(request, "partials/slots.html", **slots_context(day, selected or None, notice=message))


@router.post("/subjects/quick-map", response_class=HTMLResponse)
def quick_map(
    request: Request,
    summary: str = Form(...),
    day: str = Form(""),
    selected: str = Form(""),
    subject_id: str = Form("new"),
    new_name: str = Form(""),
    teacher: str = Form(""),
    match: str = Form("exact"),
    pattern: str = Form(""),
):
    """Associe un intitulé ADE non reconnu à une matière (existante ou nouvelle)."""
    try:
        if subject_id == "new":
            sid = subjects.create_subject(new_name or calendar_ics.suggest_subject_name(summary), teacher)
        else:
            sid = int(subject_id)
        if match == "regex":
            re.compile(pattern)
            db.add_mapping(pattern, True, sid)
        else:
            db.add_mapping(summary, False, sid)
        notice = f"« {summary} » est maintenant associé à « {db.get_subject(sid)['name']} »."
    except Exception as exc:  # noqa: BLE001 (regex invalide, nom vide…)
        notice = f"Association impossible : {exc}"
    return render(request, "partials/slots.html", **slots_context(day, selected or None, notice=notice))


@router.get("/fragments/latest", response_class=HTMLResponse)
def latest_fragment(request: Request):
    return render(request, "partials/latest.html", recs=db.list_recordings(limit=5))


@router.get("/fragments/worker", response_class=HTMLResponse)
def worker_fragment(request: Request):
    return render(request, "partials/worker.html", current=pipeline.current, detail=pipeline.describe(),
                  queued=pipeline.queued_count())


@router.post("/consent/dismiss", response_class=HTMLResponse)
def dismiss_consent():
    db.set_setting("consent_reminder_dismissed", "1")
    return HTMLResponse("")


# --- API de l'enregistreur ---------------------------------------------------------------------------

class NewRecording(BaseModel):
    subject_id: int | None = None
    subject_name: str | None = None
    course_type: str = "CM"
    teacher: str = ""
    session_date: str | None = None
    event: dict | None = None
    mime_type: str = ""


class SegmentIn(BaseModel):
    mime_type: str = ""


class HeartbeatIn(BaseModel):
    state: str = "recording"
    elapsed: float | None = None


class StopIn(BaseModel):
    elapsed: float | None = None


@router.post("/api/recordings")
def api_create(payload: NewRecording):
    sid = payload.subject_id
    if not sid and (payload.subject_name or "").strip():
        sid = subjects.create_subject(payload.subject_name)
    if not sid or not db.get_subject(sid):
        raise HTTPException(400, "Choisissez une matière avant de démarrer.")
    session_date = (parse_date(payload.session_date) or calendar_ics.now_local().date()).isoformat()
    rid = recorder.create_recording(
        subject_id=sid,
        course_type=payload.course_type,
        session_date=session_date,
        teacher=payload.teacher,
        mime_type=payload.mime_type,
        event=payload.event,
    )
    return {"id": rid, **recorder.start_segment(rid, payload.mime_type)}


@router.get("/api/recordings/active")
def api_active():
    return {"recordings": [
        {k: r[k] for k in ("id", "subject_name", "course_type", "session_number", "status", "elapsed_seconds",
                           "idle_seconds", "chunks")}
        for r in _active_recordings()
    ]}


VOXTRAL_MAX_SECONDS = 3 * 3600


@router.post("/api/recordings/import")
async def api_import(
    file: UploadFile = File(...),
    subject_id: str = Form(""),
    subject_name: str = Form(""),
    course_type: str = Form("CM"),
    teacher: str = Form(""),
    session_date: str = Form(""),
    event: str = Form(""),
):
    """Import d'un fichier audio (téléphone, dictaphone…) : traité comme un enregistrement."""
    sid = int(subject_id) if subject_id.strip().isdigit() else None
    if not sid and subject_name.strip():
        sid = subjects.create_subject(subject_name)
    if not sid or not db.get_subject(sid):
        raise HTTPException(400, "Choisissez une matière avant d'importer.")
    try:
        event_data = json.loads(event) if event else None
    except ValueError:
        event_data = None
    rid = recorder.create_recording(
        subject_id=sid,
        course_type=course_type,
        session_date=(parse_date(session_date) or calendar_ics.now_local().date()).isoformat(),
        teacher=teacher,
        mime_type=file.content_type or "",
        event=event_data if isinstance(event_data, dict) else None,
        origin="import",
        source_filename=file.filename,
    )
    try:
        path = await run_in_threadpool(recorder.save_import, rid, file.file, recorder.import_extension(file.filename))
        duration = await run_in_threadpool(recorder.probe_audio, path)
    except Exception:
        recorder.delete_files(rid)
        db.delete_recording(rid)
        raise
    if duration is None:
        recorder.delete_files(rid)
        db.delete_recording(rid)
        raise HTTPException(400, "Ce fichier ne contient pas de piste audio lisible.")
    warning = None
    if duration > VOXTRAL_MAX_SECONDS:
        warning = "Plus de 3 h d'audio : au-delà de la limite d'une requête de transcription Voxtral."
        db.log(rid, warning, "warning")
    db.update_recording(rid, elapsed_seconds=duration, ended_at=db.now_iso())
    pipeline.submit("recording", rid, "finalize")
    return {"id": rid, "duration": duration, "warning": warning}


@router.post("/api/recordings/{rid}/segments")
def api_segment(rid: int, payload: SegmentIn):
    try:
        return recorder.start_segment(rid, payload.mime_type)
    except recorder.RecorderError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/api/recordings/{rid}/chunks/{seq}")
async def api_chunk(rid: int, seq: int, request: Request, segment: int, ext: str = "webm",
                    elapsed: float | None = None):
    data = await request.body()
    if not data:
        raise HTTPException(400, "Morceau vide.")
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(413, "Morceau trop volumineux.")
    ext = ext if ext in ALLOWED_EXT else "webm"
    try:
        await run_in_threadpool(recorder.save_chunk, rid, seq, segment, data, ext, elapsed)
    except recorder.RecorderError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "size": len(data)}


@router.post("/api/recordings/{rid}/heartbeat")
def api_heartbeat(rid: int, payload: HeartbeatIn):
    rec = recorder.heartbeat(rid, payload.state, payload.elapsed)
    if not rec:
        raise HTTPException(404, "Enregistrement introuvable.")
    return {"status": rec["status"]}


@router.post("/api/recordings/{rid}/stop")
def api_stop(rid: int, payload: StopIn | None = None):
    rec = db.get_recording(rid)
    if not rec:
        raise HTTPException(404, "Enregistrement introuvable.")
    if rec["status"] not in ("recording", "interrupted"):
        return {"ok": True, "status": rec["status"]}
    fields: dict = {"status": "finalizing", "client_state": "stopped", "ended_at": db.now_iso()}
    if payload and payload.elapsed and payload.elapsed > float(rec["elapsed_seconds"] or 0):
        fields["elapsed_seconds"] = payload.elapsed
    if db.chunk_stats(rid)["n"] == 0:
        db.update_recording(rid, status="error", error_step="finalisation audio",
                            error_message="Aucun morceau audio n'a été reçu.", client_state="stopped")
        return {"ok": False, "status": "error"}
    db.update_recording(rid, **fields)
    db.log(rid, "Arrêt demandé : finalisation et traitement mis en file.")
    pipeline.submit("recording", rid, "finalize")
    return {"ok": True, "status": "finalizing"}
