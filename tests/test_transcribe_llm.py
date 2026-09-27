import json

import pytest

from app import llm, transcribe

# Extrait réel d'une réponse Voxtral (diarisation + horodatage), cf. tests manuels.
RESPONSE = {
    "model": "voxtral-mini-latest",
    "text": "…",
    "segments": [
        {"text": " Bonjour à tous.", "start": 0.1, "end": 1.1, "speaker_id": "speaker_1"},
        {"text": " Un graphe_pondéré est un graphe muni de poids.", "start": 1.3, "end": 5.4, "speaker_id": "speaker_1"},
        {"text": " Monsieur, ça tombe à l'examen ?", "start": 9.0, "end": 10.5, "speaker_id": "speaker_2"},
        {"text": " Oui.", "start": 11.0, "end": 11.4, "speaker_id": "speaker_1"},
    ],
    "_context_bias_restore": {"graphe_pondéré": "graphe pondéré"},
}


def test_normalize_bias_terms():
    terms = ["Dijkstra", "graphe pondéré", "Bellman, Ford", "dijkstra", "  "] + [f"t{i}" for i in range(200)]
    out = transcribe.normalize_bias_terms(terms)
    assert out[:3] == ["Dijkstra", "graphe_pondéré", "Bellman_Ford"]
    assert len(out) == 100 and all(" " not in t and "," not in t for t in out)
    assert transcribe._originals(["graphe pondéré", "Graphe  pondéré"]) == ["graphe pondéré"]


def test_paragraphs_speakers_and_restored_terms():
    paragraphs = transcribe.build_paragraphs(RESPONSE)
    assert [p["speaker"] for p in paragraphs] == ["speaker_1", "speaker_2", "speaker_1"]
    assert "graphe pondéré" in paragraphs[0]["text"] and "_" not in paragraphs[0]["text"]
    assert paragraphs[1]["gap_before"] == pytest.approx(3.6)
    text = transcribe.transcript_text(RESPONSE, "Graphes — CM 1")
    assert "L1 = locuteur principal (probablement l'enseignant" in text
    assert "[00:00:09] L2 : Monsieur, ça tombe à l'examen ?" in text
    assert transcribe.restore_terms("Le Graphe_pondéré.", {"graphe_pondéré": "graphe pondéré"}) == "Le Graphe pondéré."


def test_transcribe_retries_without_language(monkeypatch, tmp_path):
    from mistralai.client.errors import SDKError
    import httpx

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"fake")
    calls = []

    def fake_call(path, **kwargs):
        calls.append(kwargs)
        if "language" in kwargs:
            raise SDKError("bad", httpx.Response(400, text='{"message":"language is not compatible with timestamp_granularities"}'))
        class R:
            def model_dump(self, mode=None):
                return {"text": "ok", "segments": []}
        return R()

    monkeypatch.setattr(transcribe, "_call", fake_call)
    data = transcribe.transcribe_file(audio, ["graphe pondéré"])
    assert data["_context_bias_restore"] == {"graphe_pondéré": "graphe pondéré"}
    assert calls[0]["language"] == "fr" and calls[0]["context_bias"] == ["graphe_pondéré"]
    assert "language" not in calls[1] and calls[1]["diarize"] is True


def test_chunk_transcript_cuts_on_pauses():
    paragraphs, lines = [], []
    for i in range(40):
        paragraphs.append({"gap_before": 5.0 if i % 10 == 0 else 0.5})
        lines.append(f"[{i}] " + "mot " * 50)
    chunks = llm.chunk_transcript(paragraphs, lines, max_chars=3000)
    assert len(chunks) >= 3
    assert sum(len(c) for c in chunks) == 40
    assert all(sum(len(l) + 2 for l in c) <= 3000 * 1.15 for c in chunks)


def test_remove_final_sections():
    md = "## 1. A\n\ntexte\n\n## Points clés\n\n- x\n\n### sous\n\n## À retenir pour la suite\n\n- y\n\n## 2. B\n\nz"
    assert llm.remove_sections(md, llm.FINAL_SECTIONS) == "## 1. A\n\ntexte\n\n## 2. B\n\nz"


META = {"matiere": "Graphes", "type": "CM", "numero": 3, "date": "lundi 29 septembre 2026",
        "date_iso": "2026-09-29", "enseignant": "DURAND Sophie", "duree": "1 h 50 min", "intitule_ade": ""}


