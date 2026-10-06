"""Supports de cours (diapositives, PDF, documents) joints à une séance.

Le fichier d'origine est conservé dans `recordings/<id>/supports/` puis déposé dans le dossier `Supports/` de
la matière sur Drive, où la tâche Claude qui rédige le cours le lit avec la transcription.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import BinaryIO

from . import db, recorder
from .textutils import slugify

FORMATS = {".pdf": "PDF", ".pptx": "PowerPoint", ".docx": "Word"}
MIMETYPES = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
ACCEPT = ".pdf,.pptx,.docx,application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation," \
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAGIC = {".pdf": b"%PDF", ".pptx": b"PK\x03\x04", ".docx": b"PK\x03\x04"}
MAX_BYTES = 50 * 1024 * 1024


class SupportError(ValueError):
    pass


# --- Fichiers -------------------------------------------------------------------------------------

def supports_dir(recording_id: int) -> Path:
    return recorder.recording_dir(recording_id) / "supports"


def file_path(sup: dict) -> Path:
    return supports_dir(sup["recording_id"]) / sup["stored_name"]


def check_filename(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in FORMATS:
        raise SupportError(f"« {filename} » : format non pris en charge (PDF, PPTX ou DOCX). "
                           "Pour Keynote, Google Slides ou un ancien .ppt, exportez d'abord en PDF.")
    return ext


def add(recording_id: int, filename: str, stream: BinaryIO) -> dict:
    """Enregistre le fichier (lu par morceaux, 50 Mo au plus)."""
    ext = check_filename(filename)
    folder = supports_dir(recording_id)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".envoi-{uuid.uuid4().hex}{ext}"
    size = 0
    head = b""
    try:
        with tmp.open("wb") as out:
            while chunk := stream.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SupportError(f"« {filename} » dépasse 50 Mo.")
                if len(head) < 1024:
                    head += chunk[: 1024 - len(head)]
                out.write(chunk)
        if size == 0:
            raise SupportError(f"« {filename} » est vide.")
        if MAGIC[ext] not in head:
            raise SupportError(f"« {filename} » n'est pas un fichier {FORMATS[ext]} valide.")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sid = db.insert_support(recording_id=recording_id, filename=Path(filename).name, stored_name="", size=size)
    stored = f"{sid:03d}_{slugify(Path(filename).stem, 50)}{ext}"
    tmp.replace(folder / stored)
    db.update_support(sid, stored_name=stored)
    db.log(recording_id, f"Support de cours ajouté : « {Path(filename).name} ».")
    return db.get_support(sid)


def add_many(recording_id: int, files: list[tuple[str, BinaryIO]]) -> tuple[list[dict], list[str]]:
    """Ajoute plusieurs fichiers ; renvoie (ajoutés, erreurs)."""
    added: list[dict] = []
    errors: list[str] = []
    for name, stream in files:
        if not name:
            continue
        try:
            sup = add(recording_id, name, stream)
        except SupportError as exc:
            errors.append(str(exc))
            continue
        added.append(sup)
    return added, errors


def remove(support_id: int) -> None:
    sup = db.get_support(support_id)
    if not sup:
        return
    if sup["stored_name"]:
        file_path(sup).unlink(missing_ok=True)
    db.delete_support(support_id)
    db.log(sup["recording_id"], f"Support de cours retiré : « {sup['filename']} ».")
