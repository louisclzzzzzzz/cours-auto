"""Google Drive : archive Markdown + Google Doc miroir pour NotebookLM.

Scope `drive.file` uniquement : l'app ne voit que les fichiers qu'elle a créés, donc tous les
identifiants (dossiers, fichiers) sont stockés en base.

Mode « automatisation » (paramètre `drive_writer`) : l'app dépose seulement les transcriptions et les
supports de cours (dossiers `Transcriptions/` et `Supports/` de chaque matière) ; une automatisation externe
rédige les séances, le cours complet, `_etat.md` et le Google Doc NotebookLM, que l'app ne touche plus.
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import threading
import time
import webbrowser
import wsgiref.simple_server
from typing import Callable
from urllib.parse import parse_qs

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from .. import config, db, recorder, subjects, supports
from ..textutils import fmt_duration, fmt_time, fr_date
from . import PublishSkipped

log = logging.getLogger(__name__)

# Google peut renvoyer les scopes dans un ordre différent : ne pas en faire une erreur.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
FIELDS = "id, name, webViewLink, mimeType, trashed"


class DriveAuthError(RuntimeError):
    pass


# --- Authentification -----------------------------------------------------------------------------
#
# Flux « application de bureau » recommandé par Google : la page de connexion s'ouvre dans le navigateur
# habituel de l'utilisateur (Google refuse souvent la connexion dans les navigateurs intégrés) et un
# serveur temporaire sur 127.0.0.1 (port libre) reçoit la redirection contenant l'autorisation.

AUTH_TIMEOUT_S = 300
_status_cache: dict = {"at": 0.0, "value": None}
_auth_lock = threading.Lock()
_auth: dict = {}  # connexion en cours : status (waiting|done|error), url, message, started, browser_opened

_CALLBACK_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Cours auto</title></head>
<body style="font-family:system-ui,sans-serif;max-width:34rem;margin:4rem auto;line-height:1.5;padding:0 1rem">
<h2>{title}</h2><p>{message}</p><p><a href="http://127.0.0.1:{port}/parametres">Revenir à Cours auto</a></p></body></html>"""


def is_configured() -> bool:
    return config.CREDENTIALS_FILE.exists()


def _save(creds: Credentials) -> None:
    config.TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(config.TOKEN_FILE, 0o600)
    except OSError:
        pass


def load_credentials() -> Credentials:
    if not is_configured():
        raise PublishSkipped("Drive non configuré (credentials.json absent).")
    if not config.TOKEN_FILE.exists():
        raise DriveAuthError("Drive non connecté : connectez-vous dans Paramètres, puis relancez la publication.")
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_FILE), SCOPES)
    except ValueError as exc:
        raise DriveAuthError(f"token.json invalide ({exc}) : reconnectez Drive dans Paramètres.") from exc
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise DriveAuthError(
                    "Autorisation Google expirée ou révoquée : reconnectez Drive dans Paramètres "
                    "(si cela arrive tous les 7 jours, publiez l'app OAuth en « Production »)."
                ) from exc
            _save(creds)
        else:
            raise DriveAuthError("Autorisation Google invalide : reconnectez Drive dans Paramètres.")
    return creds


