"""Historique : agenda (par défaut) ou liste, détail (avancement, relances, supports de cours), écoute, transcription, suppression."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from .. import calendar_ics, config, db, recorder, subjects, supports
from ..markdown_utils import replace_title
from ..pipeline import BUSY_STATUSES, STEP_LABELS, pipeline
from ..publish import drive
from ..textutils import MOIS, fmt_duration, parse_date
from ..web import redirect, render

router = APIRouter()

CHAINED = {"transcribe", "publish"}
ACTIONS = {"finalize", "transcribe", "publish"}

# Avancement affiché en 4 étapes : la dernière (le cours) est faite par la tâche Claude, puis récupérée de Drive.
STEPS = ["Audio", "Transcription", "Dépôt Drive", "Cours (Claude)"]
RUNNING_STEP = {"recording": 0, "finalizing": 0, "transcribing": 1, "publishing": 2}
FAILED_STEP = {"finalisation audio": 0, "transcription": 1, "publication": 2}
RETRY_ACTION = {v: k for k, v in STEP_LABELS.items()}  # libellé d'étape en échec → action


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
        "chunks": db.chunk_stats(rid),
    }


def _list_context(day: str | None = None) -> dict:
    """Séances (toutes, ou celles d'un jour de l'agenda) et rechargement de la liste pendant un traitement."""
    recs = db.list_recordings(day=day)
    polling = any(r["status"] in BUSY_STATUSES or r["status"] == "recording" or pipeline.is_active(r["id"])
                  or r.get("auto_retry_at") for r in recs)
    if day is None:
        return {"recs": recs, "polling": polling, "rows_url": "/fragments/recordings", "back": "/enregistrements?vue=liste"}
    return {"recs": recs, "polling": polling, "rows_url": f"/fragments/recordings?jour={day}",
            "back": f"/enregistrements?mois={day[:7]}&jour={day}"}


def _month(value: str) -> date | None:
    """« 2026-10 » → 1er octobre 2026."""
    try:
        year, month = (int(x) for x in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return None


def agenda_context(month: str = "", day: str = "") -> dict:
    """Vue agenda : le mois en semaines (lundi → dimanche), le nombre de séances par jour et celles du jour choisi
    (par défaut aujourd'hui, s'il y en a)."""
    today = calendar_ics.now_local().date()
    selected = parse_date(day)
    first = _month(month) or (selected or today).replace(day=1)
    weeks = calendar.Calendar().monthdatescalendar(first.year, first.month)
    counts = db.recordings_per_day(weeks[0][0].isoformat(), weeks[-1][-1].isoformat())
    if selected is None and first == today.replace(day=1) and counts.get(today.isoformat()):
        selected = today
    return {
        "month_label": f"{MOIS[first.month - 1]} {first.year}",
        "month": first,
        "prev_month": (first - timedelta(days=1)).replace(day=1),
        "next_month": (first + timedelta(days=31)).replace(day=1),
        "weeks": weeks,
        "counts": counts,
        "today": today,
        "selected": selected,
        **(_list_context(selected.isoformat()) if selected else {"recs": [], "polling": False}),
    }


def progress_steps(rec: dict, files: dict) -> list[dict]:
    status = rec["status"]
    running = RUNNING_STEP.get(status)
    failed = FAILED_STEP.get(rec.get("error_step") or "") if status == "error" else None
    done = [files["audio"], files["transcript"], rec["drive_status"] == "done", files["course"]]
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
    if steps[2]["state"] == "done" and steps[3]["state"] == "todo":
        steps[3]["detail"] = "en attente de la tâche Claude"
    return steps


def retry_action(rec: dict) -> str | None:
    """Action du bouton « Réessayer » : relance l'étape en échec (ou termine un enregistrement interrompu)."""
    if rec["status"] == "interrupted":
        return "finalize"
    if rec["status"] != "error":
        return None
    return RETRY_ACTION.get(rec.get("error_step") or "")


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
    }


def supports_context(rid: int) -> dict:
    return {"supports": db.list_supports(rid), "support_accept": supports.ACCEPT}


def _support_or_404(rid: int, sid: int) -> dict:
    sup = db.get_support(sid)
    if not sup or sup["recording_id"] != rid:
        raise HTTPException(404, "Support introuvable.")
    return sup


def _ready_to_process() -> list[dict]:
    """Séances dont l'audio est prêt et que rien n'attend déjà dans la file (plus ancienne d'abord)."""
    return [r for r in db.list_recordings_by_status("uploaded") if not pipeline.is_active(r["id"])]


@router.get("/enregistrements", response_class=HTMLResponse)
def list_page(request: Request, vue: str = "", mois: str = "", jour: str = ""):
    agenda = vue != "liste"  # l'agenda est la vue par défaut
    return render(request, "recordings.html", **(agenda_context(mois, jour) if agenda else _list_context()),
                  view="agenda" if agenda else "liste", n_ready=len(_ready_to_process()))


@router.post("/enregistrements/tout-traiter")
def process_all(back: str = Form("")):
    """« Tout traiter » : lance le traitement de chaque séance prête ; la file les traite une à la fois."""
    target = back if back.startswith("/") else "/enregistrements"
    ready = _ready_to_process()
    if not ready:
        return redirect(target, "Aucune séance prête à traiter.")
    for rec in ready:
        db.update_recording(rec["id"], auto_retry_at=None, auto_retry_count=0)
        pipeline.submit("recording", rec["id"], "transcribe")
    n = len(ready)
    return redirect(target, f"Traitement lancé pour {n} séance{'s' if n > 1 else ''} : transcription puis "
                                        "dépôt dans Drive, une à la fois.")


