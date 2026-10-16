"""Archivage de l'audio dans Google Drive, pour libérer le disque.

Une fois une séance traitée (transcrite et déposée dans Drive), son audio n'est plus utile sur le Mac : il part dans
`<racine Drive>/Z-Data/recordings/<id de la séance>/`, est vérifié (somme MD5 calculée par Drive) puis supprimé du
disque. La transcription, les supports et le cours restent en local.

Contenu archivé d'une séance : `audio.mp3` (l'audio final), `source.<ext>` (fichier importé, tel quel) et
`chunks.zip` (morceaux bruts reçus du navigateur, regroupés sans compression). Rien n'est supprimé en local avant
que *tous* ces fichiers soient vérifiés dans Drive, et rien n'est jamais supprimé ni écrasé dans Drive : un fichier
déjà présent et identique (envoi à la main, essai précédent) est simplement reconnu, un fichier différent de même nom
laisse la place à une copie `audio_2.mp3`.

Quand : au lancement de l'app, puis chaque soir (à partir de l'heure réglée) tant que l'app tourne, et à la demande
(Paramètres). L'audio d'une séance pas encore traitée n'est jamais archivé : la transcription en a besoin. Si une
étape a besoin de l'audio archivé (nouvelle transcription), il est rapatrié automatiquement ; une séance restaurée à
la main garde son audio sur le disque `RESTORE_KEEP` avant d'être de nouveau archivée.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import config, db, recorder
from .publish import PublishSkipped
from .publish import drive as drive_pub
from .textutils import fmt_size

log = logging.getLogger(__name__)

RECORDINGS_FOLDER = "recordings"
AUDIO_NAME = recorder.AUDIO_FILENAME
CHUNKS_ARCHIVE = "chunks.zip"
RESTORE_KEEP = timedelta(days=2)
RETRY_AFTER_ERROR_S = 3600
MAX_CONSECUTIVE_ERRORS = 3


class ArchiveError(RuntimeError):
    pass


class Guard(Protocol):
    """Réservation d'une séance pendant son archivage : refusée si un traitement la concerne (voir `Pipeline.hold`)."""

    def hold(self, recording_id: int) -> bool: ...

    def release(self, recording_id: int) -> None: ...


# --- Fichiers locaux --------------------------------------------------------------------------------

def audio_files(rid: int) -> dict:
    """Fichiers audio d'une séance sur le disque : audio final, fichiers importés, morceaux bruts, variantes allégées."""
    folder = recorder.recording_dir(rid)

    def real(pattern: str) -> list[Path]:
        return sorted(p for p in folder.glob(pattern) if p.is_file() and p.suffix != ".part")

    return {"final": recorder.audio_path(rid), "sources": real("source.*"), "chunks": real("chunk_*"),
            "derived": [p for p in real("audio_*.mp3")]}  # ex. audio_24k.mp3, recréable depuis l'audio final