def _client_config() -> dict:
    try:
        return json.loads(config.CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def client_type() -> str | None:
    """« installed » (application de bureau, attendu) ou « web »."""
    data = _client_config()
    return next((k for k in ("installed", "web") if k in data), None)


def project_id() -> str | None:
    data = _client_config()
    return (data.get("installed") or data.get("web") or {}).get("project_id")


def console_links() -> dict:
    """Pages utiles de la console Google Cloud, pour le projet de credentials.json."""
    pid = project_id()
    q = f"?project={pid}" if pid else ""
    return {
        "api": f"https://console.cloud.google.com/apis/library/drive.googleapis.com{q}",
        "audience": f"https://console.cloud.google.com/auth/audience{q}",
        "scopes": f"https://console.cloud.google.com/auth/scopes{q}",
        "clients": f"https://console.cloud.google.com/auth/clients{q}",
    }


def explain_oauth_error(error: str, description: str = "") -> str:
    if error == "access_denied":
        return (
            "Google a refusé l'accès. Si vous n'avez pas cliqué sur « Annuler », l'application OAuth est "
            "probablement en mode « Test » sans votre adresse parmi les utilisateurs test : publiez-la en "
            "Production (recommandé) ou ajoutez votre adresse dans « Audience », puis recommencez."
        )
    return f"Google a renvoyé l'erreur « {error} »" + (f" : {description}" if description else "") + "."


class _CallbackApp:
    """Mini-application WSGI qui reçoit la redirection de Google (code d'autorisation ou erreur)."""

    def __init__(self) -> None:
        self.query: dict | None = None

    def __call__(self, environ, start_response):
        q = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        if "code" not in q and "error" not in q:  # favicon, etc.
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b""]
        self.query = q
        if "code" in q:
            title, message = "✅ Connexion à Google Drive réussie", "Vous pouvez fermer cet onglet et revenir à Cours auto."
        else:
            title, message = "❌ Connexion refusée", html.escape(explain_oauth_error(q["error"], q.get("error_description", "")))
        body = _CALLBACK_PAGE.format(title=title, message=message, port=config.PORT)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args) -> None:  # pas de journal par requête
        pass


def auth_session() -> dict:
    with _auth_lock:
        return dict(_auth)


def clear_auth_session() -> None:
    with _auth_lock:
        if _auth.get("status") != "waiting":
            _auth.clear()


def cancel_browser_auth() -> None:
    with _auth_lock:
        if _auth.get("status") == "waiting":
            _auth["cancelled"] = True


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=1, autoraise=True))
    except Exception:  # noqa: BLE001
        return False


def start_browser_auth(open_browser: bool = True, on_success: Callable[[], None] | None = None) -> dict:
    """Lance la connexion : ouvre la page Google dans le navigateur par défaut et attend la redirection
    en tâche de fond (AUTH_TIMEOUT_S). Renvoie l'état de la connexion (voir `auth_session`)."""
    if not is_configured():
        raise PublishSkipped("credentials.json absent : voir le README pour le créer.")
    with _auth_lock:
        if (_auth.get("status") == "waiting" and not _auth.get("cancelled")
                and time.time() - _auth["started"] < AUTH_TIMEOUT_S):
            url = _auth["url"]  # connexion déjà en cours : on rouvre simplement la page Google
        else:
            url = None
    if url:
        if open_browser:
            _open_browser(url)
        return auth_session()

    flow = Flow.from_client_secrets_file(str(config.CREDENTIALS_FILE), scopes=SCOPES)
    app = _CallbackApp()
    server = wsgiref.simple_server.make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
    flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    with _auth_lock:
        _auth.clear()
        _auth.update(status="waiting", url=url, message="", started=time.time(), browser_opened=False)

    def wait_for_google() -> None:
        status, message = "error", "Échec de la connexion."
        try:
            server.timeout = 1
            deadline = time.time() + AUTH_TIMEOUT_S
            while app.query is None and time.time() < deadline and not _auth.get("cancelled"):
                server.handle_request()
            q = app.query
            if q is None and _auth.get("cancelled"):
                message = "Connexion annulée."
            elif q is None:
                # Google n'a pas renvoyé vers l'app : c'est le cas de l'écran « Accès bloqué » (app en mode Test).
                message = (
                    "Délai dépassé : Google n'a pas renvoyé l'autorisation. S'il a affiché « Accès bloqué : … n'a pas "
                    "terminé la procédure de validation », publiez l'application OAuth (Audience) ou ajoutez votre "
                    "adresse aux utilisateurs test, puis recommencez."
                )
            elif "error" in q:
                message = explain_oauth_error(q["error"], q.get("error_description", ""))
            elif q.get("state") != state:
                message = "Réponse de Google inattendue (jeton d'état différent) : recommencez."
            else:
                flow.fetch_token(code=q["code"])
                _save(flow.credentials)
                _status_cache["at"] = 0.0
                status, message = "done", "Google Drive est connecté."
        except Exception as exc:  # noqa: BLE001
            log.exception("Connexion Google Drive en échec")
            message = f"Échec de la connexion : {exc}"
        finally:
            server.server_close()
        with _auth_lock:
            _auth.update(status=status, message=message)
        if status == "done" and on_success:
            try:
                on_success()
            except Exception:  # noqa: BLE001
                log.exception("Relance des publications Drive impossible")

    threading.Thread(target=wait_for_google, name="drive-auth", daemon=True).start()
    if open_browser:
        opened = _open_browser(url)
        with _auth_lock:
            _auth["browser_opened"] = opened
    return auth_session()


