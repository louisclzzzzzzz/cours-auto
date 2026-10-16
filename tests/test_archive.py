"""Archivage de l'audio : envoi dans Drive (`Cours M1/Z-Data/recordings/<id>/`), vérification, suppression sur le disque,
rapatriement, planification (lancement + chaque soir) et affichage."""

import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import archive, calendar_ics, config, db, pipeline as pl, recorder, subjects
from app.publish import drive as drive_pub
from app.textutils import fmt_size
from fakes import FakeDownloader, FakeDriveService

AUDIO = b"ID3-audio-final" * 500
FULL = "https://www.googleapis.com/auth/drive"
PARIS = ZoneInfo("Europe/Paris")


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def new_recording(*, status="done", chunks=0, source: tuple[str, bytes] | None = None, transcript=True,
                  audio: bytes | None = AUDIO, subject="Graphes") -> int:
    sid = subjects.create_subject(subject)
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")
    folder = recorder.recording_dir(rid)
    if audio is not None:
        (folder / "audio.mp3").write_bytes(audio)
    for i in range(1, chunks + 1):
        (folder / f"chunk_{i:04d}.webm").write_bytes(bytes([i]) * 100 * i)
        db.upsert_chunk(rid, i, 1, "webm", 100 * i)
    if source:
        (folder / source[0]).write_bytes(source[1])
    if transcript:
        (folder / "transcript.txt").write_text("# Transcription\n\n[00:00:00] L1 : Bonjour.\n", encoding="utf-8")
    db.update_recording(rid, status=status, drive_status="done")
    return rid


@pytest.fixture
def drive():
    svc = FakeDriveService()
    return svc, drive_pub.DriveClient(service=svc)


@pytest.fixture
def user_tree(drive):
    """Ce que l'utilisateur a créé à la main dans Drive : Cours M1 / Z-Data / recordings."""
    svc, _ = drive
    root = svc.add_folder("Cours M1")
    db.set_setting("drive_root_id", root["id"])
    z_data = svc.add_folder("Z-Data", root["id"])
    recordings = svc.add_folder("recordings", z_data["id"])
    return root, z_data, recordings


def write_token(scopes: list[str]) -> None:
    config.CREDENTIALS_FILE.write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}))
    config.TOKEN_FILE.write_text(json.dumps({
        "token": "t", "refresh_token": "r", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "c",
        "client_secret": "s", "scopes": scopes, "expiry": "2099-01-01T00:00:00Z"}))


# --- Envoi, vérification, suppression ------------------------------------------------------------------

def test_audio_is_sent_verified_then_removed_from_disk(drive):
    svc, client = drive
    rid = new_recording(chunks=3, source=("source.m4a", b"original-m4a" * 100))
    folder = recorder.recording_dir(rid)
    before = len(AUDIO) + (100 + 200 + 300) + len(b"original-m4a" * 100)

    result = archive.sweep(pl.Pipeline(), client)

    assert result["archived"] == 1 and result["bytes"] == before and not result["errors"]
    # Cours M1 / Z-Data / recordings / <id> / …
    root, z_data, recordings, drive_folder = (svc.by_name(n) for n in ("Cours M1", "Z-Data", "recordings", str(rid)))
    assert z_data["parents"] == [root["id"]] and recordings["parents"] == [z_data["id"]]
    assert drive_folder["parents"] == [recordings["id"]]
    # L'audio final (et le fichier importé) sont dans Drive ; les morceaux bruts n'y sont pas envoyés.
    assert [f["name"] for f in svc.children(drive_folder["id"])] == ["audio.mp3", "source.m4a"]
    audio = svc.by_name("audio.mp3")
    assert audio["content"] == AUDIO and audio["source_mime"] == "audio/mpeg"
    assert svc.by_name("source.m4a")["content"] == b"original-m4a" * 100
    # Disque libéré (morceaux bruts supprimés compris) ; la transcription reste.
    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == ["meta.json", "transcript.txt"]
    rec = db.get_recording(rid)
    assert rec["audio_drive_id"] == audio["id"] and rec["audio_drive_url"] == audio["webViewLink"]
    assert rec["audio_drive_folder_id"] == drive_folder["id"] and rec["audio_archived_at"]
    assert rec["audio_archived_bytes"] == before and archive.is_archived(rec)
    # Un second passage n'a plus rien à faire.
    n = len(svc.log)
    assert archive.sweep(pl.Pipeline(), client)["archived"] == 0 and len(svc.log) == n


