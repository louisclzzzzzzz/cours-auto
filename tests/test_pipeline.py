"""Orchestration : enchaînement des étapes (audio → transcription → dépôt Drive), statuts, reprise au démarrage."""

import json

import pytest

from app import db, pipeline as pl, recorder, subjects, transcribe
from app.publish import PublishSkipped


@pytest.fixture
def fakes(monkeypatch):
    calls = {"drive": 0}

    monkeypatch.setattr(recorder, "finalize_audio", lambda rid: (recorder.audio_path(rid).write_bytes(b"mp3"), 3600.0)[1])
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, vocab, rid=None: {
        "text": "t", "segments": [{"text": "Bonjour, aujourd'hui les graphes.", "start": 0, "end": 3, "speaker_id": "speaker_1"}]})

    def drive(rid):
        calls["drive"] += 1

    monkeypatch.setattr(pl.drive_pub, "publish_recording", drive)
    return calls


def new_rec(sid, day="2026-09-21"):
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date=day)
    db.upsert_chunk(rid, 1, 1, "webm", 10)
    db.update_recording(rid, status="finalizing")
    return rid


def finalize_then_launch(p: pl.Pipeline, rid: int) -> None:
    """Arrêt : seul l'audio est préparé ; le traitement est ensuite lancé à la main (« Lancer le traitement »)."""
    p._run(pl.Job("recording", rid, "finalize"))
    assert db.get_recording(rid)["status"] == "uploaded"
    p._run(pl.Job("recording", rid, "transcribe"))


def test_full_chain(fakes):
    sid = subjects.create_subject("Graphes")
    p = pl.Pipeline()
    r1 = new_rec(sid)
    p._run(pl.Job("recording", r1, "finalize"))
    rec = db.get_recording(r1)
    # La finalisation n'enchaîne pas : rien n'est envoyé à Mistral avant que l'utilisateur lance le traitement.
    assert rec["status"] == "uploaded" and rec["duration_seconds"] == 3600.0
    assert not (recorder.recording_dir(r1) / "transcript.json").exists() and fakes["drive"] == 0
    p._run(pl.Job("recording", r1, "transcribe"))
    rec = db.get_recording(r1)
    assert rec["status"] == "done" and rec["drive_status"] == "done", rec["error_message"]
    folder = recorder.recording_dir(r1)
    assert json.loads((folder / "transcript.json").read_text())["segments"]
    text = (folder / "transcript.txt").read_text()
    assert text.startswith("# Transcription — Graphes — CM 1 — 2026-09-21") and "L1 :" in text
    # Plus de mise en forme locale : le cours sera rédigé par la tâche Claude, puis récupéré depuis Drive.
    assert not subjects.course_path(r1).exists() and fakes["drive"] == 1


def test_drive_not_configured_ends_after_transcription(fakes, monkeypatch):
    sid = subjects.create_subject("Réseaux")
    p = pl.Pipeline()
    rid = new_rec(sid)

    def drive_skip(r):
        raise PublishSkipped("Drive non configuré")

    monkeypatch.setattr(pl.drive_pub, "publish_recording", drive_skip)
    finalize_then_launch(p, rid)
    rec = db.get_recording(rid)
    assert rec["status"] == "done" and rec["drive_status"] == "skipped" and rec["drive_error"] == "Drive non configuré"


def test_drive_failure_then_redeposit(fakes, monkeypatch):
    sid = subjects.create_subject("Réseaux")
    p = pl.Pipeline()
    rid = new_rec(sid)

    def drive_fail(r):
        raise RuntimeError("Drive indisponible")

    monkeypatch.setattr(pl.drive_pub, "publish_recording", drive_fail)
    finalize_then_launch(p, rid)
    rec = db.get_recording(rid)
    assert rec["status"] == "error" and rec["error_step"] == "publication"
    assert rec["drive_status"] == "error" and "indisponible" in rec["drive_error"]
    # La transcription est conservée : on relance seulement le dépôt.
    monkeypatch.setattr(pl.drive_pub, "publish_recording", lambda r: None)
    p._run(pl.Job("recording", rid, "publish", chain=False))
    rec = db.get_recording(rid)
    assert rec["status"] == "done" and rec["drive_status"] == "done" and rec["error_step"] is None