def disconnect() -> None:
    if config.TOKEN_FILE.exists():
        config.TOKEN_FILE.unlink()
    _status_cache["at"] = 0.0


def connection_status(force: bool = False) -> dict:
    """État affiché dans l'UI (mis en cache 60 s pour ne pas appeler Google à chaque page)."""
    if not force and _status_cache["value"] and time.time() - _status_cache["at"] < 60:
        return _status_cache["value"]
    if not is_configured():
        value = {"configured": False, "connected": False, "message": "credentials.json absent (voir README)."}
    elif not config.TOKEN_FILE.exists():
        value = {"configured": True, "connected": False, "message": "Non connecté"}
    else:
        try:
            client = DriveClient(load_credentials())
            about = client._exec(client.svc.about().get(fields="user"))
            email = about.get("user", {}).get("emailAddress", "")
            value = {"configured": True, "connected": True, "message": f"Connecté ({email})" if email else "Connecté"}
        except (DriveAuthError, PublishSkipped) as exc:
            value = {"configured": True, "connected": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - hors ligne…
            value = {"configured": True, "connected": config.TOKEN_FILE.exists(), "message": f"Vérification impossible : {exc}"}
    _status_cache.update(at=time.time(), value=value)
    return value


# --- Client ----------------------------------------------------------------------------------------

def raise_friendly(exc: HttpError) -> None:
    """Transforme les erreurs de configuration Google Cloud en message clair (avec le lien à suivre)."""
    content = (exc.content or b"").decode("utf-8", "ignore") if isinstance(exc.content, bytes) else str(exc.content)
    if exc.resp.status == 403 and any(k in content for k in ("accessNotConfigured", "SERVICE_DISABLED", "has not been used")):
        raise DriveAuthError(
            "L'API Google Drive n'est pas activée dans le projet Google Cloud : activez-la "
            f"({console_links()['api']}), attendez une minute, puis relancez."
        ) from exc
    if exc.resp.status == 401:
        raise DriveAuthError("Autorisation Google refusée : reconnectez Drive dans Paramètres.") from exc

class DriveClient:
    def __init__(self, creds: Credentials | None = None, service=None):
        self.svc = service or build("drive", "v3", credentials=creds or load_credentials(), cache_discovery=False)

    @staticmethod
    def _exec(request):
        try:
            return request.execute(num_retries=5)
        except HttpError as exc:
            raise_friendly(exc)
            raise

    def get(self, file_id: str | None) -> dict | None:
        if not file_id:
            return None
        try:
            f = self._exec(self.svc.files().get(fileId=file_id, fields=FIELDS))
        except HttpError as exc:
            if exc.resp.status in (404, 403):
                return None
            raise
        return None if f.get("trashed") else f

    def ensure_folder(self, name: str, parent_id: str | None, existing_id: str | None) -> dict:
        existing = self.get(existing_id)
        if existing:
            if existing.get("name") != name:
                existing = self._exec(self.svc.files().update(fileId=existing["id"], body={"name": name}, fields=FIELDS))
            return existing
        body: dict = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        return self._exec(self.svc.files().create(body=body, fields=FIELDS))

    def upsert_text(self, name: str, parent_id: str, content: str, existing_id: str | None,
                    mimetype: str = "text/markdown") -> dict:
        media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype=mimetype, resumable=False)
        if self.get(existing_id):
            return self._exec(
                self.svc.files().update(fileId=existing_id, body={"name": name}, media_body=media, fields=FIELDS)
            )
        return self._exec(
            self.svc.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields=FIELDS)
        )

    def upsert_file(self, name: str, parent_id: str, path, mimetype: str, existing_id: str | None) -> dict:
        media = MediaFileUpload(str(path), mimetype=mimetype, resumable=True)
        if self.get(existing_id):
            return self._exec(
                self.svc.files().update(fileId=existing_id, body={"name": name}, media_body=media, fields=FIELDS)
            )
        return self._exec(
            self.svc.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields=FIELDS)
        )

    def upsert_gdoc(self, name: str, parent_id: str, markdown: str, existing_id: str | None) -> tuple[dict, bool]:
        """Google Doc converti depuis le Markdown ; mis à jour sur le même ID (NotebookLM garde la source).

        Renvoie (fichier, créé?)."""
        existing = self.get(existing_id)
        for mimetype in ("text/markdown", "text/plain"):
            media = MediaIoBaseUpload(io.BytesIO(markdown.encode("utf-8")), mimetype=mimetype, resumable=True)
            try:
                if existing:
                    f = self._exec(self.svc.files().update(
                        fileId=existing["id"], body={"name": name}, media_body=media, fields=FIELDS))
                    return f, False
                f = self._exec(self.svc.files().create(
                    body={"name": name, "mimeType": GDOC_MIME, "parents": [parent_id]}, media_body=media, fields=FIELDS))
                return f, True
            except HttpError as exc:
                # Conversion Markdown refusée : repli en texte brut (le contenu reste lisible par NotebookLM).
                if mimetype == "text/markdown" and exc.resp.status == 400:
                    log.warning("Conversion Markdown → Google Doc refusée (%s), repli en texte brut.", exc)
                    continue
                raise
        raise RuntimeError("unreachable")


