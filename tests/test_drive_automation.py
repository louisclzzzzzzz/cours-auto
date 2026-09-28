"""Drive en mode « automatisation » : l'app dépose transcriptions et supports, une automatisation externe
rédige les séances, le cours complet, `_etat.md` et le Google Doc NotebookLM."""

import json

import pytest

from app import db, llm, pipeline as pl, recorder, subjects, supports, transcribe
from app.publish import PublishSkipped
from app.publish import drive as drive_pub
from fakes import FakeDriveService, make_pdf

EVENT = {"uid": "u1", "summary": "Graphes CM", "start": "2026-09-28T10:15:00+02:00",
         "end": "2026-09-28T12:15:00+02:00", "location": "IS_A213"}


@pytest.fixture
def automation():
    db.set_setting("drive_writer", "automatisation")


def new_session(tmp_path, with_support=True) -> int:
    sid = subjects.create_subject("Graphes", "DURAND Sophie")
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28", event=EVENT)
    folder = recorder.recording_dir(rid)
    (folder / "transcript.txt").write_text("# Transcription — Graphes — CM 1 — 2026-09-28\n\n"
                                           "Locuteurs : L1 = locuteur unique.\n\n[00:00:00] L1 : Bonjour.\n")
    db.update_recording(rid, status="transcribed", duration_seconds=5400)
    if with_support:
        make_pdf(tmp_path / "CM1 diapos.pdf", [["Dijkstra"]])
        with (tmp_path / "CM1 diapos.pdf").open("rb") as f:
            supports.add(rid, "CM1 diapos.pdf", f)
    return rid


def test_deposit_transcription_and_supports(tmp_path, automation):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    rid = new_session(tmp_path)
    drive_pub.deposit_inputs(rid, client)
    folder = svc.by_name("Graphes")
    transcriptions, support_dir = svc.by_name("Transcriptions"), svc.by_name("Supports")
    assert transcriptions["parents"] == [folder["id"]] and support_dir["parents"] == [folder["id"]]
    t = svc.by_name("2026-09-28_CM01_transcription.txt")
    assert t["parents"] == [transcriptions["id"]] and t["source_mime"] == "text/plain"
    text = t["content"].decode("utf-8")
    assert text.startswith("# Transcription — Graphes — CM 1 — 2026-09-28\n\nSéance (informations de l'app")
    for line in ("- Matière : Graphes", "- Séance : CM 1", "- Date : lundi 28 septembre 2026", "- Horaire : 10:15 – 12:15",
                 "- Salle : IS_A213", "- Enseignant : DURAND Sophie", "- Intitulé dans l'emploi du temps : Graphes CM",
                 "- Durée de l'enregistrement : 1 h 30 min",
                 "- Support(s) de cours, dossier « Supports » : 2026-09-28_CM01_CM1 diapos.pdf"):
        assert line in text, line
    assert text.rstrip().endswith("[00:00:00] L1 : Bonjour.")
    pdf = svc.by_name("2026-09-28_CM01_CM1 diapos.pdf")
    assert pdf["parents"] == [support_dir["id"]] and pdf["content"].startswith(b"%PDF")
    assert db.get_recording(rid)["drive_transcription_url"] == t["webViewLink"]
    # Nouveau dépôt : même fichier de transcription mis à jour, support pas renvoyé.
    n_files = len(svc.store)
    drive_pub.deposit_inputs(rid, client)
    assert len(svc.store) == n_files
    assert [x[0] for x in svc.log].count("update") == 1


def test_automation_mode_leaves_course_files_to_the_automation(tmp_path, automation):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    rid = new_session(tmp_path, with_support=False)
    subjects.course_path(rid).write_text("# CM 1 – Graphes\n\nTexte.")
    subjects.write_state(db.get_subject(db.get_recording(rid)["subject_id"]), "# État\n")
    drive_pub.publish_recording(rid, client)
    names = {f["name"] for f in svc.store.values()}
    assert "2026-09-28_CM01_transcription.txt" in names
    assert not any(n.endswith(".md") for n in names) and "_etat.md" not in names
    assert not any("NotebookLM" in n or "Cours complet" in n for n in names)
    with pytest.raises(PublishSkipped):
        drive_pub.publish_subject(db.get_recording(rid)["subject_id"], client)