@router.get("/fragments/recordings", response_class=HTMLResponse)
def list_fragment(request: Request, jour: str = ""):
    day = parse_date(jour)
    return render(request, "partials/recordings_rows.html", **_list_context(day.isoformat() if day else None))


@router.get("/fragments/agenda", response_class=HTMLResponse)
def agenda_fragment(request: Request, mois: str = "", jour: str = ""):
    """Changement de mois ou de jour dans l'agenda, sans recharger la page."""
    return render(request, "partials/agenda.html", **agenda_context(mois, jour))


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
    message = "Support ajouté." if len(added) == 1 else f"{len(added)} supports ajoutés."
    return redirect(target, " ".join([message, *errors]), "warn" if errors else "ok")


@router.get("/enregistrements/{rid}/supports/{sid}")
def support_file(rid: int, sid: int):
    sup = _support_or_404(rid, sid)
    return FileResponse(supports.file_path(sup), filename=sup["filename"], content_disposition_type="inline")


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
def course_file(rid: int):
    md = subjects.read_course(rid)
    if md is None:
        raise HTTPException(404, "Cours absent.")
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")


@router.post("/enregistrements/{rid}/action")
def run_action(rid: int, action: str = Form(...), back: str = Form("")):
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
    if action == "finalize":
        db.update_recording(rid, status="finalizing", ended_at=rec.get("ended_at") or db.now_iso())
    db.update_recording(rid, auto_retry_at=None, auto_retry_count=0)  # relance manuelle : les essais auto repartent de zéro
    pipeline.submit("recording", rid, action, chain=action in CHAINED)
    if action == "transcribe" and rec["status"] == "uploaded":
        return redirect(target, "Traitement lancé : transcription puis dépôt dans Drive.")
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
    _refresh_headers(rid, rec)
    recorder.write_meta(rid)
    return redirect(f"/enregistrements/{rid}", "Informations mises à jour." + (
        " Relancez le dépôt Drive pour renommer la transcription dans Drive." if rec.get("drive_transcription_id") else ""))


def _refresh_headers(rid: int, before: dict) -> None:
    """Répercute type/numéro/titre dans l'en-tête du cours et de la transcription."""
    rec = db.get_recording(rid)
    old_num = f" — {before['course_type']} {before['session_number']} "
    new_num = f" — {rec['course_type']} {rec['session_number']} "
    transcript = recorder.recording_dir(rid) / "transcript.txt"
    if transcript.exists():  # « # Transcription — Matière — CM 3 — 2026-10-05 »
        title, sep, rest = transcript.read_text(encoding="utf-8").partition("\n")
        if old_num in title:
            transcript.write_text(title.replace(old_num, new_num, 1) + sep + rest, encoding="utf-8")
    md = subjects.read_course(rid)
    if md is None or not rec.get("title"):
        return
    new_md = replace_title(md, f"{rec['course_type']} {rec['session_number']} – {rec['title']}")
    new_md = new_md.replace(old_num + "du ", new_num + "du ", 1)  # « *Matière — CM 3 du 5 octobre 2026 — … » sous le titre
    if new_md != md:
        subjects.course_path(rid).write_text(new_md, encoding="utf-8")


def _deposited(rec: dict) -> bool:
    return bool(rec.get("drive_transcription_id") or rec.get("drive_md_id"))


@router.post("/enregistrements/{rid}/delete")
def delete(rid: int, back: str = Form("")):
    rec = _get(rid)
    here = back if back.startswith("/") else f"/enregistrements/{rid}"
    if pipeline.is_active(rid) or rec["status"] in BUSY_STATUSES:
        return redirect(here, "Impossible de supprimer pendant un traitement.", "err")
    # Les séances suivantes reculent d'un cran (ex. faux départ supprimé : le vrai CM 3 redevient CM 2).
    later = db.sessions_to_renumber(rec)
    busy = next((r for r in later if pipeline.is_active(r["id"]) or r["status"] in BUSY_STATUSES), None)
    if busy:
        return redirect(here, f"Suppression impossible pendant le traitement du {busy['course_type']} "
                              f"{busy['session_number']} (son numéro doit changer) : réessayez une fois terminé.", "err")
    subject = db.get_subject(rec["subject_id"]) if rec.get("subject_id") else None
    recorder.delete_files(rid)
    db.delete_recording(rid)
    for r in later:
        db.update_recording(r["id"], session_number=r["session_number"] - 1)
        _refresh_headers(r["id"], r)
        recorder.write_meta(r["id"])
    if subject:
        subjects.build_full_course(db.get_subject(subject["id"]))

    msg = [f"{rec.get('subject_name') or 'Séance'} — {rec['course_type']} {rec['session_number']} supprimé."]
    if later:
        msg.append("Numéros mis à jour : " + ", ".join(
            f"{r['course_type']} {r['session_number']} → {r['course_type']} {r['session_number'] - 1}" for r in later) + ".")
        deposited = [f"{r['course_type']} {r['session_number'] - 1}" for r in later if r.get("drive_transcription_id")]
        if deposited:
            msg.append(f"Relancez le dépôt Drive de {', '.join(deposited)} pour renommer sa transcription dans Drive.")
    if _deposited(rec):
        msg.append("Ses fichiers déjà déposés dans Drive ne sont pas supprimés.")
    return redirect(back if back.startswith("/") else "/enregistrements", " ".join(msg))
