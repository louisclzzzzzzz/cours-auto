"""Supports de cours : dépôt dans l'app (contrôle du format), pages et API, envoi dans Drive après coup."""

import io
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import db, pipeline as pl, recorder, subjects, supports
from app.publish import drive as drive_pub
from fakes import make_pdf


def new_rec(sid=None) -> int:
    sid = sid or subjects.create_subject("Graphes")
    return recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")


def add_file(rid, path) -> dict:
    with path.open("rb") as f:
        return supports.add(rid, path.name, f)


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        pl.pipeline.stop()  # aucun traitement réel
        yield c


def test_add_checks_format_and_content(tmp_path):
    rid = new_rec()
    with pytest.raises(supports.SupportError, match="format non pris en charge"):
        supports.add(rid, "cours.key", io.BytesIO(b"xx"))
    with pytest.raises(supports.SupportError, match="pas un fichier PDF valide"):
        supports.add(rid, "cours.pdf", io.BytesIO(b"<html>pas un pdf</html>"))
    with pytest.raises(supports.SupportError, match="vide"):
        supports.add(rid, "cours.pdf", io.BytesIO(b""))
    assert db.list_supports(rid) == [] and not [p for p in supports.supports_dir(rid).iterdir()]
    make_pdf(tmp_path / "x.pdf", [["Bonjour"]])
    sup = add_file(rid, tmp_path / "x.pdf")
    assert supports.file_path(sup).read_bytes().startswith(b"%PDF")
    assert sup["stored_name"].startswith(f"{sup['id']:03d}_") and sup["filename"] == "x.pdf"


def test_detail_page_upload_and_manage(tmp_path, client, monkeypatch):
    deposits = []
    monkeypatch.setattr(drive_pub, "deposit_in_background", lambda rid: deposits.append(rid))
    rid = new_rec()
    page = client.get(f"/enregistrements/{rid}").text
    assert 'id="supports"' in page and "Ajouter un support" in page
    make_pdf(tmp_path / "CM1 Graphes.pdf", [["a"], ["b"]])
    with (tmp_path / "CM1 Graphes.pdf").open("rb") as f:
        r = client.post(f"/enregistrements/{rid}/supports", files=[("files", ("CM1 Graphes.pdf", f, "application/pdf"))],
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#supports") and deposits == [rid]
    [sup] = db.list_supports(rid)
    page = client.get(f"/enregistrements/{rid}").text
    assert "CM1 Graphes.pdf" in page and "déposé dans Drive" not in page
    assert client.get(f"/enregistrements/{rid}/supports/{sup['id']}").content.startswith(b"%PDF")
    db.update_support(sup["id"], drive_url="https://drive.example/s1")
    assert "déposé dans Drive" in client.get(f"/enregistrements/{rid}").text
    # Mauvais format : message d'erreur, rien d'ajouté.
    r = client.post(f"/enregistrements/{rid}/supports", files=[("files", ("cours.key", b"xx", "application/octet-stream"))],
                    follow_redirects=False)
    assert "level=err" in r.headers["location"] and len(db.list_supports(rid)) == 1
    # Retrait : fichier supprimé.
    client.post(f"/enregistrements/{rid}/supports/{sup['id']}/delete")
    assert db.list_supports(rid) == [] and not supports.file_path(sup).exists()
    assert client.get(f"/enregistrements/{rid}/supports/{sup['id']}").status_code == 404


def test_support_added_after_deposit_is_sent_to_drive(monkeypatch):
    sent = []
    monkeypatch.setattr(drive_pub, "is_configured", lambda: True)
    monkeypatch.setattr(drive_pub, "_deposit_safely", lambda rid: sent.append(rid))
    monkeypatch.setattr(drive_pub.threading, "Thread", lambda target, args, name, daemon: type(
        "T", (), {"start": lambda self: target(*args)})())
    rid = new_rec()
    drive_pub.deposit_in_background(rid)
    assert sent == []  # transcription pas encore déposée : le support partira avec elle
    db.update_recording(rid, drive_transcription_id="t1")
    drive_pub.deposit_in_background(rid)
    assert sent == [rid]


def test_recorder_api_adds_support_during_recording(client):
    rid = new_rec()
    pptx = b"PK\x03\x04" + b"\x00" * 64  # en-tête d'archive Office : seul le format est vérifié
    r = client.post(f"/api/recordings/{rid}/supports", files=[("files", ("cours.pptx", pptx, "application/octet-stream"))])
    assert r.status_code == 200 and r.json()["supports"][0]["filename"] == "cours.pptx"
    r = client.post(f"/api/recordings/{rid}/supports", files=[("files", ("notes.txt", b"texte", "text/plain"))])
    assert r.status_code == 400 and "format non pris en charge" in r.json()["detail"]
    assert client.post("/api/recordings/9999/supports", files=[("files", ("a.pdf", b"%PDF", "application/pdf"))]).status_code == 404
    assert 'id="btn-support"' in client.get("/").text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")
def test_import_audio_with_support(tmp_path, client):
    m4a = tmp_path / "cours.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=3", "-c:a", "aac", str(m4a)], check=True)
    make_pdf(tmp_path / "diapos.pdf", [["Graphes"]])
    sid = subjects.create_subject("Réseaux")
    r = client.post("/api/recordings/import", data={"subject_id": str(sid)}, files=[
        ("file", ("cours.m4a", m4a.read_bytes(), "audio/mp4")),
        ("support", ("diapos.pdf", (tmp_path / "diapos.pdf").read_bytes(), "application/pdf")),
        ("support", ("photo.heic", b"xx", "image/heic")),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supports"] == 1 and "photo.heic" in body["warning"]
    [sup] = db.list_supports(body["id"])
    assert sup["filename"] == "diapos.pdf"


def test_deleting_recording_removes_supports(tmp_path):
    rid = new_rec()
    make_pdf(tmp_path / "a.pdf", [["a"]])
    add_file(rid, tmp_path / "a.pdf")
    db.delete_recording(rid)
    assert db.list_supports(rid) == []
