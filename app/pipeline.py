"""File de tâches (un seul traitement à la fois, dans un thread dédié) et orchestration des étapes :

recording → finalizing → uploaded → transcribing → transcribed → formatting → formatted → publishing → done
(+ error avec l'étape en échec). La publication a un sous-statut par destination (drive, notion).
Chaque étape relit ses entrées sur le disque : elles sont idempotentes et relançables.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime

from . import db, llm, recorder, subjects, supports, transcribe
from .publish import PublishSkipped
from .publish import drive as drive_pub
from .publish import notion as notion_pub

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "recording": "Enregistrement en cours",
    "interrupted": "Interrompu",
    "finalizing": "Finalisation audio…",
    "uploaded": "Audio prêt",
    "transcribing": "Transcription…",
    "transcribed": "Transcrit",
    "formatting": "Mise en forme…",
    "formatted": "Mis en forme",
    "publishing": "Publication…",
    "done": "Terminé",
    "error": "Erreur",
}
SUB_LABELS = {
    "pending": "en attente",
    "running": "en cours…",
    "done": "OK",
    "error": "erreur",
    "skipped": "non configuré",
}
STEP_LABELS = {
    "finalize": "finalisation audio",
    "transcribe": "transcription",
    "format": "mise en forme",
    "state": "état de matière",
    "publish": "publication",
    "publish_drive": "publication Drive",
    "publish_notion": "publication Notion",
    "import_annotations": "import des annotations",
}
CHAIN = ["finalize", "transcribe", "format", "publish"]
BUSY_STATUSES = {"finalizing", "transcribing", "formatting", "publishing"}


@dataclass(frozen=True)
class Job:
    kind: str  # "recording" | "subject"
    target: int
    action: str
    chain: bool = True


class Pipeline:
    def __init__(self) -> None:
        self._queue: queue.Queue[Job] = queue.Queue()
        self._pending: set[tuple] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: dict | None = None

    # --- Cycle de vie -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="pipeline", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)  # type: ignore[arg-type]  # réveille la boucle
        if self._thread:
            self._thread.join(timeout)

    def submit(self, kind: str, target: int, action: str, chain: bool = True) -> bool:
        key = (kind, target, action)
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
        self._queue.put(Job(kind, target, action, chain))
        return True

    def is_active(self, recording_id: int) -> bool:
        with self._lock:
            if self.current and self.current.get("recording_id") == recording_id:
                return True
            return any(k[0] == "recording" and k[1] == recording_id for k in self._pending)

    def queued_count(self) -> int:
        return self._queue.qsize()

    def recover(self) -> None:
        """Au démarrage : reprend les traitements interrompus par un arrêt de l'app."""
        for rec in db.list_recordings_by_status("recording"):
            db.update_recording(rec["id"], status="interrupted")
            db.log(rec["id"], "L'app a redémarré pendant l'enregistrement : marqué interrompu.", "warning")
        db.run("UPDATE recordings SET drive_status = 'pending' WHERE drive_status = 'running'")
        db.run("UPDATE recordings SET notion_status = 'pending' WHERE notion_status = 'running'")
        resume = {
            "finalizing": "finalize", "uploaded": "transcribe", "transcribing": "transcribe",
            "transcribed": "format", "formatting": "format", "formatted": "publish", "publishing": "publish",
        }
        for rec in db.list_recordings_by_status(*resume):
            self.submit("recording", rec["id"], resume[rec["status"]])

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=15)
            except queue.Empty:
                self._periodic()
                continue
            if job is None:  # signal d'arrêt
                continue
            try:
                self._run(job)
            except Exception:  # noqa: BLE001 - le thread ne doit jamais mourir
                log.exception("Erreur inattendue dans la file de traitement")
            finally:
                with self._lock:
                    self._pending.discard((job.kind, job.target, job.action))
                    self.current = None
            self._periodic()

    def _periodic(self) -> None:
        try:
            recorder.mark_stale_interrupted()
        except Exception:  # noqa: BLE001
            log.exception("Vérification des enregistrements interrompus impossible")

    def _progress(self, recording_id: int | None, detail: str) -> None:
        if self.current is not None:
            self.current["detail"] = detail
        db.log(recording_id, detail)

    # --- Exécution ----------------------------------------------------------------------------
    def _run(self, job: Job) -> None:
        if job.kind == "subject":
            self.current = {"subject_id": job.target, "step": job.action, "detail": "", "started": datetime.now()}
            if job.action == "import_annotations":
                self.import_subject_annotations(job.target)
            elif job.action == "publish_drive":
                self._guard_subject(job.target, lambda: drive_pub.publish_subject(job.target))
            return
        rec = db.get_recording(job.target)
        if not rec:
            return
        steps = CHAIN[CHAIN.index(job.action):] if job.chain and job.action in CHAIN else [job.action]
        for step in steps:
            self.current = {"recording_id": job.target, "step": step, "detail": "", "started": datetime.now()}
            if not self._exec_step(job.target, step):
                break

    def _exec_step(self, rid: int, step: str) -> bool:
        handlers = {
            "finalize": self.step_finalize,
            "transcribe": self.step_transcribe,
            "format": self.step_format,
            "state": self.step_state,
            "publish": lambda r: self.step_publish(r, ("drive", "notion")),
            "publish_drive": lambda r: self.step_publish(r, ("drive",)),
            "publish_notion": lambda r: self.step_publish(r, ("notion",)),
            "import_annotations": self.step_import_annotations,
        }
        try:
            handlers[step](rid)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("Étape %s en échec pour l'enregistrement %s", step, rid)
            db.update_recording(rid, status="error", error_step=STEP_LABELS.get(step, step), error_message=str(exc)[:2000])
            db.log(rid, f"Échec ({STEP_LABELS.get(step, step)}) : {exc}\n{traceback.format_exc()[-1500:]}", "error")
            return False

    # --- Étapes -------------------------------------------------------------------------------
    def step_finalize(self, rid: int) -> None:
        rec = db.get_recording(rid)
        db.update_recording(rid, status="finalizing", error_step=None, error_message=None, client_state="stopped",
                            ended_at=rec.get("ended_at") or db.now_iso())
        imported = rec.get("origin") == "import" and not db.chunk_stats(rid)["n"]
        self._progress(rid, "Conversion du fichier importé (ffmpeg)…" if imported else "Assemblage des morceaux audio (ffmpeg)…")
        duration = recorder.finalize_audio(rid)
        db.update_recording(rid, status="uploaded", duration_seconds=duration)
        recorder.write_meta(rid)
        self._progress(rid, f"Audio final prêt ({duration / 60:.1f} min).")

    def step_transcribe(self, rid: int) -> None:
        rec = db.get_recording(rid)
        subject = db.get_subject(rec["subject_id"])
        if not subject:
            raise RuntimeError("Aucune matière associée : choisissez-en une avant de transcrire.")
        audio = recorder.audio_path(rid)
        if not audio.exists():
            raise RuntimeError("Audio final absent : relancez d'abord la finalisation.")
        db.update_recording(rid, status="transcribing", error_step=None, error_message=None)
        self._progress(rid, "Transcription Voxtral en cours…")
        data = transcribe.transcribe_file(audio, subjects.get_vocabulary(subject), rid)
        folder = recorder.recording_dir(rid)
        (folder / "transcript.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        title = f"{subject['name']} — {rec['course_type']} {rec['session_number']} — {rec['session_date']}"
        (folder / "transcript.txt").write_text(transcribe.transcript_text(data, title), encoding="utf-8")
        db.update_recording(rid, status="transcribed")
        n = len(data.get("segments") or [])
        self._progress(rid, f"Transcription terminée ({n} segments).")

    def _state_before(self, rid: int, subject: dict) -> str:
        """État de matière *avant* cette séance, figé au premier passage (rend l'étape rejouable)."""
        path = recorder.recording_dir(rid) / "state_before.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        state = subjects.read_state(subject) or llm.empty_state(subject["name"])
        path.write_text(state, encoding="utf-8")
        return state

    def step_format(self, rid: int) -> None:
        rec = db.get_recording(rid)
        subject = db.get_subject(rec["subject_id"])
        data = transcribe.load_transcript(recorder.recording_dir(rid))
        if data is None:
            raise RuntimeError("Transcription absente : relancez d'abord la transcription.")
        state = self._state_before(rid, subject)
        db.update_recording(rid, status="formatting", error_step=None, error_message=None, state_note=None)
        # Supports de cours (diapositives, PDF) : lus maintenant s'ils ne l'ont pas encore été.
        used = supports.prepare(rid, on_progress=lambda m: self._progress(rid, m))
        self._progress(rid, "Mise en forme du cours (LLM)" + (f" avec {len(used)} support(s) de cours…" if used else "…"))
        meta = llm.session_meta(rec, subject)
        if used:
            meta["supports"] = [s["filename"] for s in used]
        md, short = llm.format_course(data, state, meta, on_progress=lambda m: self._progress(rid, m),
                                      support_docs=supports.documents(used) if used else None)
        if subjects.course_path(rid).exists():
            subjects.archive_versions(rid)  # l'ancienne version (et l'éventuelle version annotée) est conservée
        subjects.course_path(rid).write_text(md, encoding="utf-8")
        db.update_recording(rid, title=short, annotations_imported_at=None)
        supports.mark_used([s["id"] for s in used])
        recorder.write_meta(rid)
        self._progress(rid, f"Cours rédigé : « {short} ».")
        self._update_state_and_vocabulary(rid, strict=False)
        db.update_recording(rid, status="formatted")

    def step_state(self, rid: int) -> None:
        """Relance seule de la mise à jour de l'état (appel LLM n°2) et des suggestions de vocabulaire."""
        self._update_state_and_vocabulary(rid, strict=True)
        rec = db.get_recording(rid)
        if rec["status"] == "error" and rec.get("error_step") == STEP_LABELS["state"]:
            self._settle(rid)  # revient à « terminé », « mis en forme » ou à l'erreur de publication éventuelle

    def _update_state_and_vocabulary(self, rid: int, strict: bool) -> None:
        rec = db.get_recording(rid)
        subject = db.get_subject(rec["subject_id"])
        course = subjects.read_course(rid)
        if course is None:
            raise RuntimeError("Cours absent : relancez d'abord la mise en forme.")
        meta = llm.session_meta(rec, subject)
        try:
            self._progress(rid, "Mise à jour de l'état de la matière (LLM)…")
            new_state = llm.update_state(self._state_before(rid, subject), course, meta)
            (recorder.recording_dir(rid) / "state_after.md").write_text(new_state, encoding="utf-8")
            if self._owns_state(rec, subject):
                subjects.write_state(subject, new_state)
                db.update_subject(subject["id"], state_recording_id=rid)
                db.update_recording(rid, state_note=None)
            else:
                db.update_recording(rid, state_note=(
                    "L'état de la matière n'a pas été remplacé : une séance plus récente l'a déjà mis à jour. "
                    "L'état calculé pour cette séance est dans state_after.md ; ajustez l'état à la main si besoin."))
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            db.update_recording(rid, state_note=f"Échec de la mise à jour de l'état : {exc}")
            db.log(rid, f"Mise à jour de l'état en échec (non bloquant) : {exc}", "warning")
        try:
            teachers = [t for t in f"{subject.get('teachers') or ''},{rec.get('teacher') or ''}".split(",") if t.strip()]
            terms = llm.suggest_terms(course, subjects.get_vocabulary(subject), exclude=teachers)
            if terms:
                subjects.add_proposed_terms(subject["id"], terms)
                self._progress(rid, f"{len(terms)} terme(s) de vocabulaire proposé(s) (à valider dans Matières).")
        except Exception as exc:  # noqa: BLE001
            db.log(rid, f"Suggestions de vocabulaire indisponibles : {exc}", "warning")

    @staticmethod
    def _owns_state(rec: dict, subject: dict) -> bool:
        """La séance peut écrire l'état si aucune séance plus récente ne l'a déjà mis à jour."""
        owner_id = subject.get("state_recording_id")
        if owner_id in (None, rec["id"]):
            return True
        owner = db.get_recording(owner_id)
        if not owner:
            return True
        key = lambda r: (r["session_date"] or "", r.get("event_start") or r["started_at"] or "", r["id"])  # noqa: E731
        return key(rec) >= key(owner)

    def step_publish(self, rid: int, destinations: tuple[str, ...]) -> None:
        if subjects.read_course(rid) is None:
            raise RuntimeError("Aucun cours mis en forme : relancez d'abord la mise en forme.")
        db.update_recording(rid, status="publishing", error_step=None, error_message=None)
        for dest in destinations:
            db.update_recording(rid, **{f"{dest}_status": "running", f"{dest}_error": None})
            self._progress(rid, f"Publication {dest.capitalize()}…")
            try:
                if dest == "drive":
                    drive_pub.publish_recording(rid)
                    try:  # les liens Drive peuvent maintenant être reportés dans Notion
                        if db.get_recording(rid).get("notion_page_id"):
                            notion_pub.update_links(rid)
                    except Exception as exc:  # noqa: BLE001
                        db.log(rid, f"Liens Drive non reportés dans Notion : {exc}", "warning")
                else:
                    notion_pub.publish_recording(rid)
                db.update_recording(rid, **{f"{dest}_status": "done"})
                self._progress(rid, f"Publication {dest.capitalize()} terminée.")
            except PublishSkipped as exc:
                db.update_recording(rid, **{f"{dest}_status": "skipped", f"{dest}_error": str(exc)})
                db.log(rid, str(exc), "warning")
            except Exception as exc:  # noqa: BLE001 - l'échec d'une destination ne bloque pas l'autre
                log.exception("Publication %s en échec", dest)
                db.update_recording(rid, **{f"{dest}_status": "error", f"{dest}_error": str(exc)[:1500]})
                db.log(rid, f"Publication {dest} en échec : {exc}", "error")
        self._settle(rid)

    @staticmethod
    def _settle(rid: int) -> None:
        rec = db.get_recording(rid)
        errors = [
            f"{name} : {rec[f'{key}_error'] or 'erreur'}"
            for key, name in (("drive", "Drive"), ("notion", "Notion"))
            if rec[f"{key}_status"] == "error"
        ]
        if errors:
            db.update_recording(rid, status="error", error_step="publication", error_message=" | ".join(errors))
        elif all(rec[f"{k}_status"] in ("done", "skipped") for k in ("drive", "notion")):
            db.update_recording(rid, status="done", error_step=None, error_message=None)
        else:
            db.update_recording(rid, status="formatted")

    def step_import_annotations(self, rid: int) -> None:
        self._progress(rid, "Import des annotations Notion…")
        notion_pub.import_annotations(rid)
        self._progress(rid, "Annotations importées (course_annote.md).")
        if drive_pub.is_configured():
            self.step_publish(rid, ("drive",))

    def import_subject_annotations(self, subject_id: int) -> None:
        recs = [r for r in db.list_recordings(subject_id) if r.get("notion_page_id")]
        for rec in recs:
            self.current = {"recording_id": rec["id"], "step": "import_annotations", "detail": "", "started": datetime.now()}
            try:
                notion_pub.import_annotations(rec["id"])
                db.log(rec["id"], "Annotations importées (import par matière).")
            except Exception as exc:  # noqa: BLE001
                db.log(rec["id"], f"Import des annotations en échec : {exc}", "error")
        if drive_pub.is_configured():
            for rec in recs:
                self.current = {"recording_id": rec["id"], "step": "publish_drive", "detail": "", "started": datetime.now()}
                self.step_publish(rec["id"], ("drive",))

    def _guard_subject(self, subject_id: int, fn) -> None:
        try:
            fn()
        except PublishSkipped as exc:
            log.info("Publication de la matière %s ignorée : %s", subject_id, exc)
        except Exception:  # noqa: BLE001
            log.exception("Publication de la matière %s en échec", subject_id)

    def describe(self) -> str:
        cur = self.current
        if not cur:
            return ""
        label = STEP_LABELS.get(cur.get("step", ""), cur.get("step", ""))
        return f"{label} — {cur['detail']}" if cur.get("detail") else label


pipeline = Pipeline()