# --- Publication ------------------------------------------------------------------------------------

def _root_folder(client: DriveClient) -> dict:
    name = db.get_setting("drive_root_name") or "Cours M1"
    root = client.ensure_folder(name, None, db.get_setting("drive_root_id") or None)
    db.set_setting("drive_root_id", root["id"])
    db.set_setting("drive_root_url", root.get("webViewLink", ""))
    return root


def _subject_folders(client: DriveClient, subject: dict) -> dict:
    root = _root_folder(client)
    folder = client.ensure_folder(subject["name"], root["id"], subject.get("drive_folder_id"))
    sessions = client.ensure_folder("Séances", folder["id"], subject.get("drive_sessions_folder_id"))
    fields = {
        "drive_folder_id": folder["id"], "drive_folder_url": folder.get("webViewLink"),
        "drive_sessions_folder_id": sessions["id"], "drive_sessions_folder_url": sessions.get("webViewLink"),
    }
    db.update_subject(subject["id"], **fields)
    return {**subject, **fields}


def automation_mode() -> bool:
    return db.get_setting("drive_writer") == "automatisation"


TRANSCRIPTIONS_FOLDER = "Transcriptions"
SUPPORTS_FOLDER = "Supports"


def _session_prefix(rec: dict) -> str:
    """Ex. `2026-09-28_CM03` (date, type et numéro de la séance)."""
    return f"{rec['session_date']}_{rec['course_type']}{int(rec['session_number'] or 0):02d}"


def transcription_filename(rec: dict) -> str:
    return f"{_session_prefix(rec)}_transcription.txt"


def support_filename(rec: dict, sup: dict) -> str:
    return f"{_session_prefix(rec)}_{sup['filename']}"


