"""Transcription avec Mistral Voxtral (voxtral-mini-latest) : diarisation, horodatage par segment,
context biasing avec le vocabulaire de la matière."""

from __future__ import annotations

import io
import json
import logging
import re
import wave
from collections import defaultdict
from pathlib import Path

from mistralai.client.errors import MistralError

from . import db, recorder
from .llm import get_client
from .retry import with_retries
from .textutils import fmt_ts

log = logging.getLogger(__name__)

MAX_BIAS_TERMS = 100
TRANSCRIBE_TIMEOUT_MS = 45 * 60 * 1000


def normalize_bias_terms(terms: list[str]) -> list[str]:
    """L'API refuse les espaces et virgules dans un terme : « graphe pondéré » → « graphe_pondéré »."""
    out: list[str] = []
    for term in terms:
        t = re.sub(r"[\s,]+", "_", (term or "").strip()).strip("_")
        if t and t.casefold() not in {o.casefold() for o in out}:
            out.append(t)
    return out[:MAX_BIAS_TERMS]


def _originals(terms: list[str]) -> list[str]:
    """Termes d'origine dans le même ordre que `normalize_bias_terms` (doublons retirés)."""
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        norm = re.sub(r"[\s,]+", "_", (term or "").strip()).strip("_")
        if norm and norm.casefold() not in seen:
            seen.add(norm.casefold())
            out.append(" ".join((term or "").replace(",", " ").split()))
    return out[:MAX_BIAS_TERMS]


def restore_terms(text: str, mapping: dict | None) -> str:
    for norm, orig in (mapping or {}).items():
        text = re.sub(
            re.escape(norm),
            lambda m, o=orig: o[:1].upper() + o[1:] if m.group(0)[:1].isupper() else o,
            text,
            flags=re.IGNORECASE,
        )
    return text


def _call(path: Path, **kwargs):
    client = get_client()

    def once():
        with path.open("rb") as f:
            return client.audio.transcriptions.complete(
                file={"content": f, "file_name": path.name},
                timeout_ms=TRANSCRIBE_TIMEOUT_MS,
                **kwargs,
            )

    # Peu d'essais rapprochés (chacun renvoie tout l'audio) : si Mistral reste indisponible, la pipeline
    # reprogramme l'étape plus tard.
    return with_retries(once, attempts=3, label="Transcription Voxtral")


