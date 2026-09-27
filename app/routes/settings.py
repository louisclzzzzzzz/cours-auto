"""Page « Paramètres » : emploi du temps, Google Drive, Notion, Mistral, options."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from .. import calendar_ics, config, db, llm, subjects
from ..pipeline import pipeline
from ..publish import drive, notion
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
        notion_token=bool(config.notion_token()),
        notion_root_id=notion.extract_id(values["notion_root"]),
        notion_urls={k: db.get_setting(f"notion_{k}_url") for k in ("matieres", "seances")},
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
        return redirect("/parametres", f"Fuseau horaire inconnu : {timezone}", "err")
    db.set_setting("ics_url", ics_url.strip())
    db.set_setting("timezone", timezone.strip())
    if ics_url.strip():
        ok, message = calendar_ics.refresh_from_url()
        return redirect("/parametres", message, "ok" if ok else "err")
    return redirect("/parametres", "Paramètres de l'emploi du temps enregistrés.")


@router.post("/parametres/edt/fichier")
async def upload_calendar(file: UploadFile = File(...)):
    data = await file.read()
    try:
        n = calendar_ics.save_calendar(data, "file")
    except Exception as exc:  # noqa: BLE001
        return redirect("/parametres", f"Fichier .ics invalide : {exc}", "err")
    return redirect("/parametres", f"Emploi du temps importé ({n} événements).")


@router.post("/parametres/modeles")
def save_models(
    llm_model: str = Form(...),
    transcription_model: str = Form(...),
    transcription_language: str = Form(""),
    llm_chunk_chars: int = Form(60000),
    llm_max_tokens: int = Form(32000),
    llm_reasoning_effort: str = Form("high"),
    audio_bitrate: str = Form("48k"),
):
    if not re.fullmatch(r"\d{2,3}k", audio_bitrate.strip()):
        return redirect("/parametres", "Débit audio invalide (ex. 48k).", "err")
    if llm_reasoning_effort not in ("", "none", "high"):
        return redirect("/parametres", "Niveau de raisonnement invalide.", "err")
    db.set_setting("llm_model", llm_model.strip() or config.DEFAULT_SETTINGS["llm_model"])
    db.set_setting("llm_reasoning_effort", llm_reasoning_effort)
    db.set_setting("transcription_model", transcription_model.strip() or "voxtral-mini-latest")
    db.set_setting("transcription_language", transcription_language.strip())
    db.set_setting("llm_chunk_chars", str(max(llm_chunk_chars, 5000)))
    db.set_setting("llm_max_tokens", str(max(llm_max_tokens, 2000)))
    db.set_setting("audio_bitrate", audio_bitrate.strip())
    return redirect("/parametres", "Paramètres Mistral enregistrés.")


@router.post("/parametres/mistral/test", response_class=HTMLResponse)
def test_mistral(request: Request):
    ok, message, models = llm.test_api_key()
    return render(request, "partials/test_result.html", ok=ok, message=message, models=models)


@router.post("/parametres/drive")
def save_drive(drive_root_name: str = Form("Cours M1"), drive_notebooklm: str = Form(""),
               drive_upload_sources: str = Form("")):
    db.set_setting("drive_root_name", drive_root_name.strip() or "Cours M1")
    db.set_setting("drive_notebooklm", "1" if drive_notebooklm else "0")
    db.set_setting("drive_upload_sources", "1" if drive_upload_sources else "0")
    return redirect("/parametres", "Options Drive enregistrées.")


def _requeue_failed_drive() -> None:
    """Après connexion : republie sur Drive les séances dont la publication Drive avait échoué."""
    for rec in db.q("SELECT id FROM recordings WHERE drive_status = 'error'"):
        if subjects.course_path(rec["id"]).exists():
            pipeline.submit("recording", rec["id"], "publish_drive", chain=False)


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
        return redirect("/parametres", f"Connexion impossible : {exc}", "err")
    return redirect("/parametres")


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
    return redirect("/parametres", "Google Drive déconnecté (token.json supprimé).")


@router.post("/parametres/notion")
def save_notion(notion_root: str = Form("")):
    if notion_root.strip() and not notion.extract_id(notion_root):
        return redirect("/parametres", "URL ou ID de page Notion non reconnu.", "err")
    db.set_setting("notion_root", notion_root.strip())
    return redirect("/parametres", "Page racine Notion enregistrée.")


@router.post("/parametres/notion/test", response_class=HTMLResponse)
def test_notion(request: Request):
    ok, message = notion.test_connection()
    return render(request, "partials/test_result.html", ok=ok, message=message, models=[])


@router.post("/parametres/rappel")
def reset_reminder():
    db.set_setting("consent_reminder_dismissed", "0")
    return redirect("/", "Le rappel de consentement sera de nouveau affiché.")
