"""Drive : l'app dépose transcriptions et supports, la tâche Claude rédige les cours dans `Séances/`, l'app les
récupère pour les afficher."""

import json

import pytest

from app import config, db, recorder, subjects, supports
from app.publish import PublishSkipped
from app.publish import drive as drive_pub
from fakes import FakeDriveService, make_pdf

EVENT = {"uid": "u1", "summary": "Graphes CM", "start": "2026-09-28T10:15:00+02:00",
         "end": "2026-09-28T12:15:00+02:00", "location": "IS_A213"}
CLAUDE_COURSE = ("# CM 1 – Plus courts chemins\n\n*Graphes — CM 1 du lundi 28 septembre 2026 — Enseignant : DURAND Sophie*\n\n"
                 "## 1. Dijkstra\n\nTexte.\n")


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


def test_deposit_transcription_and_supports(tmp_path):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    rid = new_session(tmp_path)
    drive_pub.publish_recording(rid, client)
    folder = svc.by_name("Graphes")
    transcriptions, support_dir = svc.by_name("Transcriptions"), svc.by_name("Supports")
    assert transcriptions["parents"] == [folder["id"]] and support_dir["parents"] == [folder["id"]]
    assert svc.by_name("Séances")["parents"] == [folder["id"]]  # où la tâche Claude écrit les cours
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
    # Ni fiche de séance, ni cours complet, ni _etat.md, ni Google Doc : c'est la tâche Claude qui les écrit.
    assert not any(f["name"].endswith(".md") for f in svc.store.values())
    # Nouveau dépôt : même fichier de transcription mis à jour, support pas renvoyé.
    n_files = len(svc.store)
    drive_pub.publish_recording(rid, client)
    assert len(svc.store) == n_files
    assert [x[0] for x in svc.log].count("update") == 1


def test_nothing_to_deposit_is_an_error(tmp_path):
    rid = new_session(tmp_path, with_support=False)
    (recorder.recording_dir(rid) / "transcript.txt").unlink()
    with pytest.raises(RuntimeError, match="Aucune transcription"):
        drive_pub.publish_recording(rid, drive_pub.DriveClient(service=FakeDriveService()))


def test_session_already_written_by_the_app_is_not_deposited(tmp_path):
    svc = FakeDriveService()
    rid = new_session(tmp_path, with_support=False)
    db.update_recording(rid, drive_md_id="ancienne-fiche")  # rédigée par l'ancienne version de l'app
    with pytest.raises(PublishSkipped, match="seconde fois"):
        drive_pub.publish_recording(rid, drive_pub.DriveClient(service=svc))
    assert not svc.store


def test_fetch_courses_written_by_claude(tmp_path):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    rid = new_session(tmp_path, with_support=False)
    sid = db.get_recording(rid)["subject_id"]
    drive_pub.publish_recording(rid, client)
    assert drive_pub.fetch_courses(client=client) == []  # Claude n'a encore rien écrit

    seances, folder = svc.by_name("Séances"), svc.by_name("Graphes")
    claude = svc.add_external("2026-09-28_CM01_plus-courts-chemins.md", seances["id"], CLAUDE_COURSE)
    svc.add_external("2026-10-05_CM02_seance-inconnue.md", seances["id"], "# CM 2 – Autre\n")  # pas de séance CM 2
    svc.add_external("notes.md", seances["id"], "# Notes\n")
    full = svc.add_external("Graphes – Cours complet.md", folder["id"], "# Graphes – Cours complet\n")
    doc = svc.add_external("Graphes – NotebookLM", folder["id"], "")
    subjects.course_path(rid).write_text("# CM 1 – Ancienne version Mistral\n", encoding="utf-8")

    fetched = drive_pub.fetch_courses(client=client)
    assert [r["id"] for r in fetched] == [rid]
    rec = db.get_recording(rid)
    assert subjects.read_course(rid) == CLAUDE_COURSE and rec["title"] == "Plus courts chemins"
    assert rec["drive_md_id"] == claude["id"] and rec["drive_md_url"] == claude["webViewLink"]
    assert list((recorder.recording_dir(rid) / "versions").glob("course_*.md"))  # l'ancienne version est gardée
    assert '"title": "Plus courts chemins"' in (recorder.recording_dir(rid) / "meta.json").read_text(encoding="utf-8")
    subject = db.get_subject(sid)
    assert subject["drive_full_url"] == full["webViewLink"] and subject["drive_gdoc_url"] == doc["webViewLink"]
    assert "Plus courts chemins" in (subjects.subject_dir(subject) / "cours_complet.md").read_text(encoding="utf-8")
    assert drive_pub.fetch_courses(client=client) == []  # rien de nouveau

    # Claude réécrit le cours : la nouvelle version remplace l'ancienne.
    svc.rewrite_external(claude["id"], CLAUDE_COURSE.replace("Texte.", "Texte corrigé."))
    assert [r["id"] for r in drive_pub.fetch_courses(client=client)] == [rid]
    assert "Texte corrigé." in subjects.read_course(rid)

    # Séance renumérotée après coup (faux départ supprimé) : le fichier déjà associé reste celui de la séance.
    db.update_recording(rid, session_number=2)
    subjects.course_path(rid).unlink()
    assert [r["id"] for r in drive_pub.fetch_courses(client=client)] == [rid]
    assert "Texte corrigé." in subjects.read_course(rid)


