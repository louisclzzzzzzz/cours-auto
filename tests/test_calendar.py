from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import calendar_ics, db, subjects

PARIS = ZoneInfo("Europe/Paris")


def test_detect_type():
    assert calendar_ics.detect_type("Algorithmique avancée CM") == "CM"
    assert calendar_ics.detect_type("Algo. Av. - TD G1") == "TD"
    assert calendar_ics.detect_type("Réseaux TP2 G2") == "TP"
    assert calendar_ics.detect_type("Cours de réseaux") == "CM"
    assert calendar_ics.detect_type("Travaux dirigés de probabilités") == "TD"
    assert calendar_ics.detect_type("Anglais scientifique") == ""


def test_extract_teacher_from_ade_description():
    desc = "\n\nM1 Informatique\nDUPONT Jean\n(Exporté le:27/09/2026 15:42)\n"
    assert calendar_ics.extract_teacher(desc) == "DUPONT Jean"
    desc = "\n\nM1 Info G1\nLE GALL Marie-Claire\nMARTIN Paul\n"
    assert calendar_ics.extract_teacher(desc) == "LE GALL Marie-Claire, MARTIN Paul"
    assert calendar_ics.extract_teacher("Groupe TP1\nISIMA") == ""


def test_suggest_subject_name():
    assert calendar_ics.suggest_subject_name("Algorithmique avancée CM") == "Algorithmique avancée"
    assert calendar_ics.suggest_subject_name("Algo. Av. - TD G1") == "Algo. Av."
    assert calendar_ics.suggest_subject_name("Réseaux TP2 G2") == "Réseaux"
    assert calendar_ics.suggest_subject_name("**CM/TD Calcul Haute Performance") == "Calcul Haute Performance"
    assert calendar_ics.suggest_subject_name("Prévision de séries (CM/TD/TP)") == "Prévision de séries"
    assert calendar_ics.suggest_subject_name("Langage C (CM)") == "Langage C"


def test_slots_for_day_expands_recurrences_and_timezones(ics_bytes):
    calendar_ics.save_calendar(ics_bytes, "file")
    slots = calendar_ics.slots_for_day(date(2026, 9, 28))
    assert [s.start.strftime("%H:%M") for s in slots] == ["08:00", "10:15", "14:00", "16:15"]
    first = slots[0]
    assert first.summary == "Algorithmique avancée CM"
    assert first.location == "Amphi A"
    assert first.teacher == "DUPONT Jean"
    assert first.course_type == "CM"
    assert first.start.tzinfo is not None and first.start.utcoffset().total_seconds() == 7200
    # L'événement récurrent hebdomadaire apparaît aussi la semaine suivante.
    assert any(s.summary == "Anglais scientifique" for s in calendar_ics.slots_for_day(date(2026, 10, 5)))
    # Les événements « journée entière » sont ignorés.
    assert all(s.summary != "Journée banalisée" for s in calendar_ics.slots_for_day(date(2026, 9, 29)))


def test_mapping_exact_then_regex(ics_bytes):
    calendar_ics.save_calendar(ics_bytes, "file")
    algo = subjects.create_subject("Algorithmique avancée")
    reseaux = subjects.create_subject("Réseaux")
    db.add_mapping("algorithmique   avancée cm", False, algo)  # casse et espaces ignorés
    db.add_mapping(r"^Algo\.? Av", True, algo)
    db.add_mapping(r"R[ée]seaux", True, reseaux)
    slots = calendar_ics.slots_for_day(date(2026, 9, 28))
    by_summary = {s.summary: s.subject_id for s in slots}
    assert by_summary["Algorithmique avancée CM"] == algo
    assert by_summary["Algo. Av. - TD G1"] == algo
    assert by_summary["Réseaux TP2 G2"] == reseaux
    assert by_summary["Anglais scientifique"] is None


def test_pick_current_slot(ics_bytes):
    calendar_ics.save_calendar(ics_bytes, "file")
    slots = calendar_ics.slots_between(date(2026, 9, 28), date(2026, 9, 28))
    at = lambda h, m: datetime(2026, 9, 28, h, m, tzinfo=PARIS)  # noqa: E731
    assert calendar_ics.pick_current(slots, at(9, 0)).summary == "Algorithmique avancée CM"
    assert calendar_ics.pick_current(slots, at(7, 55)).summary == "Algorithmique avancée CM"  # 10 min avant
    assert calendar_ics.pick_current(slots, at(13, 45)).summary == "Réseaux TP2 G2"  # commence dans 15 min
    assert calendar_ics.pick_current(slots, at(12, 30)) is None


def test_invalid_ics_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        calendar_ics.save_calendar(b"<html>pas un calendrier</html>", "file")


def test_refresh_offline_keeps_cache(ics_bytes, monkeypatch):
    calendar_ics.save_calendar(ics_bytes, "file")
    db.set_setting("ics_url", "https://ade.invalid/export.ics")

    def boom(*a, **k):
        raise OSError("réseau indisponible")

    monkeypatch.setattr(calendar_ics.httpx, "get", boom)
    ok, message = calendar_ics.refresh_from_url()
    assert not ok and "conservé" in message
    assert calendar_ics.slots_for_day(date(2026, 9, 28))  # toujours lisible hors ligne
