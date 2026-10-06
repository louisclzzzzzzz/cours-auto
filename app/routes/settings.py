"""Page « Paramètres » : emploi du temps, Google Drive, Mistral (transcription), options."""

from __future__ import annotations

import os
import re
import signal
import threading
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from .. import calendar_ics, config, db, recorder, transcribe
from ..pipeline import pipeline
from ..publish import drive
from ..web import redirect, render

router = APIRouter()


@router.get("/parametres", response_class=HTMLResponse)
def settings_page(request: Request, check: bool = False):
    keys = list(config.DEFAULT_SETTINGS)
    values = {k: db.get_setting(k) for k in keys}
    auth = drive.auth_session()
    drive.clear_auth_session()  # un résultat (réussite/échec) ne s'affiche qu'une fois
    return render(
        request, "settings.html",
        s=values,
        drive_status=drive.connection_status(force=check or auth.get("status") == "done"),
        drive_auth=auth,
        drive_links=drive.console_links(),
        drive_project=drive.project_id(),
        drive_client_type=drive.client_type(),
        drive_failed=db.q1("SELECT COUNT(*) AS n FROM recordings WHERE drive_status = 'error'")["n"],
        drive_root_url=db.get_setting("drive_root_url"),
        mistral_key=bool(config.mistral_api_key()),
        ics_last=db.get_setting("ics_last_refresh"),
        ics_error=db.get_setting("ics_last_error"),
        ics_source=db.get_setting("ics_source"),
        has_calendar=config.CALENDAR_CACHE.exists(),
    )


@router.post("/parametres/edt")
def save_calendar_settings(ics_url: str = Form(""), timezone: str = Form("Europe/Paris")):
    try:
        ZoneInfo(timezone.strip())
    except Exception:  # noqa: BLE001
        return redirect("/parametres#edt", f"Fuseau horaire inconnu : {timezone}", "err")
    db.set_setting("ics_url", ics_url.strip())
    db.set_setting("timezone", timezone.strip())
    if ics_url.strip():
        ok, message = calendar_ics.refresh_from_url()
        return redirect("/parametres#edt", message, "ok" if ok else "err")
    return redirect("/parametres#edt", "Paramètres de l'emploi du temps enregistrés.")


@router.post("/parametres/edt/fichier")
async def upload_calendar(file: UploadFile = File(...)):
    data = await file.read()
    try:
        n = calendar_ics.save_calendar(data, "file")
    except Exception as exc:  # noqa: BLE001
        return redirect("/parametres#edt", f"Fichier .ics invalide : {exc}", "err")
    return redirect("/parametres#edt", f"Emploi du temps importé ({n} événements).")


@router.post("/parametres/modeles")
def save_models(
    transcription_model: str = Form(...),
    transcription_language: str = Form(""),
    audio_bitrate: str = Form("48k"),
):
    if not re.fullmatch(r"\d{2,3}k", audio_bitrate.strip()):
        return redirect("/parametres#mistral", "Débit audio invalide (ex. 48k).", "err")
    db.set_setting("transcription_model", transcription_model.strip() or "voxtral-mini-latest")
    db.set_setting("transcription_language", transcription_language.strip())
    db.set_setting("audio_bitrate", audio_bitrate.strip())
    return redirect("/parametres#mistral", "Réglages de la transcription enregistrés.")


@router.post("/parametres/mistral/test", response_class=HTMLResponse)
def test_mistral(request: Request):
    ok, message, models = transcribe.test_api_key()
    return render(request, "partials/test_result.html", ok=ok, message=message, models=models)


@router.post("/parametres/drive")
def save_drive(drive_root_name: str = Form("Cours M1")):
    db.set_setting("drive_root_name", drive_root_name.strip() or "Cours M1")
    return redirect("/parametres#drive", "Options Drive enregistrées.")


def _requeue_failed_drive() -> None:
    """Après connexion : redépose dans Drive les séances dont le dépôt avait échoué, et récupère les cours."""
    for rec in db.q("SELECT id FROM recordings WHERE drive_status = 'error'"):
        if (recorder.recording_dir(rec["id"]) / "transcript.txt").exists():
            pipeline.submit("recording", rec["id"], "publish", chain=False)
    drive.sync_state["at"] = 0.0  # prochaine récupération des cours dès le prochain passage de la file


def _drive_auth_fragment(request: Request):
    auth = drive.auth_session()
    response = render(request, "partials/drive_auth.html", auth=auth, links=drive.console_links())
    if auth.get("status") in ("done", "error"):
        response.headers["HX-Refresh"] = "true"  # recharge Paramètres pour afficher le nouvel état
    return response


@router.post("/drive/connect", response_class=HTMLResponse)
def drive_connect(request: Request):
    try:
        drive.start_browser_auth(on_success=_requeue_failed_drive)
    except Exception as exc:  # noqa: BLE001
        return render(request, "partials/drive_auth.html", auth={"status": "error", "message": f"Connexion impossible : {exc}"},
                      links=drive.console_links())
    return render(request, "partials/drive_auth.html", auth=drive.auth_session(), links=drive.console_links())


@router.get("/drive/connect")
def drive_connect_link():
    try:
        drive.start_browser_auth(on_success=_requeue_failed_drive)
    except Exception as exc:  # noqa: BLE001
        return redirect("/parametres#drive", f"Connexion impossible : {exc}", "err")
    return redirect("/parametres#drive")


@router.post("/drive/connect/cancel", response_class=HTMLResponse)
def drive_connect_cancel(request: Request):
    drive.cancel_browser_auth()
    return render(request, "partials/drive_auth.html", auth={"status": "error", "message": "Connexion annulée."},
                  links=drive.console_links())


@router.get("/fragments/drive-auth", response_class=HTMLResponse)
def drive_auth_fragment(request: Request):
    return _drive_auth_fragment(request)


@router.post("/drive/disconnect")
def drive_disconnect():
    drive.disconnect()
    return redirect("/parametres#drive", "Google Drive déconnecté (token.json supprimé).")


@router.post("/parametres/rappel")
def reset_reminder():
    db.set_setting("consent_reminder_dismissed", "0")
    return redirect("/", "Le rappel de consentement sera de nouveau affiché.")


QUIT_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Cours auto arrêté</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; background: #f7f3ec; color: #2a2521; display: grid; place-items: center;
min-height: 90vh; text-align: center"><div><h1 style="font-size: 1.4rem">Cours auto est arrêté.</h1>
<p style="color: #7a7066">Vous pouvez fermer cet onglet. Pour le relancer : un clic sur l'icône « Cours auto ».</p></div></body></html>"""


def _stop_server() -> None:
    """Arrêt propre (comme Ctrl+C) : la file de traitement s'arrête, un traitement en cours reprendra au lancement."""
    os.kill(os.getpid(), signal.SIGINT)


@router.post("/quitter", response_class=HTMLResponse)
def quit_app():
    threading.Timer(0.5, _stop_server).start()  # laisse le temps d'envoyer la page
    return HTMLResponse(QUIT_PAGE)