def transcription_document(rec: dict) -> str | None:
    """Transcription lisible précédée des informations de séance connues de l'app (emploi du temps, supports)."""
    path = recorder.recording_dir(rec["id"]) / "transcript.txt"
    if not path.exists():
        return None
    subject = db.get_subject(rec["subject_id"]) or {}
    info = [
        "Séance (informations de l'app d'enregistrement) :",
        f"- Matière : {subject.get('name', '')}",
        f"- Séance : {rec['course_type']} {rec['session_number']} (numérotation de l'app)",
        f"- Date : {fr_date(rec['session_date'])}",
    ]
    if rec.get("event_start") and rec.get("event_end"):
        info.append(f"- Horaire : {fmt_time(rec['event_start'])} – {fmt_time(rec['event_end'])}")
    if rec.get("location"):
        info.append(f"- Salle : {rec['location']}")
    info.append(f"- Enseignant : {rec.get('teacher') or subject.get('teachers') or 'non renseigné'}")
    if rec.get("event_summary"):
        info.append(f"- Intitulé dans l'emploi du temps : {rec['event_summary']}")
    info.append(f"- Durée de l'enregistrement : {fmt_duration(rec.get('duration_seconds') or rec.get('elapsed_seconds'))}")
    sups = db.list_supports(rec["id"])
    if sups:
        info.append(f"- Support(s) de cours, dossier « {SUPPORTS_FOLDER} » : "
                    + ", ".join(support_filename(rec, s) for s in sups))
    text = path.read_text(encoding="utf-8")
    title, sep, rest = text.partition("\n")
    if title.startswith("# "):
        return f"{title}\n\n" + "\n".join(info) + f"\n{sep}{rest}"
    return "\n".join(info) + "\n\n" + text


def deposit_inputs(recording_id: int, client: DriveClient | None = None) -> dict:
    """Dépose la transcription et les supports de la séance dans `Transcriptions/` et `Supports/`.

    La transcription est remplacée à chaque dépôt (même fichier) ; un support déjà déposé n'est pas renvoyé.
    """
    rec = db.get_recording(recording_id)
    document = transcription_document(rec)
    sups = [s for s in db.list_supports(recording_id) if s["stored_name"] and supports.file_path(s).exists()]
    if document is None and not sups:
        return {}
    client = client or DriveClient()
    subject = _subject_folders(client, db.get_subject(rec["subject_id"]))
    fields: dict = {}
    if document is not None:
        folder = client.ensure_folder(TRANSCRIPTIONS_FOLDER, subject["drive_folder_id"],
                                      subject.get("drive_transcriptions_folder_id"))
        db.update_subject(subject["id"], drive_transcriptions_folder_id=folder["id"],
                          drive_transcriptions_folder_url=folder.get("webViewLink"))
        f = client.upsert_text(transcription_filename(rec), folder["id"], document, rec.get("drive_transcription_id"),
                               mimetype="text/plain")
        fields.update(drive_transcription_id=f["id"], drive_transcription_url=f.get("webViewLink"))
        db.update_recording(recording_id, **fields)
    pending = [s for s in sups if not client.get(s.get("drive_id"))]
    if pending:
        folder = client.ensure_folder(SUPPORTS_FOLDER, subject["drive_folder_id"], subject.get("drive_supports_folder_id"))
        db.update_subject(subject["id"], drive_supports_folder_id=folder["id"],
                          drive_supports_folder_url=folder.get("webViewLink"))
        for sup in pending:
            path = supports.file_path(sup)
            f = client.upsert_file(support_filename(rec, sup), folder["id"], path,
                                   supports.MIMETYPES.get(path.suffix.lower(), "application/octet-stream"), None)
            db.update_support(sup["id"], drive_id=f["id"], drive_url=f.get("webViewLink"))
    return fields


def _deposit_safely(recording_id: int) -> None:
    try:
        deposit_inputs(recording_id)
        db.log(recording_id, "Support(s) de cours déposé(s) dans Drive (dossier Supports).")
    except Exception as exc:  # noqa: BLE001 - nouvel essai à la prochaine publication
        db.log(recording_id, f"Dépôt des supports dans Drive impossible pour l'instant : {exc}", "warning")


def deposit_in_background(recording_id: int) -> None:
    """Mode automatisation : support ajouté après le dépôt de la transcription → envoyé tout de suite."""
    rec = db.get_recording(recording_id)
    if automation_mode() and is_configured() and rec and rec.get("drive_transcription_id"):
        threading.Thread(target=_deposit_safely, args=(recording_id,), name=f"depot-{recording_id}", daemon=True).start()


