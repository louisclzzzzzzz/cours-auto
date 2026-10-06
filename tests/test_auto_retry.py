"""Mistral momentanément indisponible (5xx, 429, réseau) : l'étape est reprogrammée et relancée toute seule."""

from datetime import datetime, timedelta

import httpx
import pytest

from app import db, pipeline as pl, recorder, subjects, transcribe


class ApiError(Exception):
    """Erreur de l'API Mistral (même attribut `status_code` que MistralError)."""

    def __init__(self, status: int):
        super().__init__(f"API error occurred: Status {status}. Body: unreachable_backend")
        self.status_code = status


def uploaded_rec() -> int:
    sid = subjects.create_subject("Graphes")
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-29")
    recorder.audio_path(rid).write_bytes(b"mp3")
    db.update_recording(rid, status="uploaded", duration_seconds=3600)
    return rid


def minutes_until(iso: str) -> float:
    return (datetime.fromisoformat(iso) - datetime.now().astimezone()).total_seconds() / 60


@pytest.fixture
def mistral(monkeypatch):
    state = {"down": True, "uploads": 0, "probes": 0}

    def transcribe_file(path, vocab, rid=None):
        state["uploads"] += 1
        if state["down"]:
            raise ApiError(503)
        return {"text": "t", "segments": [{"text": "Bonjour.", "start": 0, "end": 2, "speaker_id": "s1"}]}

    def probe():
        state["probes"] += 1
        if state["down"]:
            raise ApiError(503)

    monkeypatch.setattr(transcribe, "transcribe_file", transcribe_file)
    monkeypatch.setattr(transcribe, "probe", probe)
    return state


def test_unavailable_schedules_an_automatic_retry(mistral):
    rid = uploaded_rec()
    p = pl.Pipeline()
    assert p._exec_step(rid, "transcribe") is False
    rec = db.get_recording(rid)
    assert rec["status"] == "error" and rec["error_step"] == "transcription"
    assert rec["error_message"].startswith(
        "Le service de transcription de Mistral est momentanément indisponible (erreur 503). Nouvel essai automatique vers ")
    assert rec["auto_retry_count"] == 1 and 4.9 < minutes_until(rec["auto_retry_at"]) <= 5
    assert p.submit_due_retries() == []  # pas encore l'heure


def test_due_retry_checks_mistral_before_sending_the_audio_again(mistral):
    rid = uploaded_rec()
    p = pl.Pipeline()
    p._exec_step(rid, "transcribe")
    db.update_recording(rid, auto_retry_at=(datetime.now().astimezone() - timedelta(seconds=1)).isoformat())
    assert p.submit_due_retries() == [rid]
    assert ("recording", rid, "transcribe") in p._pending
    assert db.get_recording(rid)["auto_retry_at"] is None and p.submit_due_retries() == []

    # Toujours en panne : la sonde échoue, l'audio n'est pas renvoyé, essai suivant dans 10 min.
    p._exec_step(rid, "transcribe")
    rec = db.get_recording(rid)
    assert mistral["probes"] == 1 and mistral["uploads"] == 1
    assert rec["auto_retry_count"] == 2 and 9.9 < minutes_until(rec["auto_retry_at"]) <= 10

    # Mistral répond de nouveau : transcription faite, compteur remis à zéro.
    mistral["down"] = False
    assert p._exec_step(rid, "transcribe") is True
    rec = db.get_recording(rid)
    assert rec["status"] == "transcribed" and rec["auto_retry_count"] == 0 and rec["auto_retry_at"] is None
    assert mistral["probes"] == 2 and mistral["uploads"] == 2


def test_delays_and_messages():
    assert [pl.auto_retry_delay(n).seconds // 60 for n in (1, 2, 3, 4, 5, 20)] == [5, 10, 20, 40, 60, 60]
    assert pl.unavailable_message(ApiError(502)) == (
        "Le service de transcription de Mistral est momentanément indisponible (erreur 502).")
    assert pl.unavailable_message(ApiError(429)).startswith("Mistral limite temporairement")
    assert pl.unavailable_message(httpx.ConnectError("x")).startswith("Connexion à Mistral impossible")


def test_other_errors_wait_for_the_user(monkeypatch):
    rid = uploaded_rec()
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, vocab, rid=None: (_ for _ in ()).throw(ApiError(401)))
    pl.Pipeline()._exec_step(rid, "transcribe")
    rec = db.get_recording(rid)
    assert rec["status"] == "error" and "Status 401" in rec["error_message"]
    assert rec["auto_retry_at"] is None and rec["auto_retry_count"] == 0


def test_rate_limit_and_giving_up(monkeypatch):
    rid = uploaded_rec()
    monkeypatch.setattr(transcribe, "transcribe_file", lambda *a, **k: (_ for _ in ()).throw(ApiError(429)))
    p = pl.Pipeline()
    p._exec_step(rid, "transcribe")
    rec = db.get_recording(rid)
    assert rec["error_step"] == "transcription" and rec["auto_retry_count"] == 1
    assert rec["error_message"].startswith("Mistral limite temporairement les requêtes (erreur 429). Nouvel essai")
    db.update_recording(rid, auto_retry_count=pl.AUTO_RETRY_MAX)
    p._exec_step(rid, "transcribe")
    rec = db.get_recording(rid)
    assert rec["auto_retry_at"] is None and "Les essais automatiques sont arrêtés" in rec["error_message"]


def test_page_refreshes_and_manual_retry_starts_over(mistral, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    rid = uploaded_rec()
    pl.Pipeline()._exec_step(rid, "transcribe")
    submitted = []
    with TestClient(app) as client:
        pl.pipeline.stop()
        monkeypatch.setattr(pl.pipeline, "submit", lambda kind, target, action, chain=True: submitted.append(action))
        page = client.get(f"/enregistrements/{rid}").text
        assert "Nouvel essai automatique vers" in page and 'hx-trigger="every 3s"' in page
        assert 'every 5s' in client.get("/enregistrements?vue=liste").text
        r = client.post(f"/enregistrements/{rid}/action", data={"action": "transcribe"}, follow_redirects=False)
        assert r.status_code == 303 and submitted == ["transcribe"]
    rec = db.get_recording(rid)
    assert rec["auto_retry_at"] is None and rec["auto_retry_count"] == 0
