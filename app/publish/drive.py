"""Google Drive : archive Markdown + Google Doc miroir pour NotebookLM.

Scope `drive.file` uniquement : l'app ne voit que les fichiers qu'elle a créés, donc tous les
identifiants (dossiers, fichiers) sont stockés en base.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from .. import config, db, recorder, subjects
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

_flows: dict[str, Flow] = {}
_flows_lock = threading.Lock()
_status_cache: dict = {"at": 0.0, "value": None}


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


def start_auth(redirect_uri: str) -> str:
    if not is_configured():
        raise PublishSkipped("credentials.json absent : voir le README pour le créer.")
    flow = Flow.from_client_secrets_file(str(config.CREDENTIALS_FILE), scopes=SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    with _flows_lock:
        _flows[state] = flow
    return url


def has_pending_flow(state: str | None) -> bool:
    with _flows_lock:
        return bool(state) and state in _flows


def finish_auth(state: str, code: str) -> None:
    with _flows_lock:
        flow = _flows.pop(state, None)
    if flow is None:
        raise DriveAuthError("Session de connexion inconnue ou expirée : recommencez depuis Paramètres.")
    flow.fetch_token(code=code)
    _save(flow.credentials)
    _status_cache["at"] = 0.0


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
            creds = load_credentials()
            about = build("drive", "v3", credentials=creds, cache_discovery=False).about().get(fields="user").execute()
            email = about.get("user", {}).get("emailAddress", "")
            value = {"configured": True, "connected": True, "message": f"Connecté ({email})" if email else "Connecté"}
        except (DriveAuthError, PublishSkipped) as exc:
            value = {"configured": True, "connected": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - hors ligne…
            value = {"configured": True, "connected": config.TOKEN_FILE.exists(), "message": f"Vérification impossible : {exc}"}
    _status_cache.update(at=time.time(), value=value)
    return value


# --- Client ----------------------------------------------------------------------------------------

class DriveClient:
    def __init__(self, creds: Credentials | None = None, service=None):
        self.svc = service or build("drive", "v3", credentials=creds or load_credentials(), cache_discovery=False)

    @staticmethod
    def _exec(request):
        return request.execute(num_retries=5)

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


def publish_subject(subject_id: int, client: DriveClient | None = None, recording_id: int | None = None) -> dict:
    """Régénère et envoie le cours complet, `_etat.md` et le Google Doc NotebookLM de la matière."""
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
