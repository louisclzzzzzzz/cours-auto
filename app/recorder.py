"""Réception des morceaux audio envoyés par le navigateur (ou d'un fichier audio importé) et
finalisation avec ffmpeg.

Un enregistrement est composé de *segments* : chaque segment correspond à une instance de
MediaRecorder côté navigateur (un rechargement de page ou une reprise après interruption en crée un
nouveau). Les morceaux d'un même segment se concatènent octet par octet (seul le premier contient
l'en-tête du conteneur) ; les segments sont ensuite décodés puis assemblés par ffmpeg.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from . import config, db

log = logging.getLogger(__name__)

EXT_BY_MIME = {
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "video/mp4": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}

AUDIO_FILENAME = "audio.mp3"
STALE_AFTER_SECONDS = 180  # sans morceau ni signal de vie → « interrompu »


class RecorderError(RuntimeError):
    pass


def ext_for_mime(mime: str | None) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    return EXT_BY_MIME.get(base, "webm")


def recording_dir(recording_id: int) -> Path:
    return config.RECORDINGS_DIR / str(recording_id)


def audio_path(recording_id: int) -> Path:
    return recording_dir(recording_id) / AUDIO_FILENAME


def write_meta(recording_id: int) -> None:
    """Copie des métadonnées à côté des fichiers (utile si la base est perdue)."""
    rec = db.get_recording(recording_id)
    if not rec:
        return
    keep = (
        "id", "subject_name", "course_type", "session_number", "session_date", "title", "teacher", "origin", "source_filename",
        "event_summary", "event_start", "event_end", "location", "started_at", "ended_at", "duration_seconds",
    )
    path = recording_dir(recording_id) / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: rec.get(k) for k in keep}, ensure_ascii=False, indent=2), encoding="utf-8")


def create_recording(
    *,
    subject_id: int,
    course_type: str,
    session_date: str,
    teacher: str = "",
    mime_type: str = "",
    event: dict | None = None,
    origin: str = "browser",
    source_filename: str | None = None,
) -> int:
    """`origin` : « browser » (enregistré dans l'app, morceau par morceau) ou « import » (fichier audio)."""
    course_type = course_type if course_type in config.COURSE_TYPES else "CM"
    event = event or {}
    imported = origin == "import"
    rid = db.create_recording(
        subject_id=subject_id,
        course_type=course_type,
        session_number=db.next_session_number(subject_id, course_type),
        session_date=session_date,
        teacher=teacher.strip(),
        mime_type=mime_type,
        origin=origin,
        source_filename=source_filename,
        event_uid=event.get("uid"),
        event_summary=event.get("summary"),
        event_start=event.get("start"),
        event_end=event.get("end"),
        location=event.get("location"),
        status="finalizing" if imported else "recording",
        client_state="imported" if imported else "recording",
        started_at=db.now_iso(),
        last_seen_at=db.now_iso(),
    )
    recording_dir(rid).mkdir(parents=True, exist_ok=True)
    _learn_teacher(subject_id, teacher)
    write_meta(rid)
    db.log(rid, f"Fichier audio importé : {source_filename}." if imported else "Enregistrement créé.")
    return rid


def _learn_teacher(subject_id: int, teacher: str) -> None:
    subject = db.get_subject(subject_id)
    if not subject or not teacher.strip():
        return
    known = [t.strip() for t in subject["teachers"].split(",") if t.strip()]
    for name in (t.strip() for t in teacher.split(",")):
        if name and name.casefold() not in {k.casefold() for k in known}:
            known.append(name)
    db.update_subject(subject_id, teachers=", ".join(known))


def start_segment(recording_id: int, mime_type: str = "") -> dict:
    rec = db.get_recording(recording_id)
    if not rec:
        raise RecorderError("Enregistrement introuvable.")
    if rec["status"] not in ("recording", "interrupted"):
        raise RecorderError("Cet enregistrement est déjà arrêté.")
    segment = int(rec["segment_count"] or 0) + 1
    db.update_recording(
        recording_id,
        segment_count=segment,
        status="recording",
        client_state="recording",
        last_seen_at=db.now_iso(),
        mime_type=mime_type or rec["mime_type"],
    )
    db.log(recording_id, f"Début du segment {segment}.")
    return {
        "segment": segment,
        "next_seq": int(db.chunk_stats(recording_id)["max_seq"]) + 1,
        "elapsed": float(rec["elapsed_seconds"] or 0),
    }


def save_chunk(recording_id: int, seq: int, segment: int, data: bytes, ext: str, elapsed: float | None) -> None:
    rec = db.get_recording(recording_id)
    if not rec:
        raise RecorderError("Enregistrement introuvable.")
    if rec["status"] not in ("recording", "interrupted"):
        raise RecorderError("Cet enregistrement n'accepte plus de morceaux (déjà arrêté).")
    if seq < 1 or segment < 1:
        raise RecorderError("Numéro de morceau invalide.")
    folder = recording_dir(recording_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"chunk_{seq:04d}.{ext}"
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    db.upsert_chunk(recording_id, seq, segment, ext, len(data))
    fields: dict = {"last_seen_at": db.now_iso(), "status": "recording"}
    if elapsed is not None and elapsed > float(rec["elapsed_seconds"] or 0):
        fields["elapsed_seconds"] = elapsed
    db.update_recording(recording_id, **fields)


def heartbeat(recording_id: int, client_state: str, elapsed: float | None) -> dict | None:
    rec = db.get_recording(recording_id)
    if not rec or rec["status"] not in ("recording", "interrupted"):
        return rec
    fields: dict = {"last_seen_at": db.now_iso(), "client_state": client_state, "status": "recording"}
    if elapsed is not None and elapsed > float(rec["elapsed_seconds"] or 0):
        fields["elapsed_seconds"] = elapsed
    db.update_recording(recording_id, **fields)
    return db.get_recording(recording_id)


def mark_stale_interrupted(max_idle_seconds: int = STALE_AFTER_SECONDS) -> int:
    """Passe en « interrompu » les enregistrements sans signe de vie (onglet fermé, veille…)."""
    count = 0
    limit = datetime.now().astimezone() - timedelta(seconds=max_idle_seconds)
    for rec in db.list_recordings_by_status("recording"):
        try:
            last = datetime.fromisoformat(rec["last_seen_at"] or rec["started_at"])
        except ValueError:
            continue
        if last < limit:
            db.update_recording(rec["id"], status="interrupted")
            db.log(rec["id"], "Aucune donnée reçue depuis plusieurs minutes : enregistrement marqué interrompu.", "warning")
            count += 1
    return count


# --- Finalisation ffmpeg ------------------------------------------------------------------

def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RecorderError(f"{name} introuvable : installez ffmpeg (ex. `brew install ffmpeg`).")
    return path


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float | None:
    res = _run([
        _tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=120)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


# --- Import d'un fichier audio ----------------------------------------------------------------------

def import_extension(filename: str) -> str:
    ext = re.sub(r"[^a-z0-9]", "", Path(filename or "").suffix.lower())[:5]
    return ext or "bin"


def source_path(recording_id: int) -> Path | None:
    """Fichier audio importé, conservé tel quel (`source.<ext>`)."""
    matches = sorted(recording_dir(recording_id).glob("source.*"))
    return matches[0] if matches else None


def save_import(recording_id: int, fileobj: BinaryIO, ext: str) -> Path:
    folder = recording_dir(recording_id)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"source.{ext}"
    tmp = folder / f"source.{ext}.part"
    with tmp.open("wb") as out:
        shutil.copyfileobj(fileobj, out, length=1024 * 1024)
    tmp.replace(dest)
    return dest


def probe_audio(path: Path) -> float | None:
    """Durée (s) si le fichier contient une piste audio (0.0 si durée inconnue), sinon None."""
    res = _run([
        _tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_type:format=duration", "-of", "json", str(path),
    ], timeout=120)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        return None
    if not data.get("streams"):
        return None
    try:
        return float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _convert_source(recording_id: int, src: Path) -> float:
    tmp = recording_dir(recording_id) / "_audio_tmp.mp3"
    bitrate = db.get_setting("audio_bitrate") or "48k"
    res = _run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", bitrate, str(tmp),
    ])
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        raise RecorderError(f"Conversion du fichier importé impossible : {res.stderr.strip()[-500:]}")
    tmp.replace(audio_path(recording_id))
    return probe_duration(audio_path(recording_id)) or 0.0


def finalize_audio(recording_id: int) -> float:
    """Produit l'audio final (MP3 mono 16 kHz) : assemblage des morceaux reçus du navigateur,
    ou conversion du fichier importé. Renvoie la durée en secondes."""
    chunks = db.list_chunks(recording_id)
    if not chunks:
        src = source_path(recording_id)
        if src is not None:
            return _convert_source(recording_id, src)
        raise RecorderError("Aucun morceau audio reçu pour cet enregistrement.")
    folder = recording_dir(recording_id)
    work = folder / "_finalize"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    ffmpeg = _tool("ffmpeg")

    segments: dict[int, list[dict]] = {}
    for c in chunks:
        segments.setdefault(c["segment"], []).append(c)

    wavs: list[Path] = []
    for seg, seg_chunks in sorted(segments.items()):
        ext = seg_chunks[0]["ext"]
        joined = work / f"segment_{seg:02d}.{ext}"
        with joined.open("wb") as out:
            for c in sorted(seg_chunks, key=lambda c: c["seq"]):
                part = folder / f"chunk_{c['seq']:04d}.{c['ext']}"
                if part.exists():
                    out.write(part.read_bytes())
                else:
                    db.log(recording_id, f"Morceau manquant : {part.name}", "warning")
        wav = work / f"segment_{seg:02d}.wav"
        res = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+discardcorrupt",
            "-i", str(joined), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ])
        # Un dernier morceau tronqué (onglet fermé) fait échouer ffmpeg en fin de fichier : on garde
        # ce qui a pu être décodé.
        if wav.exists() and wav.stat().st_size > 1000:
            if res.returncode != 0:
                db.log(recording_id, f"Segment {seg} partiellement décodé : {res.stderr.strip()[-300:]}", "warning")
            wavs.append(wav)
        else:
            db.log(recording_id, f"Segment {seg} illisible, ignoré : {res.stderr.strip()[-300:]}", "error")

    if not wavs:
        raise RecorderError("Aucun segment audio n'a pu être décodé.")

    listing = work / "segments.txt"
    listing.write_text(
        "".join("file '{}'\n".format(str(w).replace("'", "'\\''")) for w in wavs), encoding="utf-8"
    )
    target = audio_path(recording_id)
    tmp_target = work / AUDIO_FILENAME
    bitrate = db.get_setting("audio_bitrate") or "48k"
    res = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", bitrate, str(tmp_target),
    ])
    if res.returncode != 0 or not tmp_target.exists():
        raise RecorderError(f"ffmpeg n'a pas pu produire l'audio final : {res.stderr.strip()[-500:]}")
    tmp_target.replace(target)
    shutil.rmtree(work, ignore_errors=True)
    duration = probe_duration(target) or 0.0
    return duration


def reencode_lower_bitrate(recording_id: int, bitrate: str = "24k") -> Path:
    """Version plus légère de l'audio (si l'API refuse un fichier trop volumineux)."""
    src = audio_path(recording_id)
    dst = recording_dir(recording_id) / f"audio_{bitrate}.mp3"
    res = _run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", bitrate, str(dst),
    ])
    if res.returncode != 0:
        raise RecorderError(f"Ré-encodage impossible : {res.stderr.strip()[-300:]}")
    return dst


def delete_files(recording_id: int) -> None:
    shutil.rmtree(recording_dir(recording_id), ignore_errors=True)