def test_error_step_and_restart(fakes, monkeypatch):
    sid = subjects.create_subject("Probas")
    p = pl.Pipeline()
    rid = new_rec(sid)

    def boom(*a, **k):
        raise RuntimeError("429 trop de requêtes")

    monkeypatch.setattr(transcribe, "transcribe_file", boom)
    finalize_then_launch(p, rid)
    rec = db.get_recording(rid)
    assert rec["status"] == "error" and rec["error_step"] == "transcription"
    assert recorder.audio_path(rid).exists()  # l'audio est conservé
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, vocab, rid=None: {"text": "x", "segments": [
        {"text": "Bonjour.", "start": 0, "end": 1, "speaker_id": "speaker_1"}]})
    p._run(pl.Job("recording", rid, "transcribe"))
    assert db.get_recording(rid)["status"] == "done"


def test_recover_after_restart(fakes):
    sid = subjects.create_subject("Algo")
    rid = recorder.create_recording(subject_id=sid, course_type="TD", session_date="2026-09-21")
    other = new_rec(sid)
    db.update_recording(other, status="transcribing")
    waiting = new_rec(sid)
    db.update_recording(waiting, status="uploaded")
    p = pl.Pipeline()
    p.recover()
    assert db.get_recording(rid)["status"] == "interrupted"
    assert ("recording", other, "transcribe") in p._pending
    # Audio prêt mais traitement jamais lancé : il attend toujours l'utilisateur.
    assert not p.is_active(waiting) and db.get_recording(waiting)["status"] == "uploaded"


def test_recover_moves_legacy_steps_to_drive_deposit(fakes):
    """Séances laissées par l'ancienne version (mise en forme Mistral, Notion) : reprise au dépôt Drive."""
    sid = subjects.create_subject("Algo")
    formatted = new_rec(sid)
    db.update_recording(formatted, status="formatted")
    format_error = new_rec(sid)
    db.update_recording(format_error, status="error", error_step="mise en forme", error_message="LLM")
    for rid in (formatted, format_error):
        (recorder.recording_dir(rid) / "transcript.txt").write_text("# Transcription\n", encoding="utf-8")
    already_in_drive = new_rec(sid)  # transcription locale perdue, mais déjà déposée dans Drive
    db.update_recording(already_in_drive, status="error", error_step="mise en forme", drive_transcription_id="t1")
    no_transcript = new_rec(sid)
    db.update_recording(no_transcript, status="error", error_step="mise en forme")
    notion_only = new_rec(sid)
    db.update_recording(notion_only, status="error", error_step="publication", drive_status="done",
                        error_message="Notion : indisponible")
    drive_error = new_rec(sid)
    db.update_recording(drive_error, status="error", error_step="publication Drive", drive_status="error")
    p = pl.Pipeline()
    p.recover()
    for rid in (formatted, format_error):
        assert db.get_recording(rid)["status"] == "transcribed" and ("recording", rid, "publish") in p._pending
    assert db.get_recording(already_in_drive)["status"] == "done" and db.get_recording(already_in_drive)["drive_status"] == "done"
    assert db.get_recording(no_transcript)["status"] == "uploaded"  # traitement à relancer à la main
    assert not p.is_active(already_in_drive) and not p.is_active(no_transcript)
    assert db.get_recording(notion_only)["status"] == "done" and db.get_recording(notion_only)["error_step"] is None
    rec = db.get_recording(drive_error)
    assert rec["status"] == "error" and rec["error_step"] == "publication"  # « Réessayer » relance le dépôt