def test_raw_chunks_are_deleted_but_never_uploaded(drive):
    svc, client = drive
    rid = new_recording(chunks=4)
    folder = recorder.recording_dir(rid)
    assert len(list(folder.glob("chunk_*"))) == 4

    assert archive.sweep(pl.Pipeline(), client)["archived"] == 1

    assert not list(folder.glob("chunk_*")) and not recorder.audio_path(rid).exists()
    assert [f["name"] for f in svc.children(svc.by_name(str(rid))["id"])] == ["audio.mp3"]
    assert not [f for f in svc.store.values() if f["name"].endswith((".webm", ".zip"))]
    rec = db.get_recording(rid)
    assert archive.raw_audio_gone(rec) and rec["audio_archived_bytes"] == len(AUDIO) + 100 + 200 + 300 + 400
    assert not archive.raw_audio_gone(db.get_recording(new_recording(chunks=2)))  # jamais archivée : morceaux conservés


def test_audio_already_uploaded_by_hand_is_recognised_not_duplicated(drive, user_tree):
    svc, client = drive
    _, z_data, recordings = user_tree
    rid = new_recording()
    manual = svc.add_folder(str(rid), recordings["id"])
    existing = svc.add_blob("audio.mp3", manual["id"], AUDIO)  # envoi à la main, avant l'archivage automatique

    result = archive.sweep(pl.Pipeline(), client)

    assert result["archived"] == 1
    assert not [e for e in svc.log if e[0] == "create"]  # rien n'a été (re)créé, pas même un dossier
    assert [f["id"] for f in svc.children(manual["id"])] == [existing["id"]]
    assert db.get_recording(rid)["audio_drive_id"] == existing["id"]
    assert not recorder.audio_path(rid).exists()
    assert db.get_setting("audio_archive_folder_id") == z_data["id"]  # le Z-Data de l'utilisateur, pas un doublon


def test_a_different_file_with_the_same_name_is_never_overwritten(drive, user_tree):
    svc, client = drive
    _, _, recordings = user_tree
    rid = new_recording()
    manual = svc.add_folder(str(rid), recordings["id"])
    other = svc.add_blob("audio.mp3", manual["id"], b"autre audio, d'une ancienne base")

    assert archive.sweep(pl.Pipeline(), client)["archived"] == 1

    assert other["content"] == b"autre audio, d'une ancienne base" and not other["trashed"]
    copy = svc.by_name("audio_2.mp3")
    assert copy["content"] == AUDIO and db.get_recording(rid)["audio_drive_id"] == copy["id"]
    # Une reprise après interruption retrouve cette copie au lieu d'en créer une troisième.
    recorder.audio_path(rid).write_bytes(AUDIO)
    db.update_recording(rid, audio_restored_at=None)
    archive.sweep(pl.Pipeline(), client)
    assert [f["name"] for f in svc.children(manual["id"])] == ["audio.mp3", "audio_2.mp3"]


def test_nothing_is_deleted_when_the_drive_copy_does_not_match(drive):
    svc, client = drive
    svc._files.corrupt_uploads = True
    rid = new_recording(chunks=2)
    folder = recorder.recording_dir(rid)
    names = sorted(p.name for p in folder.iterdir())

    result = archive.sweep(pl.Pipeline(), client)

    assert result["archived"] == 0 and "ne correspond pas" in result["errors"][0]
    assert sorted(p.name for p in folder.iterdir()) == names  # tout est toujours là
    rec = db.get_recording(rid)
    assert not rec["audio_drive_id"] and "ne correspond pas" in rec["audio_archive_error"]
    copies = [f for f in svc.store.values() if f["name"] == "audio.mp3"]
    assert copies and all(f["trashed"] for f in copies)  # copie douteuse mise à la corbeille
    assert not archive.is_archived(rec)


def test_one_failing_recording_does_not_block_the_others(drive, monkeypatch):
    _, client = drive
    first, second = new_recording(), new_recording()
    real = archive.archive_recording

    def flaky(rid, client, folder=None):
        if rid == first:
            raise OSError("réseau coupé")
        return real(rid, client, folder)

    monkeypatch.setattr(archive, "archive_recording", flaky)
    result = archive.sweep(pl.Pipeline(), client)
    assert result["archived"] == 1 and len(result["errors"]) == 1 and "réseau coupé" in result["errors"][0]
    assert recorder.audio_path(first).exists() and not recorder.audio_path(second).exists()
    assert "réseau coupé" in db.get_recording(first)["audio_archive_error"]


