"""Orchestration : enchaînement des étapes, statuts, sous-statuts de publication, état de matière."""

import json

import pytest

from app import db, llm, pipeline as pl, recorder, subjects, transcribe
from app.publish import PublishSkipped


@pytest.fixture
def fakes(monkeypatch):
    calls = {"drive": 0, "notion": 0, "state_inputs": []}

    monkeypatch.setattr(recorder, "finalize_audio", lambda rid: (recorder.audio_path(rid).write_bytes(b"mp3"), 3600.0)[1])
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, vocab, rid=None: {
        "text": "t", "segments": [{"text": "Bonjour, aujourd'hui les graphes.", "start": 0, "end": 3, "speaker_id": "speaker_1"}]})

    def fake_format(data, state, meta, on_progress=None, support_docs=None):
        return llm.finalize_course(f"# Séance {meta['numero']}\n\n## Contenu\n\nTexte.", meta)

    def fake_state(old, course, meta):
        calls["state_inputs"].append(old)
        return old.rstrip() + f"\n- {meta['type']} {meta['numero']}\n"

    monkeypatch.setattr(llm, "format_course", fake_format)
    monkeypatch.setattr(llm, "update_state", fake_state)
    monkeypatch.setattr(llm, "suggest_terms", lambda course, vocab, exclude=(): ["Dijkstra"])

    def drive(rid):
        calls["drive"] += 1

    def notion(rid):
        calls["notion"] += 1

    monkeypatch.setattr(pl.drive_pub, "publish_recording", drive)
    monkeypatch.setattr(pl.notion_pub, "publish_recording", notion)
    return calls


def new_rec(sid, day="2026-09-21"):
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date=day)
    db.upsert_chunk(rid, 1, 1, "webm", 10)
    db.update_recording(rid, status="finalizing")
    return rid


def test_full_chain_and_state(fakes):
    sid = subjects.create_subject("Graphes")
    p = pl.Pipeline()
    r1 = new_rec(sid)
    p._run(pl.Job("recording", r1, "finalize"))
    rec = db.get_recording(r1)
    assert rec["status"] == "done", rec["error_message"]
    assert rec["title"] == "Séance 1" and rec["duration_seconds"] == 3600.0
    folder = recorder.recording_dir(r1)
    assert json.loads((folder / "transcript.json").read_text())["segments"]
    assert "L1 :" in (folder / "transcript.txt").read_text()
    assert subjects.course_path(r1).read_text().startswith("# CM 1 – Séance 1")
    state = subjects.read_state(db.get_subject(sid))
    assert "- CM 1" in state and db.get_subject(sid)["state_recording_id"] == r1
    assert subjects.get_proposed_terms(db.get_subject(sid)) == ["Dijkstra"]
    assert fakes["drive"] == 1 and fakes["notion"] == 1

    # Séance suivante : l'état de départ est celui produit par la séance 1.
    r2 = new_rec(sid, "2026-09-28")
    p._run(pl.Job("recording", r2, "finalize"))
    assert "- CM 1" in fakes["state_inputs"][-1]
    assert "- CM 2" in subjects.read_state(db.get_subject(sid))

    # Relancer la mise en forme de la séance 1 : état d'entrée identique (state_before figé),
    # et l'état de la matière n'est pas écrasé car la séance 2, plus récente, l'a déjà mis à jour.
    p._run(pl.Job("recording", r1, "format"))
    assert fakes["state_inputs"][-1] == fakes["state_inputs"][0]
    assert "- CM 2" in subjects.read_state(db.get_subject(sid))
    assert "plus récente" in db.get_recording(r1)["state_note"]
    assert list((recorder.recording_dir(r1) / "versions").glob("course_*.md"))


def test_publication_substatuses(fakes, monkeypatch):
    sid = subjects.create_subject("Réseaux")
    p = pl.Pipeline()
    rid = new_rec(sid)

    def drive_skip(r):
        raise PublishSkipped("Drive non configuré")

    def notion_fail(r):
        raise RuntimeError("Notion indisponible")

    monkeypatch.setattr(pl.drive_pub, "publish_recording", drive_skip)
    monkeypatch.setattr(pl.notion_pub, "publish_recording", notion_fail)
    p._run(pl.Job("recording", rid, "finalize"))
    rec = db.get_recording(rid)
    assert rec["status"] == "error" and rec["error_step"] == "publication"
    assert rec["drive_status"] == "skipped" and rec["notion_status"] == "error"
    assert "Notion indisponible" in rec["notion_error"]
    # L'échec de Notion n'a pas empêché le cours d'exister ; on relance Notion seul.
    monkeypatch.setattr(pl.notion_pub, "publish_recording", lambda r: None)
    p._run(pl.Job("recording", rid, "publish_notion", chain=False))
    rec = db.get_recording(rid)
    assert rec["status"] == "done" and rec["notion_status"] == "done" and rec["drive_status"] == "skipped"


def test_error_step_and_restart(fakes, monkeypatch):
    sid = subjects.create_subject("Probas")
    p = pl.Pipeline()
    rid = new_rec(sid)

    def boom(*a, **k):
        raise RuntimeError("429 trop de requêtes")

    monkeypatch.setattr(transcribe, "transcribe_file", boom)
    p._run(pl.Job("recording", rid, "finalize"))
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
    p = pl.Pipeline()
    p.recover()
    assert db.get_recording(rid)["status"] == "interrupted"
    assert ("recording", other, "transcribe") in p._pending


def test_format_sets_notion_pending_action_via_route(fakes):
    from fastapi.testclient import TestClient

    from app.main import app

    sid = subjects.create_subject("Compil")
    rid = new_rec(sid)
    db.update_recording(rid, status="done", notion_page_id="p1")
    with TestClient(app) as client:
        pl.pipeline.stop()  # on ne veut pas que le thread traite la tâche pendant le test
        r = client.post(f"/enregistrements/{rid}/action", data={"action": "format", "notion_mode": "overwrite"},
                        follow_redirects=False)
        assert r.status_code == 303
    assert db.get_recording(rid)["notion_pending_action"] == "overwrite"