def probe() -> None:
    """Vérifie que le service de transcription répond, avec une seconde de silence (coût négligeable) :
    pendant une panne, on évite de renvoyer tout l'audio du cours à chaque essai. Lève l'erreur de l'API."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    get_client().audio.transcriptions.complete(
        model=db.get_setting("transcription_model") or "voxtral-mini-latest",
        file={"content": buf.getvalue(), "file_name": "sonde.wav"},
        timeout_ms=120_000,
    )


def transcribe_file(path: Path, vocabulary: list[str], recording_id: int | None = None) -> dict:
    model = db.get_setting("transcription_model") or "voxtral-mini-latest"
    language = (db.get_setting("transcription_language") or "").strip()
    kwargs: dict = {"model": model, "diarize": True, "timestamp_granularities": ["segment"]}
    bias = normalize_bias_terms(vocabulary)
    if bias:
        kwargs["context_bias"] = bias
    if language:
        kwargs["language"] = language

    # Voxtral recopie parfois le terme tel qu'envoyé (« graphe_pondéré ») : on garde la correspondance
    # pour restaurer l'orthographe d'origine dans le texte lisible (le JSON brut reste intact).
    restore = {norm: orig for norm, orig in zip(bias, _originals(vocabulary)) if norm != orig}
    for _ in range(4):
        try:
            resp = _call(path, **kwargs)
            data = resp.model_dump(mode="json")
            if restore:
                data["_context_bias_restore"] = restore
            return data
        except MistralError as exc:
            body = (exc.body or "").lower()
            if exc.status_code == 400 and "language" in kwargs and "language" in body:
                # La doc indique que `language` et `timestamp_granularities` peuvent être incompatibles.
                db.log(recording_id, "Paramètre de langue refusé avec l'horodatage : nouvel essai sans `language`.", "warning")
                kwargs.pop("language")
                continue
            if exc.status_code == 400 and "context_bias" in kwargs and "context" in body:
                db.log(recording_id, f"Vocabulaire refusé par l'API ({exc.body[:200]}) : nouvel essai sans context biasing.", "warning")
                kwargs.pop("context_bias")
                continue
            if exc.status_code == 413 and recording_id is not None and "24k" not in path.name:
                db.log(recording_id, "Fichier audio trop volumineux pour l'API : ré-encodage à 24 kb/s.", "warning")
                path = recorder.reencode_lower_bitrate(recording_id, "24k")
                continue
            raise
    raise RuntimeError("La transcription a échoué après plusieurs ajustements de paramètres.")


# --- Mise en forme lisible -------------------------------------------------------------------

def build_paragraphs(data: dict, max_len_s: float = 60.0, pause_s: float = 2.5) -> list[dict]:
    """Regroupe les segments : nouveau paragraphe à chaque changement de locuteur, pause ou ~1 min."""
    segments = data.get("segments") or []
    mapping = data.get("_context_bias_restore")
    if not segments:
        text = restore_terms((data.get("text") or "").strip(), mapping)
        return [{"start": 0.0, "end": 0.0, "speaker": None, "text": text, "gap_before": 0.0}] if text else []
    paragraphs: list[dict] = []
    cur: dict | None = None
    for s in segments:
        text = restore_terms((s.get("text") or "").strip(), mapping)
        if not text:
            continue
        speaker = s.get("speaker_id")
        start = float(s.get("start") or 0.0)
        end = float(s.get("end") or start)
        gap = start - cur["end"] if cur else 0.0
        if cur is None or speaker != cur["speaker"] or gap > pause_s or end - cur["start"] > max_len_s:
            cur = {"start": start, "end": end, "speaker": speaker, "text": text, "gap_before": max(gap, 0.0)}
            paragraphs.append(cur)
        else:
            cur["text"] += " " + text
            cur["end"] = end
    return paragraphs


def speaker_labels(paragraphs: list[dict]) -> tuple[dict, dict]:
    """L1 = locuteur qui parle le plus (probablement l'enseignant), puis L2, L3…"""
    talk: dict = defaultdict(float)
    for p in paragraphs:
        if p["speaker"] is not None:
            talk[p["speaker"]] += max(p["end"] - p["start"], 0.0)
    ordered = sorted(talk, key=lambda k: -talk[k])
    total = sum(talk.values()) or 1.0
    labels = {spk: f"L{i}" for i, spk in enumerate(ordered, 1)}
    shares = {labels[spk]: round(100 * talk[spk] / total) for spk in ordered}
    return labels, shares


def paragraph_line(p: dict, labels: dict) -> str:
    who = labels.get(p["speaker"]) if p["speaker"] is not None else None
    return f"[{fmt_ts(p['start'])}] {who} : {p['text']}" if who else f"[{fmt_ts(p['start'])}] {p['text']}"


def speakers_header(shares: dict) -> str:
    if not shares:
        return ""
    if len(shares) == 1:
        return "Locuteurs : L1 = locuteur unique (probablement l'enseignant)."
    first, share = next(iter(shares.items()))
    return (
        f"Locuteurs : {first} = locuteur principal (probablement l'enseignant, {share} % du temps de parole) ; "
        "L2, L3… = autres intervenants (souvent des étudiants)."
    )


def transcript_text(data: dict, title: str = "") -> str:
    """Transcription lisible : un paragraphe horodaté par ligne, séparés par une ligne vide."""
    paragraphs = build_paragraphs(data)
    labels, shares = speaker_labels(paragraphs)
    blocks = []
    if title:
        blocks.append(f"# Transcription — {title}")
    header = speakers_header(shares)
    if header:
        blocks.append(header)
    blocks.extend(paragraph_line(p, labels) for p in paragraphs)
    return "\n\n".join(blocks).strip() + "\n"


def load_transcript(folder: Path) -> dict | None:
    path = folder / "transcript.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