def test_only_processed_recordings_are_archived(drive):
    svc, client = drive
    waiting = new_recording(status="uploaded")  # en attente de « Lancer le traitement » : la transcription a besoin de l'audio
    failed = new_recording(status="error")
    transcribing = new_recording(status="transcribing")
    interrupted = new_recording(status="interrupted")
    no_transcript = new_recording(transcript=False)  # l'audio serait la seule trace du cours
    no_audio = new_recording(audio=None)
    held = new_recording()
    ok = new_recording()
    guard = pl.Pipeline()
    assert guard.hold(held)  # un traitement vient de la prendre

    result = archive.sweep(guard, client)

    assert result["archived"] == 1
    for rid in (waiting, failed, transcribing, interrupted, held):
        assert recorder.audio_path(rid).exists(), rid
    assert recorder.audio_path(no_transcript).exists() and not recorder.audio_path(no_audio).exists()
    assert not recorder.audio_path(ok).exists() and db.get_recording(ok)["audio_drive_id"]
    assert not guard._held - {held}  # les séances réservées par l'archivage sont libérées


def test_pipeline_hold_blocks_processing_and_is_blocked_by_it():
    p = pl.Pipeline()
    assert p.hold(1) and p.is_active(1) and not p.hold(1)
    assert not p.submit("recording", 1, "transcribe")  # audio en cours de suppression : aucun traitement
    p.release(1)
    assert not p.is_active(1) and p.submit("recording", 1, "transcribe")
    assert not p.hold(1)  # un traitement est en file : pas d'archivage


# --- Rapatriement ----------------------------------------------------------------------------------------

@pytest.fixture
def downloads(monkeypatch):
    monkeypatch.setattr(drive_pub, "MediaIoBaseDownload", FakeDownloader)


def test_restore_brings_the_audio_back_and_keeps_it_for_a_few_days(drive, downloads):
    svc, client = drive
    rid = new_recording(chunks=2)
    archive.sweep(pl.Pipeline(), client)
    assert not recorder.audio_path(rid).exists()

    path = archive.restore_audio(rid, client)

    assert path.read_bytes() == AUDIO and not list(path.parent.glob("*.part"))
    rec = db.get_recording(rid)
    assert rec["audio_restored_at"] and not archive.is_archived(rec)
    now = datetime.now().astimezone()
    assert rid not in [r["id"] for r in archive.eligible(now)]  # restauré à la main : gardé quelques jours
    assert rid in [r["id"] for r in archive.eligible(now + archive.RESTORE_KEEP + timedelta(hours=1))]
    # Au passage suivant, la copie restaurée est reconnue dans Drive (rien de renvoyé) puis supprimée.
    n = len(svc.log)
    later = now + archive.RESTORE_KEEP + timedelta(hours=1)
    assert archive.sweep(pl.Pipeline(), client, now=later)["archived"] == 1
    assert len(svc.log) == n and not recorder.audio_path(rid).exists()
    assert db.get_recording(rid)["audio_restored_at"] is None
    assert db.get_recording(rid)["audio_archived_bytes"] > len(AUDIO)  # le total libéré (morceaux bruts compris) n'est pas écrasé


def test_restore_rejects_a_corrupted_download(drive, downloads):
    svc, client = drive
    rid = new_recording()
    archive.sweep(pl.Pipeline(), client)
    svc.by_name("audio.mp3")["content"] = b"contenu altere"  # la somme MD5 annoncée par Drive ne correspond plus
    with pytest.raises(archive.ArchiveError, match="corrompu"):
        archive.restore_audio(rid, client)
    assert not list(recorder.recording_dir(rid).glob("audio.mp3*"))


def test_restore_when_the_file_is_gone_from_drive(drive, downloads):
    svc, client = drive
    rid = new_recording()
    archive.sweep(pl.Pipeline(), client)
    svc.by_name("audio.mp3")["trashed"] = True
    with pytest.raises(archive.ArchiveError, match="introuvable"):
        archive.restore_audio(rid, client)
    with pytest.raises(archive.ArchiveError, match="pas d'audio archivé"):
        archive.restore_audio(new_recording(audio=None), client)  # jamais archivé et plus d'audio