@pytest.mark.parametrize("raw_title", ["# Graphes pondérés", "# CM3 : Graphes pondérés", "# CM 3 – Graphes pondérés", "# cm 3 - Graphes pondérés"])
def test_finalize_course_normalizes_title(raw_title):
    md, short = llm.finalize_course(f"```markdown\n{raw_title}\n\n## 1. Intro\n\nSoit \\(x\\).\n```", META)
    assert short == "Graphes pondérés"
    assert md.startswith("# CM 3 – Graphes pondérés\n\n*Graphes — CM 3 du lundi 29 septembre 2026 — Enseignant : DURAND Sophie*\n")
    assert "Soit $x$." in md


def test_format_course_single_and_multi_chunk(monkeypatch):
    prompts = []

    def fake_complete(messages, **kwargs):
        prompts.append(messages[-1]["content"])
        label = kwargs.get("label", "")
        if label == "Points clés":
            return "## Points clés\n\n- tout"
        if "partie 1/" in label or label == "Mise en forme du cours":
            return "# Titre de séance\n\n## 1. Début\n\nContenu.\n\n## Points clés\n\n- à retirer si multi"
        return "# Titre répété\n\n## 2. Suite\n\nContenu 2."

    monkeypatch.setattr(llm, "complete", fake_complete)
    md, short = llm.format_course(RESPONSE, "état", META)
    assert short == "Titre de séance" and "## Points clés" in md and len(prompts) == 1
    assert "# État de la matière" in prompts[0] and "Rappel : tout ce que l'enseignant n'a pas dit" in prompts[0]

    prompts.clear()
    big = {"segments": [{"text": "phrase " * 400, "start": i * 60.0, "end": i * 60.0 + 55, "speaker_id": "speaker_1"} for i in range(6)]}
    from app import db
    db.set_setting("llm_chunk_chars", "6000")
    md, short = llm.format_course(big, "état", META)
    assert len(prompts) >= 3  # plusieurs parties + points clés
    assert md.count("# ") >= 1 and "Titre répété" not in md
    assert md.count("## Points clés") == 1 and "à retirer si multi" not in md
    assert "Il se termine ainsi" in prompts[1]


class _FakeChat:
    """Faux `client.chat` : refuse reasoning_effort comme le fait mistral-large, puis répond."""

    def __init__(self, reject_reasoning: bool):
        self.reject = reject_reasoning
        self.calls = []

    def complete(self, **kwargs):
        import httpx
        from mistralai.client.errors import SDKError

        self.calls.append(kwargs)
        if self.reject and "reasoning_effort" in kwargs:
            raise SDKError("bad", httpx.Response(400, text='{"message":"reasoning_effort is not enabled for this model"}'))
        from types import SimpleNamespace

        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="OK"))])


@pytest.mark.parametrize("reject", [False, True])
def test_complete_reasoning_effort_and_fallback(monkeypatch, reject):
    from types import SimpleNamespace

    chat = _FakeChat(reject)
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(chat=chat))
    assert llm.complete([{"role": "user", "content": "x"}]) == "OK"
    first = chat.calls[0]
    assert first["model"] == "mistral-small-2603" and first["reasoning_effort"] == "high"
    assert first["max_tokens"] == 32000 + llm.REASONING_BUDGET
    if reject:
        assert len(chat.calls) == 2 and "reasoning_effort" not in chat.calls[1]
    else:
        assert len(chat.calls) == 1


def test_complete_retries_when_reasoning_exhausts_budget(monkeypatch):
    """Réponse vide car le budget s'est épuisé pendant la réflexion : relance avec un budget doublé."""
    from types import SimpleNamespace as NS

    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        thinking = NS(type="thinking", thinking=[NS(type="text", text="je réfléchis…")])
        if len(calls) == 1:
            return NS(choices=[NS(finish_reason="length", message=NS(content=[thinking]))])
        return NS(choices=[NS(finish_reason="stop", message=NS(content=[thinking, NS(type="text", text="Réponse")]))])

    monkeypatch.setattr(llm, "get_client", lambda: NS(chat=NS(complete=fake_complete)))
    assert llm.complete([{"role": "user", "content": "x"}], max_tokens=1500) == "Réponse"
    assert [c["max_tokens"] for c in calls] == [1500 + llm.REASONING_BUDGET, 2 * (1500 + llm.REASONING_BUDGET)]
    assert all("prefix" not in m for c in calls for m in c["messages"])


def test_suggest_terms_filters(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: json.dumps({"termes": [
        "Dijkstra", "d⁻(v)", "N=|V|", "Kruskal", "DURAND Sophie", "file de priorité", "Bellman-Ford"]}))
    course = "L'algorithme de Dijkstra utilise une file de priorité ; Bellman Ford gère les poids négatifs. DURAND Sophie."
    assert llm.suggest_terms(course, ["bellman-ford"], exclude=["DURAND Sophie"]) == ["Dijkstra", "file de priorité"]