def publish_subject(subject_id: int, client: DriveClient | None = None, recording_id: int | None = None) -> dict:
    """Régénère et envoie le cours complet, `_etat.md` et le Google Doc NotebookLM de la matière."""
    if automation_mode():
        raise PublishSkipped("Mode automatisation : le cours complet, _etat.md et le Google Doc NotebookLM "
                             "sont rédigés par l'automatisation.")
    client = client or DriveClient()
    subject = _subject_folders(client, db.get_subject(subject_id))
    full_md = subjects.build_full_course(subject)
    full = client.upsert_text(subjects.full_course_name(subject), subject["drive_folder_id"], full_md,
                              subject.get("drive_full_id"))
    fields = {"drive_full_id": full["id"], "drive_full_url": full.get("webViewLink")}
    state_md = subjects.read_state(subject)
    if state_md:
        state = client.upsert_text("_etat.md", subject["drive_folder_id"], state_md, subject.get("drive_state_id"))
        fields.update(drive_state_id=state["id"], drive_state_url=state.get("webViewLink"))
    if db.get_bool_setting("drive_notebooklm"):
        had_doc = bool(subject.get("drive_gdoc_id"))
        doc, created = client.upsert_gdoc(subjects.notebooklm_doc_name(subject), subject["drive_folder_id"], full_md,
                                          subject.get("drive_gdoc_id"))
        fields.update(drive_gdoc_id=doc["id"], drive_gdoc_url=doc.get("webViewLink"))
        if created and had_doc:
            db.log(recording_id, "Le Google Doc NotebookLM avait disparu : un nouveau a été créé, "
                                 "ajoutez-le à nouveau comme source dans NotebookLM.", "warning")
    db.update_subject(subject_id, **fields)
    return db.get_subject(subject_id)


def publish_recording(recording_id: int, client: DriveClient | None = None) -> None:
    if automation_mode():  # séances, cours complet, _etat.md et Google Doc : rédigés par l'automatisation
        rec = db.get_recording(recording_id)
        if rec.get("drive_md_id") and not rec.get("drive_transcription_id"):
            raise PublishSkipped("Séance déjà rédigée dans Drive par l'app avant le mode automatisation : sa "
                                 "transcription n'est pas déposée (l'automatisation la rédigerait une seconde fois).")
        if not deposit_inputs(recording_id, client):
            raise RuntimeError("Aucune transcription à déposer dans Drive.")
        return
    rec = db.get_recording(recording_id)
    course = subjects.read_course(recording_id)
    if course is None:
        raise RuntimeError("Aucun cours mis en forme à publier.")
    client = client or DriveClient()
    subject = _subject_folders(client, db.get_subject(rec["subject_id"]))
    parent = subject["drive_sessions_folder_id"]

    f = client.upsert_text(subjects.session_filename(rec), parent, course, rec.get("drive_md_id"))
    fields = {"drive_md_id": f["id"], "drive_md_url": f.get("webViewLink"), "drive_md_name": f.get("name")}
    annotated = subjects.read_course(recording_id, prefer_annotated=True) if subjects.annotated_path(recording_id).exists() else None
    if annotated:
        a = client.upsert_text(subjects.annotated_filename(rec), parent, annotated, rec.get("drive_annot_id"))
        fields.update(drive_annot_id=a["id"], drive_annot_url=a.get("webViewLink"))

    if db.get_bool_setting("drive_upload_sources"):
        sources = client.ensure_folder("Sources", subject["drive_folder_id"], subject.get("drive_sources_folder_id"))
        db.update_subject(subject["id"], drive_sources_folder_id=sources["id"],
                          drive_sources_folder_url=sources.get("webViewLink"))
        stem = subjects.session_filename(rec)[:-3]
        audio = recorder.audio_path(recording_id)
        if audio.exists():
            a = client.upsert_file(f"{stem}.mp3", sources["id"], audio, "audio/mpeg", rec.get("drive_audio_id"))
            fields.update(drive_audio_id=a["id"], drive_audio_url=a.get("webViewLink"))
        transcript = recorder.recording_dir(recording_id) / "transcript.txt"
        if transcript.exists():
            t = client.upsert_text(f"{stem}_transcription.txt", sources["id"], transcript.read_text(encoding="utf-8"),
                                   rec.get("drive_transcript_id"), mimetype="text/plain")
            fields.update(drive_transcript_id=t["id"], drive_transcript_url=t.get("webViewLink"))

    db.update_recording(recording_id, **fields)
    publish_subject(rec["subject_id"], client, recording_id)