@pytest.fixture
def pipeline_fakes(monkeypatch):
    from app import transcribe

    calls = {"restore": 0, "drive": 0}

    def restore(rid, client=None):
        calls["restore"] += 1
        recorder.audio_path(rid).write_bytes(AUDIO)

    monkeypatch.setattr(archive, "restore_audio", restore)
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, vocab, rid=None: {
        "text": "t", "segments": [{"text": "Bonjour.", "start": 0, "end": 3, "speaker_id": "speaker_1"}]})
    monkeypatch.setattr(pl.drive_pub, "publish_recording", lambda rid: calls.update(drive=calls["drive"] + 1))
    return calls


def test_a_new_transcription_fetches_the_archived_audio_by_itself(drive, pipeline_fakes):
    _, client = drive
    rid = new_recording()
    archive.sweep(pl.Pipeline(), client)
    assert not recorder.audio_path(rid).exists()

    p = pl.Pipeline()
    p._run(pl.Job("recording", rid, "transcribe"))

    rec = db.get_recording(rid)
    assert pipeline_fakes["restore"] == 1 and rec["status"] == "done" and pipeline_fakes["drive"] == 1, rec["error_message"]


def test_transcription_without_any_audio_still_fails_clearly(pipeline_fakes):
    rid = new_recording(status="uploaded", audio=None)
    p = pl.Pipeline()
    p._run(pl.Job("recording", rid, "transcribe"))
    rec = db.get_recording(rid)
    assert rec["status"] == "error" and "Audio final absent" in rec["error_message"] and pipeline_fakes["restore"] == 0


# --- Planification : au lancement, puis chaque soir ------------------------------------------------------------

@pytest.fixture
def launches(monkeypatch):
    runs = []
    monkeypatch.setattr(archive, "_start", lambda guard: runs.append(1))
    return runs


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PARIS)


def test_runs_at_launch_then_every_evening(launches):
    guard = pl.Pipeline()
    write_token([FULL])

    def due(when):
        archive.state["running"] = False  # le passage précédent est terminé
        return archive.sweep_if_due(guard, when)

    assert due(at(16, 9, 30))  # lancement de l'app
    assert not due(at(16, 9, 31)) and not due(at(16, 15))
    assert due(at(16, 21, 0))  # le soir (21 h par défaut)
    assert not due(at(16, 21, 5)) and not due(at(16, 23, 59))
    assert not due(at(17, 8, 0))  # lendemain matin : rien avant le soir
    assert due(at(17, 21, 15))
    assert len(launches) == 3


def test_launch_after_the_evening_hour_counts_as_the_evening_pass(launches):
    write_token([FULL])
    assert archive.sweep_if_due(pl.Pipeline(), at(16, 22, 30))
    archive.state["running"] = False
    assert not archive.sweep_if_due(pl.Pipeline(), at(16, 22, 45))


def test_no_pass_while_running_when_disabled_or_when_drive_is_not_connected(launches):
    guard = pl.Pipeline()
    assert not archive.sweep_if_due(guard, at(16, 10))  # Drive pas configuré
    config.CREDENTIALS_FILE.write_text("{}")
    assert not archive.sweep_if_due(guard, at(16, 10))  # configuré mais pas connecté
    write_token([FULL])
    db.set_setting("audio_archive", "0")
    assert not archive.sweep_if_due(guard, at(16, 10))  # désactivé
    db.set_setting("audio_archive", "1")
    assert archive.sweep_if_due(guard, at(16, 10)) and archive.state["running"]
    assert not archive.sweep_if_due(guard, at(16, 21, 30))  # un passage est déjà en cours
    assert launches == [1]


def test_evening_hour_is_configurable(launches):
    write_token([FULL])
    db.set_setting("audio_archive_hour", "18")
    guard = pl.Pipeline()
    assert archive.sweep_if_due(guard, at(16, 9))
    archive.state["running"] = False
    assert not archive.sweep_if_due(guard, at(16, 17, 59)) and archive.sweep_if_due(guard, at(16, 18))


def test_failed_pass_is_retried_an_hour_later(drive, monkeypatch):
    write_token([FULL])
    guard = pl.Pipeline()
    new_recording()

    class SyncThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(archive.threading, "Thread", SyncThread)
    monkeypatch.setattr(archive, "_client", lambda: (_ for _ in ()).throw(OSError("hors ligne")))
    assert archive.sweep_if_due(guard, at(16, 9))
    assert archive.state["retry_at"] > 0 and not archive.state["running"]
    assert "hors ligne" in db.get_setting("audio_archive_last_error")
    assert not archive.sweep_if_due(guard, at(16, 9, 30))
    archive.state["retry_at"] -= archive.RETRY_AFTER_ERROR_S + 1
    assert archive.sweep_if_due(guard, at(16, 10, 30))


