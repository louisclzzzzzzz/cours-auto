import pytest

from app import config, transcribe

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




def test_missing_key_is_reported(monkeypatch):
    monkeypatch.setattr(config, "mistral_api_key", lambda: "")
    with pytest.raises(transcribe.TranscriptionError, match="MISTRAL_API_KEY"):
        transcribe.get_client()
    ok, message, models = transcribe.test_api_key()
    assert not ok and "MISTRAL_API_KEY" in message and models == []