def test_app_mode_does_not_deposit(tmp_path):
    svc = FakeDriveService()
    rid = new_session(tmp_path, with_support=False)
    subjects.course_path(rid).write_text("# CM 1 – Graphes\n\nTexte.")
    drive_pub.publish_recording(rid, drive_pub.DriveClient(service=svc))
    names = {f["name"] for f in svc.store.values()}
    assert "Transcriptions" not in names and any(n.endswith("_graphes.md") or n.endswith(".md") for n in names)


def test_transcription_is_deposited_right_after_transcribing(tmp_path, monkeypatch, automation):
    deposits = []
    monkeypatch.setattr(drive_pub, "is_configured", lambda: True)
    monkeypatch.setattr(drive_pub, "deposit_inputs", lambda rid, client=None: deposits.append(rid) or {"x": 1})
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, vocab, rid=None: {
        "text": "t", "segments": [{"text": "Bonjour.", "start": 0, "end": 2, "speaker_id": "s1"}]})
    sid = subjects.create_subject("Graphes")
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")
    recorder.audio_path(rid).write_bytes(b"mp3")
    pl.Pipeline().step_transcribe(rid)
    assert deposits == [rid]
    # Drive injoignable : la transcription reste réussie, l'échec est noté (nouvel essai à la publication).
    monkeypatch.setattr(drive_pub, "deposit_inputs", lambda rid, client=None: (_ for _ in ()).throw(RuntimeError("hors ligne")))
    pl.Pipeline().step_transcribe(rid)
    assert db.get_recording(rid)["status"] == "transcribed"
    assert any("hors ligne" in log["message"] for log in db.list_logs(rid, 10))


def test_settings_switch_mode(automation):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        pl.pipeline.stop()
        page = client.get("/parametres").text
        assert "Mode automatisation" in page and 'value="automatisation" checked' in page
        r = client.post("/parametres/drive", data={"drive_writer": "app", "drive_root_name": "Cours M1"}, follow_redirects=False)
        assert r.status_code == 303 and db.get_setting("drive_writer") == "app"
        r = client.post("/parametres/drive", data={"drive_writer": "n'importe quoi"}, follow_redirects=False)
        assert "level=err" in r.headers["location"] and db.get_setting("drive_writer") == "app"
        sid = subjects.create_subject("Graphes")
        assert "Régénérer le cours complet" in client.get(f"/cours/{sid}").text
        db.set_setting("drive_writer", "automatisation")
        assert "Régénérer le cours complet" not in client.get(f"/cours/{sid}").text


def test_format_step_still_runs_in_automation_mode(tmp_path, monkeypatch, automation):
    """La mise en forme locale (Mistral) et Notion continuent : seul Drive change de rôle."""
    monkeypatch.setattr(llm, "format_course", lambda data, state, meta, on_progress=None, support_docs=None:
                        llm.finalize_course("# Séance\n\n## A\n\nTexte.", meta))
    monkeypatch.setattr(llm, "update_state", lambda old, course, meta: old)
    monkeypatch.setattr(llm, "suggest_terms", lambda course, vocab, exclude=(): [])
    rid = new_session(tmp_path, with_support=False)
    (recorder.recording_dir(rid) / "transcript.json").write_text(json.dumps(
        {"segments": [{"text": "Bonjour.", "start": 0, "end": 2, "speaker_id": "s1"}]}))
    pl.Pipeline().step_format(rid)
    assert subjects.read_course(rid).startswith("# CM 1 – Séance")


def test_session_already_written_by_the_app_is_not_deposited(tmp_path, automation):
    svc = FakeDriveService()
    rid = new_session(tmp_path, with_support=False)
    subjects.course_path(rid).write_text("# CM 1 – Graphes\n\nTexte.")
    db.update_recording(rid, drive_md_id="ancienne-fiche")  # publiée par l'app avant le passage en mode automatisation
    with pytest.raises(PublishSkipped, match="seconde fois"):
        drive_pub.publish_recording(rid, drive_pub.DriveClient(service=svc))
    assert not svc.store
