"""Supports de cours : dépôt, lecture (OCR simulé, repli local), texte transmis au LLM, pages et API."""

import io
import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, llm, pipeline as pl, recorder, subjects, supports
from app.markdown_utils import to_notion_markdown
from fakes import make_pdf, make_pptx


def new_rec(sid=None) -> int:
    sid = sid or subjects.create_subject("Graphes")
    return recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")


def add_file(rid, path) -> dict:
    with path.open("rb") as f:
        return supports.add(rid, path.name, f)


@pytest.fixture
def fake_ocr(monkeypatch):
    calls = []

    def ocr(path):
        calls.append(path.name)
        return ["# Graphes pondérés\n\nDéfinition : $G = (V, E, w)$", "Algorithme de **Dijkstra** [figure]"]

    monkeypatch.setattr(supports, "ocr_pages", ocr)
    return calls


@pytest.fixture
def no_ocr(monkeypatch):
    def ocr(path):
        raise RuntimeError("OCR indisponible")

    monkeypatch.setattr(supports, "ocr_pages", ocr)


def wait_support(sid, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        sup = db.get_support(sid)
        if sup["status"] in ("done", "error"):
            return sup
        time.sleep(0.05)
    raise AssertionError("lecture du support non terminée")


def test_clean_ocr_markdown():
    # PPTX/DOCX (conversion) : la ponctuation échappée redevient normale, les formules $…$ sont intactes.
    office = supports.clean_ocr_markdown("Définition : G = \\(V\\, E\\, w\\)\n\n\n\nw : E → ℝ\\+ et $a\\,b$", office=True)
    assert office == "Définition : G = (V, E, w)\n\nw : E → ℝ+ et $a\\,b$"
    # PDF (lu comme une image) : \( … \) délimite une formule → $…$ comme dans le cours ; images signalées.
    pdf = supports.clean_ocr_markdown("On note \\( p_s(x,y) \\).\n\n![img-0.jpeg](img-0.jpeg)", office=False)
    assert pdf == "On note $p_s(x,y)$.\n\n[figure]"
    # Tableaux extraits à part : réinsérés à leur place.
    table = SimpleNamespace(id="tbl-0.md", content="| a | b |\n|---|---|\n| 1 | 2 |")
    assert "| 1 | 2 |" in supports.clean_ocr_markdown("Avant\n\n[tbl-0.md](tbl-0.md)\n\nAprès", office=False, tables=[table])


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
    assert sup["status"] == "pending" and supports.file_path(sup).read_bytes().startswith(b"%PDF")
    assert sup["stored_name"].startswith(f"{sup['id']:03d}_") and sup["filename"] == "x.pdf"


def test_extract_with_ocr(tmp_path, fake_ocr):
    rid = new_rec()
    make_pdf(tmp_path / "CM3 graphes.pdf", [["a"], ["b"]])
    sup = supports.extract(add_file(rid, tmp_path / "CM3 graphes.pdf")["id"])
    assert sup["status"] == "done" and sup["method"] == "ocr" and sup["pages"] == 2
    text = supports.text_path(sup).read_text()
    assert text.startswith("[Page 1]\n# Graphes pondérés") and "[Page 2]\nAlgorithme de **Dijkstra**" in text
    assert supports.extract(sup["id"])["status"] == "done" and fake_ocr == [sup["stored_name"]]  # pas relu


def test_extract_falls_back_to_local_pdf(tmp_path, no_ocr):
    rid = new_rec()
    make_pdf(tmp_path / "cours.pdf", [["Graphes ponderes", "w : E -> R+"], ["Algorithme de Dijkstra"]])
    sup = supports.extract(add_file(rid, tmp_path / "cours.pdf")["id"])
    assert sup["status"] == "done" and sup["method"] == "local" and sup["pages"] == 2
    assert "Algorithme de Dijkstra" in supports.text_path(sup).read_text()


def test_pptx_speaker_notes_are_kept(tmp_path, monkeypatch, no_ocr):
    rid = new_rec()
    make_pptx(tmp_path / "cours.pptx", [("Graphes", ["G = (V, E)"], "Insister sur les graphes orientés."),
                                        ("Dijkstra", ["Poids positifs"], "")])
    sup = supports.extract(add_file(rid, tmp_path / "cours.pptx")["id"])  # OCR indisponible → python-pptx
    text = supports.text_path(sup).read_text()
    assert sup["method"] == "local" and "[Diapositive 2]\nDijkstra" in text
    assert "Notes de l'intervenant : Insister sur les graphes orientés." in text
    # Avec l'OCR, les notes (absentes des diapositives) sont ajoutées aussi.
    monkeypatch.setattr(supports, "ocr_pages", lambda path: ["# Graphes", "# Dijkstra"])
    sup2 = supports.extract(add_file(rid, tmp_path / "cours.pptx")["id"])
    assert sup2["method"] == "ocr" and "# Graphes\n\nNotes de l'intervenant : Insister" in supports.text_path(sup2).read_text()


def test_unreadable_support_is_reported(tmp_path, no_ocr):
    rid = new_rec()
    docx = tmp_path / "cours.docx"
    docx.write_bytes(b"PK\x03\x04" + b"\0" * 100)  # pas de lecture locale pour Word
    sup = supports.extract(add_file(rid, docx)["id"])
    assert sup["status"] == "error" and "OCR indisponible" in sup["error"]
    assert any("illisible" in log["message"] for log in db.list_logs(rid, 10))


def test_documents_and_render_support(tmp_path, fake_ocr):
    rid = new_rec()
    make_pdf(tmp_path / "a.pdf", [["a"]])
    sup = supports.extract(add_file(rid, tmp_path / "a.pdf")["id"])
    [doc] = supports.documents([sup])
    assert doc["name"] == "a.pdf" and [label for label, _ in doc["pages"]] == ["Page 1", "Page 2"]
    assert doc["pages"][1][1] == "Algorithme de **Dijkstra** [figure]"
    # Seulement les pages choisies ; documents trop longs raccourcis.
    only_2 = llm.render_support([doc], {0: {2}})
    assert only_2 == '<support n="1" fichier="a.pdf">\n[Page 2]\nAlgorithme de **Dijkstra** [figure]\n</support>'
    assert llm.render_support([doc], {0: set()}) == ""
    long_doc = {"name": "b.pdf", "pages": [("Page 1", "mot " * 5000)]}
    text = llm.render_support([long_doc, long_doc], max_chars=10_000)
    assert text.count("fin du document non transmise") == 2 and len(text) < 11_000


def test_align_support_parses_and_validates(monkeypatch):
    docs = [{"name": "a.pdf", "pages": [("Page 1", "Titre"), ("Page 2", "Dijkstra"), ("Page 3", "Bellman-Ford")]}]
    monkeypatch.setattr(llm, "complete", lambda messages, **kw: '{"1": {"abordees": [2, "9"], "non_abordees": [3, 2, "x"]}}')
    assert llm.align_support(docs, "transcription", {"matiere": "G", "type": "CM", "numero": 1, "date": "",
                                                      "enseignant": "", "duree": ""}) == {0: {"abordees": {2}, "non_abordees": {3}}}


def test_format_course_uses_spoken_pages_and_lists_the_others(monkeypatch):
    calls = {}

    def fake_complete(messages, **kwargs):
        calls[kwargs["label"]] = messages
        if kwargs["label"] == "Support : pages abordées":
            return '{"1": {"abordees": [2], "non_abordees": [3]}}'
        if kwargs["label"] == "Support : notions non abordées":
            return "## Sur le support, non abordé en cours\n\n- **Bellman-Ford** : poids négatifs, $O(nm)$."
        return "# CM 3 – Graphes pondérés\n\n## Dijkstra\n\nTexte.\n\n## Points clés\n\n- Dijkstra."

    monkeypatch.setattr(llm, "complete", fake_complete)
    meta = {"matiere": "Graphes", "type": "CM", "numero": 3, "date": "lundi 28 septembre 2026", "date_iso": "2026-09-28",
            "enseignant": "DUPONT Jean", "duree": "1 h 30", "intitule_ade": "", "supports": ["CM3.pdf"]}
    data = {"segments": [{"text": "Aujourd'hui l'algorithme de dix castra.", "start": 0, "end": 3, "speaker_id": "s1"}]}
    docs = [{"name": "CM3.pdf", "pages": [("Page 1", "Chapitre 3"), ("Page 2", "Dijkstra : $O(n^2)$"),
                                          ("Page 3", "Bellman-Ford : $O(nm)$")]}]
    md, short = llm.format_course(data, "", meta, support_docs=docs)
    # Repérage : tout le support et la transcription.
    align_user = calls["Support : pages abordées"][1]["content"]
    assert "[Page 3]" in align_user and "dix castra" in align_user
    # Mise en forme : seulement la page abordée, avec les règles du support.
    system, user = (m["content"] for m in calls["Mise en forme du cours"])
    assert "### 9. Support de cours" in system and llm.SUPPORT_REMINDER in user
    assert "[Page 2]" in user and "Bellman" not in user and "[Page 1]" not in user
    assert user.index("# Support de cours") < user.index("# Transcription")
    assert "- Support de cours fourni : CM3.pdf" in user
    # Section finale à partir des pages non abordées, avant les points clés.
    assert "Bellman-Ford : $O(nm)$" in calls["Support : notions non abordées"][1]["content"]
    assert md.index("## Sur le support, non abordé en cours") < md.index("## Points clés")
    assert "— Support : CM3.pdf*" in md.split("\n")[2] and short == "Graphes pondérés"


def test_format_course_without_support_or_when_alignment_fails(monkeypatch):
    calls = []

    def fake_complete(messages, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["label"] == "Support : pages abordées":
            return "pas du JSON"
        return "# CM 1 – Titre\n\nTexte."

    monkeypatch.setattr(llm, "complete", fake_complete)
    meta = {"matiere": "G", "type": "CM", "numero": 1, "date": "", "date_iso": "", "enseignant": "", "duree": "",
            "intitule_ade": ""}
    data = {"segments": [{"text": "Bonjour.", "start": 0, "end": 2, "speaker_id": "s1"}]}
    llm.format_course(data, "", meta)
    assert calls == ["Mise en forme du cours"]  # sans support : un seul appel
    calls.clear()
    docs = [{"name": "a.pdf", "pages": [("Page 1", "Dijkstra")]}]
    llm.format_course(data, "", meta, support_docs=docs)  # repérage illisible : support entier, pas de section finale
    assert calls == ["Support : pages abordées", "Mise en forme du cours"]


def test_pipeline_formatting_reads_and_marks_supports(tmp_path, monkeypatch, fake_ocr):
    received = {}

    def fake_format(data, state, meta, on_progress=None, support_docs=None):
        received.update(docs=support_docs, meta=meta)
        return llm.finalize_course("# Séance\n\n## Contenu\n\nTexte.", meta)

    monkeypatch.setattr(llm, "format_course", fake_format)
    monkeypatch.setattr(llm, "update_state", lambda old, course, meta: old)
    monkeypatch.setattr(llm, "suggest_terms", lambda course, vocab, exclude=(): [])
    rid = new_rec()
    (recorder.recording_dir(rid) / "transcript.json").write_text(json.dumps(
        {"segments": [{"text": "Bonjour.", "start": 0, "end": 2, "speaker_id": "s1"}]}))
    make_pdf(tmp_path / "CM1.pdf", [["a"]])
    sid = add_file(rid, tmp_path / "CM1.pdf")["id"]  # pas encore lu : la mise en forme le lit d'abord
    pl.Pipeline().step_format(rid)
    assert received["docs"] == [{"name": "CM1.pdf", "pages": [
        ("Page 1", "# Graphes pondérés\n\nDéfinition : $G = (V, E, w)$"), ("Page 2", "Algorithme de **Dijkstra** [figure]")]}]
    assert received["meta"]["supports"] == ["CM1.pdf"]
    assert db.get_support(sid)["used_at"] and supports.summary(rid)["unused"] == []
    assert "Support : CM1.pdf" in subjects.read_course(rid)


def test_support_callout_in_notion():
    out = to_notion_markdown("# T\n\n> 📑 **Support** : sur la diapositive 4.")
    assert '<callout icon="📑" color="pink_bg">' in out and "**Support** : sur la diapositive 4." in out


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        pl.pipeline.stop()  # aucun traitement réel
        yield c


def test_detail_page_upload_read_and_manage(tmp_path, client, fake_ocr):
    rid = new_rec()
    page = client.get(f"/enregistrements/{rid}").text
    assert 'id="supports"' in page and "Ajouter un support" in page
    make_pdf(tmp_path / "CM1 Graphes.pdf", [["a"], ["b"]])
    with (tmp_path / "CM1 Graphes.pdf").open("rb") as f:
        r = client.post(f"/enregistrements/{rid}/supports", files=[("files", ("CM1 Graphes.pdf", f, "application/pdf"))],
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#supports")
    sup = wait_support(db.list_supports(rid)[0]["id"])
    assert sup["status"] == "done"
    page = client.get(f"/enregistrements/{rid}").text
    assert "CM1 Graphes.pdf" in page and "2 pages lues" in page and 'hx-trigger="every 2s"' not in page
    assert client.get(f"/enregistrements/{rid}/supports/{sup['id']}").content.startswith(b"%PDF")
    assert "[Page 2]" in client.get(f"/enregistrements/{rid}/supports/{sup['id']}/texte").text
    # Cours déjà rédigé sans ce support : proposition de le refaire.
    subjects.course_path(rid).write_text("# CM 1 – Graphes\n\nTexte.")
    db.update_recording(rid, status="done")
    assert "Refaire le cours avec le support" in client.get(f"/enregistrements/{rid}").text
    # Mauvais format : message d'erreur, rien d'ajouté.
    r = client.post(f"/enregistrements/{rid}/supports", files=[("files", ("cours.key", b"xx", "application/octet-stream"))],
                    follow_redirects=False)
    assert "level=err" in r.headers["location"] and len(db.list_supports(rid)) == 1
    # Retrait : fichier et texte supprimés.
    client.post(f"/enregistrements/{rid}/supports/{sup['id']}/delete")
    assert db.list_supports(rid) == [] and not supports.file_path(sup).exists() and not supports.text_path(sup).exists()
    assert client.get(f"/enregistrements/{rid}/supports/{sup['id']}").status_code == 404


def test_recorder_api_adds_support_during_recording(tmp_path, client, fake_ocr):
    rid = new_rec()
    make_pptx(tmp_path / "cours.pptx", [("Graphes", ["G = (V, E)"], "")])
    r = client.post(f"/api/recordings/{rid}/supports", files=[("files", ("cours.pptx", (tmp_path / "cours.pptx").read_bytes(),
                                                                         "application/octet-stream"))])
    assert r.status_code == 200 and r.json()["supports"][0]["filename"] == "cours.pptx"
    assert wait_support(r.json()["supports"][0]["id"])["status"] == "done"
    r = client.post(f"/api/recordings/{rid}/supports", files=[("files", ("notes.txt", b"texte", "text/plain"))])
    assert r.status_code == 400 and "format non pris en charge" in r.json()["detail"]
    assert client.post("/api/recordings/9999/supports", files=[("files", ("a.pdf", b"%PDF", "application/pdf"))]).status_code == 404
    assert 'id="btn-support"' in client.get("/").text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")
def test_import_audio_with_support(tmp_path, client, fake_ocr):
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
    assert sup["filename"] == "diapos.pdf" and wait_support(sup["id"])["status"] == "done"


def test_deleting_recording_removes_supports(tmp_path, fake_ocr):
    rid = new_rec()
    make_pdf(tmp_path / "a.pdf", [["a"]])
    add_file(rid, tmp_path / "a.pdf")
    db.delete_recording(rid)
    assert db.list_supports(rid) == []