def file_md5(path: Path) -> tuple[str, int]:
    digest, size = hashlib.md5(usedforsecurity=False), 0
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _zip_chunks(rid: int, chunks: list[Path]) -> Path:
    """Morceaux regroupés dans un seul fichier. Dates et ordre fixes : deux archives du même contenu sont identiques
    octet pour octet, ce qui permet de reconnaître un envoi précédent."""
    tmp = recorder.recording_dir(rid) / "_chunks.zip.part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path in chunks:
            info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            with path.open("rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    return tmp


# --- Qui archiver -----------------------------------------------------------------------------------

def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def is_archived(rec: dict) -> bool:
    """Audio dans Drive et plus sur le disque."""
    return bool(rec.get("audio_drive_id")) and not recorder.audio_path(rec["id"]).exists()


def eligible(now: datetime | None = None) -> list[dict]:
    """Séances traitées (transcription faite et déposée) dont l'audio est encore sur le disque."""
    now = now or datetime.now().astimezone()
    found = []
    for rec in db.list_recordings_by_status("done"):
        folder = recorder.recording_dir(rec["id"])
        if not recorder.audio_path(rec["id"]).exists() or not (folder / "transcript.txt").exists():
            continue
        restored = _parse(rec.get("audio_restored_at"))
        if restored and now - restored < RESTORE_KEEP:
            continue
        found.append(rec)
    return found


# --- Envoi vers Drive --------------------------------------------------------------------------------

def _same(f: dict, md5: str, size: int) -> bool:
    try:
        return f.get("md5Checksum") == md5 and int(f.get("size")) == size
    except (TypeError, ValueError):
        return False


def _upload_verified(client: drive_pub.DriveClient, folder_id: str, name: str, path: Path) -> dict:
    """Envoie `path` dans le dossier et vérifie la copie. Un fichier identique déjà présent est réutilisé ; un fichier
    de même nom mais différent n'est jamais écrasé (la copie s'appelle alors `nom_2.ext`, `nom_3.ext`…)."""
    md5, size = file_md5(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    base = Path(name)
    for n in range(1, 21):
        candidate = name if n == 1 else f"{base.stem}_{n}{base.suffix}"
        found = client.find_children(folder_id, candidate, folder=False)
        match = next((f for f in found if _same(f, md5, size)), None)
        if match:
            return match
        if found:
            continue
        uploaded = client.upsert_file(candidate, folder_id, path, mime, None)
        if not uploaded.get("md5Checksum"):
            uploaded = client.get(uploaded["id"]) or uploaded
        if not _same(uploaded, md5, size):
            try:
                client.trash(uploaded["id"])
            except Exception:  # noqa: BLE001 - la copie douteuse reste dans Drive, ce n'est pas bloquant
                log.warning("Copie douteuse de %s non mise à la corbeille", candidate)
            raise ArchiveError(f"« {name} » : la copie dans Drive ne correspond pas à l'original (envoi à refaire).")
        return uploaded
    raise ArchiveError(f"« {name} » : trop de fichiers de même nom différents dans le dossier Drive.")


def _recordings_folder(client: drive_pub.DriveClient) -> dict:
    """`<racine>/Z-Data/recordings` : retrouvé par son nom s'il a été créé à la main, créé sinon."""
    root = drive_pub.root_folder(client)
    name = db.get_setting("audio_archive_folder").strip() or config.DEFAULT_SETTINGS["audio_archive_folder"]
    base = client.ensure_named_folder(name, root["id"], db.get_setting("audio_archive_folder_id") or None)
    recordings = client.ensure_named_folder(RECORDINGS_FOLDER, base["id"], db.get_setting("audio_archive_recordings_id") or None)
    db.set_setting("audio_archive_folder_id", base["id"])
    db.set_setting("audio_archive_folder_url", base.get("webViewLink") or "")
    db.set_setting("audio_archive_recordings_id", recordings["id"])
    return recordings


def archive_recording(rid: int, client: drive_pub.DriveClient, recordings_folder: dict | None = None) -> dict | None:
    """Envoie, vérifie puis supprime du disque l'audio d'une séance. Renvoie {"rid", "bytes"} (None : rien à faire)."""
    rec = db.get_recording(rid)
    files = audio_files(rid)
    if not rec or not files["final"].exists():
        return None
    recordings_folder = recordings_folder or _recordings_folder(client)
    folder = client.ensure_named_folder(str(rid), recordings_folder["id"], rec.get("audio_drive_folder_id"))

    uploads = [(AUDIO_NAME, files["final"]), *[(p.name, p) for p in files["sources"]]]
    zipped = _zip_chunks(rid, files["chunks"]) if files["chunks"] else None
    try:
        if zipped:
            uploads.append((CHUNKS_ARCHIVE, zipped))
        uploaded = {name: _upload_verified(client, folder["id"], name, path) for name, path in uploads}
    finally:
        if zipped:
            zipped.unlink(missing_ok=True)

    # Tout est vérifié dans Drive : l'adresse de l'audio est enregistrée *avant* de supprimer quoi que ce soit.
    local = [*files["derived"], *files["sources"], *files["chunks"], files["final"]]  # audio final en dernier
    freed = sum(p.stat().st_size for p in local if p.exists())
    audio = uploaded[AUDIO_NAME]
    db.update_recording(rid, audio_drive_id=audio["id"], audio_drive_url=audio.get("webViewLink"),
                        audio_drive_folder_id=folder["id"], audio_drive_folder_url=folder.get("webViewLink"),
                        audio_archived_bytes=max(int(rec.get("audio_archived_bytes") or 0), freed),
                        audio_archive_error=None)
    for path in local:
        path.unlink(missing_ok=True)
    db.update_recording(rid, audio_archived_at=db.now_iso(), audio_restored_at=None)
    db.log(rid, f"Audio archivé dans Drive ({fmt_size(freed)} libérés sur le disque).")
    return {"rid": rid, "bytes": freed}


def _client() -> drive_pub.DriveClient:
    """Client Drive avec l'accès complet nécessaire à l'archivage (voir `drive.py`)."""
    creds = drive_pub.load_credentials()  # PublishSkipped / DriveAuthError : Drive à configurer ou reconnecter
    if not drive_pub.can_archive(creds):
        raise drive_pub.DriveAuthError(drive_pub.ARCHIVE_MISSING)
    return drive_pub.DriveClient(creds)


_sweep_lock = threading.Lock()


def sweep(guard: Guard, client: drive_pub.DriveClient | None = None, now: datetime | None = None) -> dict:
    """Archive toutes les séances éligibles, une par une. Les erreurs propres à une séance sont notées et n'arrêtent pas
    les autres ; un problème de connexion ou d'autorisation arrête tout (exception)."""
    result = {"archived": 0, "bytes": 0, "errors": []}
    with _sweep_lock:
        recs = eligible(now)
        if not recs:
            return result
        client = client or _client()
        recordings_folder = _recordings_folder(client)
        failures = 0
        for rec in recs:
            rid = rec["id"]
            if not guard.hold(rid):  # un traitement vient de la prendre : au prochain passage
                continue
            try:
                done = archive_recording(rid, client, recordings_folder)
            except (drive_pub.DriveAuthError, PublishSkipped):
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Archivage de l'audio de la séance %s en échec", rid)
                message = str(exc)[:500] or exc.__class__.__name__
                db.update_recording(rid, audio_archive_error=message)
                db.log(rid, f"Archivage de l'audio impossible pour l'instant : {message}", "warning")
                result["errors"].append(f"séance {rid} : {message}")
                failures += 1
                if failures >= MAX_CONSECUTIVE_ERRORS:
                    break
            else:
                failures = 0
                if done:
                    result["archived"] += 1
                    result["bytes"] += done["bytes"]
            finally:
                guard.release(rid)
    return result


# --- Rapatriement ------------------------------------------------------------------------------------

def restore_audio(rid: int, client: drive_pub.DriveClient | None = None) -> Path:
    """Télécharge l'audio final depuis Drive (si absent du disque) et vérifie sa somme MD5."""
    rec = db.get_recording(rid)
    dest = recorder.audio_path(rid)
    if dest.exists():
        return dest
    if not rec or not rec.get("audio_drive_id"):
        raise ArchiveError("Cet enregistrement n'a pas d'audio archivé dans Drive.")
    client = client or drive_pub.DriveClient()
    meta = client.get(rec["audio_drive_id"])
    if not meta:
        raise ArchiveError("L'audio archivé est introuvable dans Drive (supprimé, dans la corbeille, ou autorisation "
                           "insuffisante) : impossible de le restaurer.")
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_to(meta["id"], tmp)
        md5, _ = file_md5(tmp)
        if meta.get("md5Checksum") and md5 != meta["md5Checksum"]:
            raise ArchiveError("Le téléchargement de l'audio est corrompu : réessayez.")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    db.update_recording(rid, audio_restored_at=db.now_iso())
    db.log(rid, f"Audio rapatrié depuis Drive ({fmt_size(dest.stat().st_size)}).")
    return dest


# --- Planification -----------------------------------------------------------------------------------

state: dict = {"started": False, "running": False, "evening_day": None, "retry_at": 0.0}
_state_lock = threading.Lock()


def archive_hour() -> int:
    return min(max(db.get_int_setting("audio_archive_hour"), 0), 23)


def reset() -> None:
    """Après une (re)connexion à Drive : nouveau passage dès que possible."""
    with _state_lock:
        state.update(started=False, retry_at=0.0)


def _summary(result: dict) -> str:
    parts = []
    if result["archived"]:
        parts.append(f"{result['archived']} enregistrement(s) archivé(s), {fmt_size(result['bytes'])} libérés.")
    if result["errors"]:
        parts.append(f"{len(result['errors'])} en échec, nouvel essai plus tard.")
    return " ".join(parts) or "Rien à archiver."


def _remember(message: str, error: str | None) -> None:
    db.set_setting("audio_archive_last_at", db.now_iso())
    db.set_setting("audio_archive_last_result", message)
    db.set_setting("audio_archive_last_error", error or "")


def _run(guard: Guard) -> None:
    message, error, retry = "", None, False
    try:
        result = sweep(guard)
        message = _summary(result)
        error = "; ".join(result["errors"][:3]) or None
        retry = bool(result["errors"])
    except (PublishSkipped, drive_pub.DriveAuthError) as exc:  # inutile de réessayer tant que Drive n'est pas réglé
        message = error = str(exc)
    except Exception as exc:  # noqa: BLE001 - hors ligne, Drive en panne…
        log.exception("Archivage de l'audio en échec")
        message = error = f"Archivage impossible pour l'instant : {exc}"
        retry = True
    finally:
        with _state_lock:
            state["running"] = False
            if retry:
                state["retry_at"] = time.time() + RETRY_AFTER_ERROR_S
    _remember(message, error)


def _start(guard: Guard) -> None:
    threading.Thread(target=_run, args=(guard,), name="archive-audio", daemon=True).start()


def sweep_if_due(guard: Guard, now: datetime | None = None) -> bool:
    """Appelée régulièrement par la file de traitement : un passage au lancement de l'app, un chaque soir (à partir de
    l'heure réglée) et un nouvel essai une heure après un échec. Le passage tourne en tâche de fond."""
    if (not db.get_bool_setting("audio_archive") or not drive_pub.is_configured()
            or not config.TOKEN_FILE.exists()):
        return False
    now = now or datetime.now().astimezone()
    evening = now.hour >= archive_hour()
    with _state_lock:
        if state["running"]:
            return False
        due_evening = evening and state["evening_day"] != now.date()
        due_retry = bool(state["retry_at"]) and time.time() >= state["retry_at"]
        if state["started"] and not due_evening and not due_retry:
            return False
        state.update(started=True, running=True, retry_at=0.0)
        if evening:
            state["evening_day"] = now.date()
    _start(guard)
    return True


def start_now(guard: Guard) -> bool:
    """« Archiver maintenant » (Paramètres) : False si un passage est déjà en cours."""
    with _state_lock:
        if state["running"]:
            return False
        state.update(started=True, running=True, retry_at=0.0)
    _start(guard)
    return True


def status() -> dict:
    """État affiché dans Paramètres."""
    recs = db.q("SELECT id, status, audio_drive_id, audio_archived_bytes FROM recordings")
    archived = [r for r in recs if r["audio_drive_id"] and not recorder.audio_path(r["id"]).exists()]
    waiting = [r for r in recs if r["status"] != "done" and recorder.audio_path(r["id"]).exists()]
    with _state_lock:
        running = state["running"]
    return {
        "enabled": db.get_bool_setting("audio_archive"),
        "folder": db.get_setting("audio_archive_folder"),
        "hour": archive_hour(),
        "pending": len(eligible()),
        "waiting": len(waiting),
        "archived": len(archived),
        "bytes": sum(int(r["audio_archived_bytes"] or 0) for r in archived),
        "running": running,
        "last_at": db.get_setting("audio_archive_last_at"),
        "last_result": db.get_setting("audio_archive_last_result"),
        "last_error": db.get_setting("audio_archive_last_error"),
        "folder_url": db.get_setting("audio_archive_folder_url"),
    }
