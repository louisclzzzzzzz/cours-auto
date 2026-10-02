"""Enregistrement : morceaux, segments (rechargement de page), morceau tronqué, finalisation ffmpeg, API."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, recorder, subjects

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")


def make_webm(path: Path, seconds: float, freq: int = 440) -> bytes:
    """Fichier WebM/Opus comparable à ce que produit MediaRecorder."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}", "-c:a", "libopus", "-b:a", "64k", "-f", "webm", str(path)],
        check=True,
    )
    return path.read_bytes()


def split(data: bytes, n: int) -> list[bytes]:
    size = len(data) // n + 1
    return [data[i:i + size] for i in range(0, len(data), size)]


def new_recording() -> int:
    sid = subjects.create_subject("Graphes")
    return recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28",
                                     mime_type="audio/webm;codecs=opus")


def test_two_segments_and_truncated_chunk(tmp_path):
    rid = new_recording()
    seg1 = recorder.start_segment(rid, "audio/webm")
    assert seg1 == {"segment": 1, "next_seq": 1, "elapsed": 0.0}
    seq = 1
    for part in split(make_webm(tmp_path / "a.webm", 20), 5):
        recorder.save_chunk(rid, seq, 1, part, "webm", elapsed=seq * 4.0)
        seq += 1
    # Rechargement de la page : nouvelle instance de MediaRecorder = nouveau segment.
    seg2 = recorder.start_segment(rid, "audio/webm")
    assert seg2["segment"] == 2 and seg2["next_seq"] == seq and seg2["elapsed"] == pytest.approx(20.0)
    parts = split(make_webm(tmp_path / "b.webm", 10, 660), 4)
    parts[-1] = parts[-1][: len(parts[-1]) // 2]  # dernier morceau tronqué (onglet fermé en plein envoi)
    for part in parts:
        recorder.save_chunk(rid, seq, 2, part, "webm", elapsed=None)
        seq += 1
    assert (recorder.recording_dir(rid) / "chunk_0001.webm").exists()
    duration = recorder.finalize_audio(rid)
    assert 26 < duration < 31
    assert recorder.audio_path(rid).exists()
    assert not (recorder.recording_dir(rid) / "_finalize").exists()


def test_chunk_rejected_once_stopped(tmp_path):
    rid = new_recording()
    recorder.start_segment(rid)
    db.update_recording(rid, status="finalizing")
    with pytest.raises(recorder.RecorderError):
        recorder.save_chunk(rid, 1, 1, b"abc", "webm", None)


def test_stale_recording_marked_interrupted_and_revived_by_chunk(tmp_path):
    rid = new_recording()
    recorder.start_segment(rid)
    db.update_recording(rid, last_seen_at="2026-01-01T10:00:00+01:00")
    assert recorder.mark_stale_interrupted() == 1
    assert db.get_recording(rid)["status"] == "interrupted"
    data = make_webm(tmp_path / "c.webm", 3)
    recorder.save_chunk(rid, 1, 1, data, "webm", 3.0)
    assert db.get_recording(rid)["status"] == "recording"


def test_teacher_learned_by_subject():
    sid = subjects.create_subject("Réseaux")
    recorder.create_recording(subject_id=sid, course_type="TD", session_date="2026-09-28", teacher="MARTIN Paul")
    recorder.create_recording(subject_id=sid, course_type="TD", session_date="2026-10-05", teacher="MARTIN Paul")
    assert db.get_subject(sid)["teachers"] == "MARTIN Paul"
    # Numérotation par type : TD 1 puis TD 2.
    assert [r["session_number"] for r in db.list_recordings(sid)] == [2, 1]


def wait_status(rid: int, done: set[str], timeout: float = 30.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        rec = db.get_recording(rid)
        if rec["status"] in done:
            return rec
        time.sleep(0.2)
    raise AssertionError(f"statut final non atteint : {db.get_recording(rid)['status']}")


def wait_audio_ready(rid: int, timeout: float = 30.0) -> dict:
    """Audio préparé et file libérée : l'enregistrement attend que l'utilisateur lance le traitement."""
    from app.pipeline import pipeline

    wait_status(rid, {"uploaded", "error"}, timeout)
    t0 = time.time()
    while pipeline.is_active(rid) and time.time() - t0 < timeout:
        time.sleep(0.05)
    return db.get_recording(rid)


def test_api_recording_survives_reload(tmp_path):
    """Critère de la phase 3 : un enregistrement survit à un rechargement de page en plein milieu."""
    from app.main import app

    with TestClient(app) as client:
        sid = subjects.create_subject("Algorithmique")
        res = client.post("/api/recordings", json={
            "subject_id": sid, "course_type": "CM", "session_date": "2026-09-28", "mime_type": "audio/webm;codecs=opus",
            "event": {"uid": "x", "summary": "Algo CM", "start": "2026-09-28T08:00:00+02:00",
                      "end": "2026-09-28T10:00:00+02:00", "location": "Amphi A"},
        })
        assert res.status_code == 200, res.text
        rid, seq = res.json()["id"], res.json()["next_seq"]
        for part in split(make_webm(tmp_path / "1.webm", 12), 3):
            r = client.put(f"/api/recordings/{rid}/chunks/{seq}?segment=1&ext=webm&elapsed={seq * 4}", content=part)
            assert r.status_code == 200, r.text
            seq += 1
        # La page est rechargée : l'onglet redemande un segment et continue.
        active = client.get("/api/recordings/active").json()["recordings"]
        assert [a["id"] for a in active] == [rid]
        seg = client.post(f"/api/recordings/{rid}/segments", json={"mime_type": "audio/webm"}).json()
        assert seg["segment"] == 2 and seg["next_seq"] == seq
        for part in split(make_webm(tmp_path / "2.webm", 8, 550), 2):
            assert client.put(f"/api/recordings/{rid}/chunks/{seq}?segment=2&ext=webm", content=part).status_code == 200
            seq += 1
        assert client.post(f"/api/recordings/{rid}/heartbeat", json={"state": "paused", "elapsed": 20}).status_code == 200
        assert client.post(f"/api/recordings/{rid}/stop", json={"elapsed": 20.0}).json()["status"] == "finalizing"
        # Finalisation OK, puis le traitement attend d'être lancé à la main.
        rec = wait_audio_ready(rid)
        assert rec["status"] == "uploaded", rec["error_message"]
        assert not (recorder.recording_dir(rid) / "transcript.json").exists()
        assert "Lancer le traitement" in client.get("/").text
        r = client.post(f"/enregistrements/{rid}/action", data={"action": "transcribe", "back": "/"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/?msg=Traitement+lanc")
        # La transcription échoue proprement (pas de clé API dans les tests).
        rec = wait_status(rid, {"error", "done"})
        assert rec["error_step"] == "transcription"
        assert "MISTRAL_API_KEY" in rec["error_message"]
        assert 18 < rec["duration_seconds"] < 21
        assert recorder.audio_path(rid).exists()
        # Les morceaux arrivés après l'arrêt sont refusés.
        assert client.put(f"/api/recordings/{rid}/chunks/{seq}?segment=2", content=b"xx").status_code == 409
        # Pages HTML
        assert client.get(f"/enregistrements/{rid}").status_code == 200
        assert client.get("/enregistrements").status_code == 200


def test_api_import_audio_file(tmp_path):
    """Import d'un fichier audio (ex. m4a de téléphone) : converti puis traité comme un enregistrement."""
    from app.main import app

    m4a = tmp_path / "cours du lundi.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=10", "-ac", "2", "-ar", "44100", "-c:a", "aac", str(m4a)], check=True)
    with TestClient(app) as client:
        sid = subjects.create_subject("Réseaux")
        with m4a.open("rb") as f:
            r = client.post("/api/recordings/import", files={"file": (m4a.name, f, "audio/mp4")}, data={
                "subject_id": str(sid), "course_type": "TD", "session_date": "2026-09-22",
                "event": '{"summary": "Réseaux TD G1", "start": "2026-09-22T10:00:00+02:00"}'})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        assert 9 < r.json()["duration"] < 11
        rec = wait_audio_ready(rid)
        assert rec["origin"] == "import" and rec["source_filename"] == "cours du lundi.m4a"
        assert rec["course_type"] == "TD" and rec["session_date"] == "2026-09-22"
        assert rec["event_summary"] == "Réseaux TD G1"
        assert rec["status"] == "uploaded", rec["error_message"]  # conversion OK, traitement à lancer à la main
        assert 9 < rec["duration_seconds"] < 11
        assert recorder.source_path(rid).name == "source.m4a"
        assert recorder.audio_path(rid).exists()
        assert "📁 importé" in client.get("/enregistrements").text

        # Fichier sans piste audio : refusé, et rien ne reste en base.
        bad = tmp_path / "notes.txt"
        bad.write_text("pas de son ici")
        with bad.open("rb") as f:
            r = client.post("/api/recordings/import", files={"file": (bad.name, f, "text/plain")},
                            data={"subject_id": str(sid)})
        assert r.status_code == 400 and "piste audio" in r.json()["detail"]
        assert [x["id"] for x in db.list_recordings()] == [rid]
        # Sans matière choisie : refusé.
        with m4a.open("rb") as f:
            assert client.post("/api/recordings/import", files={"file": (m4a.name, f, "audio/mp4")}).status_code == 400


def test_api_rejects_foreign_origin():
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/recordings", json={}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