def _write_token(scopes: list[str]) -> None:
    config.CREDENTIALS_FILE.write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}))
    config.TOKEN_FILE.write_text(json.dumps({
        "token": "t", "refresh_token": "r", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "c",
        "client_secret": "s", "scopes": scopes, "expiry": "2099-01-01T00:00:00Z"}))


def test_reading_courses_needs_the_read_scope():
    _write_token(["https://www.googleapis.com/auth/drive.file"])
    with pytest.raises(drive_pub.DriveReadError, match="drive.readonly"):
        drive_pub.fetch_courses()
    assert "drive.readonly" in drive_pub.sync_state["error"]
    _write_token(drive_pub.SCOPES)
    assert drive_pub.can_read(drive_pub.load_credentials())


def test_saved_token_keeps_only_granted_scopes():
    from google.oauth2.credentials import Credentials

    creds = Credentials("t", refresh_token="r", token_uri="https://oauth2.googleapis.com/token", client_id="c",
                        client_secret="s", scopes=drive_pub.SCOPES,
                        granted_scopes="https://www.googleapis.com/auth/drive.file")  # lecture décochée chez Google
    drive_pub._save(creds)
    assert json.loads(config.TOKEN_FILE.read_text())["scopes"] == ["https://www.googleapis.com/auth/drive.file"]


def test_automatic_fetch_every_ten_minutes(monkeypatch):
    calls = []

    class SyncThread:
        def __init__(self, target, name=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(drive_pub, "fetch_courses", lambda: calls.append(1) or [])
    monkeypatch.setattr(drive_pub.threading, "Thread", SyncThread)
    monkeypatch.setitem(drive_pub.sync_state, "at", 0.0)
    drive_pub.fetch_courses_if_due()
    assert calls == []  # Drive pas connecté
    _write_token(drive_pub.SCOPES)
    drive_pub.fetch_courses_if_due()
    drive_pub.fetch_courses_if_due()
    assert calls == [1]  # une seule fois par tranche de 10 minutes
    monkeypatch.setitem(drive_pub.sync_state, "at", drive_pub.sync_state["at"] - drive_pub.SYNC_INTERVAL_S)
    drive_pub.fetch_courses_if_due()
    assert calls == [1, 1]


def test_deposit_recreates_a_folder_deleted_in_drive(tmp_path):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    rid = new_session(tmp_path, with_support=False)
    drive_pub.publish_recording(rid, client)
    old = svc.by_name("Transcriptions")
    old["trashed"] = True  # supprimé à la main dans Drive
    svc.by_name("2026-09-28_CM01_transcription.txt")["trashed"] = True
    drive_pub.publish_recording(rid, client)
    new = svc.by_name("Transcriptions")
    assert new["id"] != old["id"] and svc.by_name("2026-09-28_CM01_transcription.txt")["parents"] == [new["id"]]


def test_drive_not_configured_or_not_connected():
    with pytest.raises(PublishSkipped):
        drive_pub.load_credentials()
    config.CREDENTIALS_FILE.write_text("{}", encoding="utf-8")
    with pytest.raises(drive_pub.DriveAuthError):
        drive_pub.load_credentials()
