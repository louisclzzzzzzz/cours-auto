"""File de tâches (un seul traitement à la fois, dans un thread dédié) et orchestration des étapes :

recording → finalizing → uploaded → transcribing → transcribed → publishing → done
(+ error avec l'étape en échec). La publication dépose la transcription et les supports dans Drive ; le cours
est ensuite rédigé par une tâche Claude planifiée, puis récupéré depuis Drive (voir publish/drive.py).
Chaque étape relit ses entrées sur le disque : elles sont idempotentes et relançables.

À l'arrêt (ou à l'import), seul l'audio est préparé : l'enregistrement attend ensuite en « uploaded »
que l'utilisateur lance le traitement (transcription → dépôt Drive).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db, recorder, retry, subjects, transcribe
from .publish import PublishSkipped
from .publish import drive as drive_pub

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "recording": "Enregistrement en cours",
    "interrupted": "Interrompu",
    "finalizing": "Finalisation audio…",
    "uploaded": "Prêt à traiter",
    "transcribing": "Transcription…",
    "transcribed": "Transcrit",
    "publishing": "Dépôt dans Drive…",
    "done": "Terminé",
    "error": "Erreur",
}
SUB_LABELS = {
    "pending": "en attente",
    "running": "en cours…",
    "done": "OK",
    "error": "erreur",
    "skipped": "ignoré",
}
STEP_LABELS = {
    "finalize": "finalisation audio",
    "transcribe": "transcription",
    "publish": "publication",
}
STEP_ACTIONS = {label: step for step, label in STEP_LABELS.items()}  # libellé d'étape en échec → étape
CHAIN = ["transcribe", "publish"]  # la finalisation de l'audio n'enchaîne pas : traitement lancé à la main
BUSY_STATUSES = {"finalizing", "transcribing", "publishing"}
# Étapes des versions précédentes (mise en forme par Mistral, Notion) : la séance reprend au dépôt Drive.
LEGACY_STEPS = {"mise en forme", "état de matière", "publication Notion", "import des annotations"}

# Transcription : si Mistral est momentanément indisponible (5xx, 429, réseau), l'étape est relancée toute
# seule plus tard (5, 10, 20, 40 min puis toutes les heures, environ 17 h au total).
AUTO_RETRY_STEPS = {"transcribe"}
AUTO_RETRY_MAX = 20


def auto_retry_delay(attempt: int) -> timedelta:
    return timedelta(minutes=min(5 * 2 ** (attempt - 1), 60))


def unavailable_message(exc: BaseException) -> str:
    status = retry.status_of(exc)
    if status == 429:
        return "Mistral limite temporairement les requêtes (erreur 429)."
    if status:
        return f"Le service de transcription de Mistral est momentanément indisponible (erreur {status})."
    return "Connexion à Mistral impossible (réseau coupé ou délai dépassé)."


@dataclass(frozen=True)
class Job:
    kind: str  # "recording"
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
        """Au démarrage : reprend les traitements interrompus par un arrêt de l'app. Un enregistrement dont
        l'audio est prêt (« uploaded ») attend toujours que l'utilisateur lance le traitement."""
        for rec in db.list_recordings_by_status("recording"):
            db.update_recording(rec["id"], status="interrupted")
            db.log(rec["id"], "L'app a redémarré pendant l'enregistrement : marqué interrompu.", "warning")
        db.run("UPDATE recordings SET drive_status = 'pending' WHERE drive_status = 'running'")
        # Séances arrêtées à une étape qui n'existe plus (mise en forme, Notion) : leur transcription est déposée
        # (ou l'est déjà) ; sans transcription, l'audio attend un nouveau lancement du traitement.
        for rec in db.list_recordings_by_status("formatting", "formatted", "error"):
            step = rec.get("error_step")
            if rec["status"] != "error" or step in LEGACY_STEPS:
                fields = ({"status": "transcribed"} if (recorder.recording_dir(rec["id"]) / "transcript.txt").exists()
                          else {"status": "done", "drive_status": "done"} if rec.get("drive_transcription_id")
                          else {"status": "uploaded"})
                db.update_recording(rec["id"], **fields, error_step=None, error_message=None,
                                    auto_retry_at=None, auto_retry_count=0)
            elif step in ("publication", "publication Drive"):  # l'échec venait peut-être de Notion seul
                if rec["drive_status"] in ("done", "skipped"):
                    db.update_recording(rec["id"], status="done", error_step=None, error_message=None)
                else:
                    db.update_recording(rec["id"], error_step="publication")
        resume = {"finalizing": "finalize", "transcribing": "transcribe", "transcribed": "publish", "publishing": "publish"}
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
        try:
            self.submit_due_retries()
        except Exception:  # noqa: BLE001
            log.exception("Essais automatiques impossibles")
        try:
            drive_pub.fetch_courses_if_due()  # cours rédigés par la tâche Claude
        except Exception:  # noqa: BLE001
            log.exception("Récupération des cours depuis Drive impossible")

    def submit_due_retries(self) -> list[int]:
        """Relance les étapes en échec dont l'essai automatique est arrivé à échéance."""
        now = datetime.now().astimezone()
        submitted = []
        for rec in db.q("SELECT id, error_step, auto_retry_at FROM recordings "
                        "WHERE status = 'error' AND auto_retry_at IS NOT NULL"):
            try:
                due = datetime.fromisoformat(rec["auto_retry_at"]) <= now
            except ValueError:
                due = True
            step = STEP_ACTIONS.get(rec["error_step"] or "")
            if not due or step not in AUTO_RETRY_STEPS:
                continue
            db.update_recording(rec["id"], auto_retry_at=None)
            db.log(rec["id"], f"Essai automatique : relance de l'étape « {rec['error_step']} ».")
            if self.submit("recording", rec["id"], step):
                submitted.append(rec["id"])
        return submitted

    def _schedule_auto_retry(self, rid: int, step: str, exc: BaseException) -> str | None:
        """Mistral momentanément indisponible : programme un nouvel essai et renvoie le message d'erreur à afficher."""
        if step not in AUTO_RETRY_STEPS or not retry.is_retryable(exc):
            return None
        attempt = ((db.get_recording(rid) or {}).get("auto_retry_count") or 0) + 1
        message = unavailable_message(exc)
        if attempt > AUTO_RETRY_MAX:
            db.update_recording(rid, auto_retry_at=None)
            return message + " Les essais automatiques sont arrêtés : cliquez sur « Réessayer » quand le service sera rétabli."
        when = datetime.now().astimezone() + auto_retry_delay(attempt)
        db.update_recording(rid, auto_retry_at=when.isoformat(timespec="seconds"), auto_retry_count=attempt)
        db.log(rid, f"Nouvel essai automatique vers {when:%H:%M} ({attempt}/{AUTO_RETRY_MAX}).", "warning")
        return message + f" Nouvel essai automatique vers {when:%H:%M}, ou cliquez sur « Réessayer »."

    def _progress(self, recording_id: int | None, detail: str) -> None:
        if self.current is not None:
            self.current["detail"] = detail
        db.log(recording_id, detail)

    # --- Exécution ----------------------------------------------------------------------------
    def _run(self, job: Job) -> None:
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
            "publish": self.step_publish,
        }
        try:
            handlers[step](rid)
        except Exception as exc:  # noqa: BLE001
            log.exception("Étape %s en échec pour l'enregistrement %s", step, rid)
            message = self._schedule_auto_retry(rid, step, exc) or str(exc)
            db.update_recording(rid, status="error", error_step=STEP_LABELS.get(step, step), error_message=message[:2000])
            db.log(rid, f"Échec ({STEP_LABELS.get(step, step)}) : {exc}\n{traceback.format_exc()[-1500:]}", "error")
            return False
        if step in AUTO_RETRY_STEPS:
            db.update_recording(rid, auto_retry_at=None, auto_retry_count=0)
        return True

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
        self._progress(rid, f"Audio final prêt ({duration / 60:.1f} min) : traitement à lancer.")

    def step_transcribe(self, rid: int) -> None:
        rec = db.get_recording(rid)
        subject = db.get_subject(rec["subject_id"])
        if not subject:
            raise RuntimeError("Aucune matière associée : choisissez-en une avant de transcrire.")
        audio = recorder.audio_path(rid)
        if not audio.exists():
            raise RuntimeError("Audio final absent : relancez d'abord la finalisation.")
        db.update_recording(rid, status="transcribing", error_step=None, error_message=None)
        if rec.get("auto_retry_count"):
            self._progress(rid, "Mistral était indisponible : vérification qu'il répond de nouveau…")
            try:
                transcribe.probe()
            except Exception as exc:  # noqa: BLE001
                if retry.is_retryable(exc):
                    raise  # toujours indisponible : nouvel essai programmé, sans avoir renvoyé tout l'audio
        self._progress(rid, "Transcription Voxtral en cours…")
        data = transcribe.transcribe_file(audio, subjects.get_vocabulary(subject), rid)
        folder = recorder.recording_dir(rid)
        (folder / "transcript.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        title = f"{subject['name']} — {rec['course_type']} {rec['session_number']} — {rec['session_date']}"
        (folder / "transcript.txt").write_text(transcribe.transcript_text(data, title), encoding="utf-8")
        db.update_recording(rid, status="transcribed")
        n = len(data.get("segments") or [])
        self._progress(rid, f"Transcription terminée ({n} segments).")

    def step_publish(self, rid: int) -> None:
        """Dépose la transcription (et les supports) dans Drive, où la tâche Claude rédige le cours."""
        if not (recorder.recording_dir(rid) / "transcript.txt").exists():
            raise RuntimeError("Transcription absente : relancez d'abord la transcription.")
        db.update_recording(rid, status="publishing", error_step=None, error_message=None,
                            drive_status="running", drive_error=None)
        self._progress(rid, "Dépôt de la transcription dans Drive…")
        try:
            drive_pub.publish_recording(rid)
        except PublishSkipped as exc:
            db.update_recording(rid, status="done", drive_status="skipped", drive_error=str(exc))
            db.log(rid, str(exc), "warning")
            return
        except Exception as exc:
            db.update_recording(rid, drive_status="error", drive_error=str(exc)[:1500])
            raise
        db.update_recording(rid, status="done", drive_status="done")
        self._progress(rid, "Transcription déposée dans Drive : la tâche Claude rédigera le cours.")

    def describe(self) -> str:
        cur = self.current
        if not cur:
            return ""
        label = STEP_LABELS.get(cur.get("step", ""), cur.get("step", ""))
        return f"{label} — {cur['detail']}" if cur.get("detail") else label


pipeline = Pipeline()