def test_missing_full_access_is_reported_and_not_retried(monkeypatch):
    write_token(["https://www.googleapis.com/auth/drive.file", drive_pub.READ_SCOPE])  # connexion d'avant l'archivage
    rid = new_recording()
    archive._run(pl.Pipeline())
    assert "…/auth/drive" in db.get_setting("audio_archive_last_error")
    assert archive.state["retry_at"] == 0 and recorder.audio_path(rid).exists()
    assert not drive_pub.can_archive(drive_pub.load_credentials()) and drive_pub.can_read(drive_pub.load_credentials())
    write_token(drive_pub.SCOPES)
    assert drive_pub.can_archive(drive_pub.load_credentials())


def test_nothing_to_archive_needs_no_drive_call():
    archive._run(pl.Pipeline())  # Drive non connecté, aucune séance à archiver : pas d'erreur
    assert db.get_setting("audio_archive_last_result") == "Rien à archiver." and not db.get_setting("audio_archive_last_error")


# --- Client Drive --------------------------------------------------------------------------------------------

def test_named_folder_reuses_existing_ones_without_renaming(drive, user_tree):
    svc, client = drive
    root, z_data, _ = user_tree
    assert client.ensure_named_folder("Z-Data", root["id"])["id"] == z_data["id"]
    again = client.ensure_named_folder("Z-Data", root["id"], z_data["id"])
    assert again["id"] == z_data["id"] and z_data["name"] == "Z-Data"
    created = client.ensure_named_folder("Autre", root["id"], z_data["id"])  # l'identifiant mémorisé a un autre nom
    assert created["id"] != z_data["id"] and z_data["name"] == "Z-Data"
    # Un dossier à la corbeille n'est pas réutilisé.
    z_data["trashed"] = True
    assert client.ensure_named_folder("Z-Data", root["id"])["id"] not in (z_data["id"],)


def test_names_with_quotes_are_searchable(drive):
    svc, client = drive
    root = svc.add_folder("Cours M1")
    svc.add_blob("l'audio \\ test.mp3", root["id"], b"x")
    assert [f["name"] for f in client.find_children(root["id"], "l'audio \\ test.mp3", folder=False)] == ["l'audio \\ test.mp3"]
    assert client.find_children(root["id"], "l'audio \\ test.mp3", folder=True) == []


def test_fmt_size():
    assert fmt_size(0) == "0 Ko" and fmt_size(850 * 1024) == "850 Ko"
    assert fmt_size(12.5 * 1024 ** 2) == "12,5 Mo" and fmt_size(1.4 * 1024 ** 3) == "1,4 Go"


# --- Pages ---------------------------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from app.main import app

    monkeypatch.setattr(calendar_ics, "now_local", lambda: datetime(2026, 9, 27, 12, 0, tzinfo=PARIS))
    with TestClient(app) as c:
        pl.pipeline.stop()
        yield c


def archived_recording(drive) -> int:
    svc, dclient = drive
    rid = new_recording(chunks=2)
    archive.sweep(pl.Pipeline(), dclient)
    return rid


def test_settings_page_shows_and_saves_the_archive_options(client):
    page = client.get("/parametres").text
    assert "Archivage de l'audio" in page and 'value="Z-Data"' in page and "Archiver maintenant" in page
    assert "À archiver maintenant : <strong>0</strong>" in page
    r = client.post("/parametres/archivage", data={"audio_archive_folder": " Mes  Archives ", "audio_archive_hour": "19"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert (db.get_setting("audio_archive"), db.get_setting("audio_archive_folder"), db.get_setting("audio_archive_hour")) == (
        "0", "Mes Archives", "19")  # case décochée : désactivé
    assert "Désactivé" in client.get("/parametres").text
    for bad in ({"audio_archive_hour": "24"}, {"audio_archive_hour": "soir"}, {"audio_archive_folder": "a/b"}, {"audio_archive_folder": " "}):
        r = client.post("/parametres/archivage", data={"audio_archive": "1", "audio_archive_folder": "Z-Data",
                                                       "audio_archive_hour": "21", **bad}, follow_redirects=False)
        assert "level=err" in r.headers["location"]
    assert db.get_setting("audio_archive_hour") == "19"


def test_settings_page_counts_and_asks_for_full_access(client, monkeypatch):
    rid = new_recording(status="uploaded")
    new_recording()
    page = client.get("/parametres").text
    assert "À archiver maintenant : <strong>1</strong>" in page and "pas encore traitée(s)" in page
    r = client.post("/parametres/archivage/lancer", follow_redirects=False)  # Drive pas connecté
    assert "level=err" in r.headers["location"]
    monkeypatch.setattr(drive_pub, "connection_status", lambda force=False: {
        "configured": True, "connected": True, "can_read": True, "can_archive": False, "message": "Connecté (moi@example.com)"})
    page = client.get("/parametres").text
    assert "Autorisation à compléter" in page and "…/auth/drive</code> dans" in page
    assert recorder.audio_path(rid).exists()  # rien n'a été archivé sans l'autorisation


def test_archive_now_starts_a_pass(client, monkeypatch):
    started = []
    monkeypatch.setattr(archive, "_start", lambda guard: started.append(guard))
    write_token([FULL])
    r = client.post("/parametres/archivage/lancer", follow_redirects=False)
    assert r.status_code == 303 and "level=err" not in r.headers["location"] and started == [pl.pipeline]
    r = client.post("/parametres/archivage/lancer", follow_redirects=False)  # déjà en cours
    assert "level=err" in r.headers["location"] and len(started) == 1


def test_detail_page_of_an_archived_recording(client, drive):
    rid = archived_recording(drive)
    page = client.get(f"/enregistrements/{rid}").text
    assert "Archivé dans Drive" in page and f'action="/enregistrements/{rid}/audio/restaurer"' in page
    assert "archivé dans Drive</span>" in page  # étape « Audio » faite, avec sa mention
    assert "<audio" not in page
    audio = client.get(f"/enregistrements/{rid}/audio")
    assert audio.status_code == 404 and "restaurez-le" in audio.text
    # Le morceau-à-morceau n'existe plus : l'assemblage ne peut pas être refait, la transcription oui (audio rapatrié tout seul).
    r = client.post(f"/enregistrements/{rid}/action", data={"action": "finalize"}, follow_redirects=False)
    assert "level=err" in r.headers["location"] and "archiv" in r.headers["location"]
    assert db.get_recording(rid)["status"] == "done"


def test_restored_recording_cannot_be_reassembled_from_deleted_chunks(client, drive, monkeypatch):
    rid = archived_recording(drive)  # 2 morceaux supprimés après l'archivage
    monkeypatch.setattr(archive, "restore_audio", lambda rid, client=None: recorder.audio_path(rid).write_bytes(AUDIO))
    client.post(f"/enregistrements/{rid}/audio/restaurer")
    assert recorder.audio_path(rid).exists() and not archive.is_archived(db.get_recording(rid))
    page = client.get(f"/enregistrements/{rid}").text
    assert "morceaux bruts ont été supprimés" in page  # le bouton « Refaire l'assemblage » est désactivé
    r = client.post(f"/enregistrements/{rid}/action", data={"action": "finalize"}, follow_redirects=False)
    assert "level=err" in r.headers["location"] and "archivage" in r.headers["location"]
    assert db.get_recording(rid)["status"] == "done" and recorder.audio_path(rid).read_bytes() == AUDIO


def test_restore_button(client, drive, monkeypatch):
    rid = archived_recording(drive)
    # Drive non connecté dans ce test : message clair, rien n'est cassé.
    r = client.post(f"/enregistrements/{rid}/audio/restaurer", follow_redirects=False)
    assert "level=err" in r.headers["location"] and "Restauration+impossible" in r.headers["location"]
    assert not pl.pipeline._held
    def restore(rid, client=None):
        recorder.audio_path(rid).write_bytes(AUDIO)
        db.update_recording(rid, audio_restored_at=db.now_iso())

    monkeypatch.setattr(archive, "restore_audio", restore)
    r = client.post(f"/enregistrements/{rid}/audio/restaurer", follow_redirects=False)
    assert r.status_code == 303 and "level=err" not in r.headers["location"] and not pl.pipeline._held
    page = client.get(f"/enregistrements/{rid}").text
    assert "<audio" in page and "Restauré depuis Drive" in page
    plain = new_recording()
    r = client.post(f"/enregistrements/{plain}/audio/restaurer", follow_redirects=False)
    assert "level=err" in r.headers["location"]
