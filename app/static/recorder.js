// Enregistreur : MediaRecorder avec timeslice, envoi immédiat de chaque morceau au serveur,
// pause / reprise, vumètre, choix du micro, Wake Lock et reprise après rechargement de la page.
(() => {
  "use strict";

  const TIMESLICE_MS = 30000;   // un morceau toutes les 30 s
  const HEARTBEAT_MS = 20000;   // signal de vie (utile pendant les pauses)
  const AUDIO_BPS = 64000;

  const $ = (id) => document.getElementById(id);
  const ui = {
    panel: $("recorder"), chrono: $("chrono"), status: $("rec-status"), device: $("device"),
    vu: $("vu-bar"), summary: $("selection-summary"), start: $("btn-start"), pause: $("btn-pause"),
    resume: $("btn-resume"), stop: $("btn-stop"), test: $("btn-test-mic"),
  };

  const st = {
    mode: "idle",            // idle | recording | paused | finishing
    recId: null, segment: null, nextSeq: 1,
    recorder: null, stream: null, mime: "", ext: "webm",
    elapsedBase: 0, runStart: null,
    queue: [], uploading: false, lastUploadOk: null, uploadErrors: 0,
    wakeLock: null, audioCtx: null, vuRaf: null, hbTimer: null, chronoTimer: null, previewStream: null,
    unloading: false, importing: false,
  };

  // --- Utilitaires ----------------------------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (s) => { s = Math.max(0, Math.floor(s)); return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`; };
  const elapsed = () => st.elapsedBase + (st.runStart ? (performance.now() - st.runStart) / 1000 : 0);
  const setStatus = (text) => { ui.status.textContent = text; };

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Erreur ${res.status}`);
    return data;
  }

  function pickMime() {
    if (!window.MediaRecorder) return "";
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4"];
    return candidates.find((m) => MediaRecorder.isTypeSupported(m)) || "";
  }
  const extFor = (mime) => (mime.startsWith("audio/ogg") ? "ogg" : mime.includes("mp4") ? "m4a" : "webm");

  function setMode(mode) {
    st.mode = mode;
    const busy = mode === "recording" || mode === "paused";
    ui.start.disabled = mode !== "idle";
    ui.pause.disabled = mode !== "recording";
    ui.resume.disabled = mode !== "paused";
    ui.stop.disabled = !busy;
    ui.device.disabled = mode !== "idle";
    $("btn-import").disabled = mode !== "idle" || st.importing;
    if (st.importing) ui.start.disabled = true;
    ui.panel.classList.toggle("is-recording", mode === "recording");
    ui.panel.classList.toggle("is-paused", mode === "paused");
    document.querySelectorAll("[data-resume],[data-finalize]").forEach((b) => { b.disabled = mode !== "idle"; });
  }

  // --- Sélection du cours -----------------------------------------------------------------------
  function currentSelection() {
    const radio = document.querySelector('input[name="slot"]:checked');
    if (!radio) return { error: "Choisissez d'abord le cours à enregistrer." };
    if (radio.value === "manual") {
      const subj = $("manual-subject").value;
      const newName = $("manual-new-subject").value.trim();
      if (!subj || (subj === "new" && !newName)) return { error: "Choisissez (ou créez) la matière du cours hors EDT." };
      return {
        label: `${subj === "new" ? newName : $("manual-subject").selectedOptions[0].text} — ${$("manual-type").value} (hors EDT)`,
        payload: {
          subject_id: subj === "new" ? null : Number(subj),
          subject_name: subj === "new" ? newName : null,
          course_type: $("manual-type").value,
          teacher: $("manual-teacher").value.trim(),
          session_date: $("manual-date").value,
          event: null,
        },
      };
    }
    const d = radio.dataset;
    const card = radio.closest("[data-slot-card]");
    const type = card.querySelector(".slot-type").value;
    if (!d.subjectId) return { error: "Associez d'abord cet intitulé ADE à une matière (formulaire sous le créneau)." };
    return {
      label: `${d.subjectName} — ${type} · ${d.summary}`,
      payload: {
        subject_id: Number(d.subjectId), course_type: type, teacher: d.teacher, session_date: d.date,
        event: { uid: d.uid, summary: d.summary, start: d.start, end: d.end, location: d.location },
      },
    };
  }

  function refreshSelection() {
    document.querySelectorAll("[data-slot-card]").forEach((c) => {
      const r = c.querySelector('input[name="slot"]');
      c.classList.toggle("selected", !!(r && r.checked));
    });
    if (st.mode !== "idle") return;
    const sel = currentSelection();
    ui.summary.textContent = sel.error ? sel.error : `Cours sélectionné : ${sel.label}`;
  }

  document.addEventListener("change", (e) => {
    if (e.target.matches('input[name="slot"], .slot-type, #manual-subject, #manual-type')) refreshSelection();
    if (e.target.id === "manual-subject") $("manual-new-subject").classList.toggle("hidden", e.target.value !== "new");
    if (e.target.name === "match") {
      e.target.form.querySelector('[name="pattern"]').classList.toggle("hidden", e.target.value !== "regex");
    }
    if (e.target.name === "subject_id" && e.target.form?.classList.contains("quick-map")) {
      e.target.form.querySelector('[name="new_name"]').classList.toggle("hidden", e.target.value !== "new");
    }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "manual-new-subject") refreshSelection(); });
  document.addEventListener("click", (e) => {
    const card = e.target.closest("[data-slot-card]");
    if (card && st.mode === "idle" && !e.target.closest("form, select, input, button, a")) {
      const r = card.querySelector('input[name="slot"]');
      if (r) { r.checked = true; refreshSelection(); }
    }
  });
  document.body.addEventListener("htmx:afterSwap", (e) => { if (e.detail.target.id === "slots") refreshSelection(); });

  // --- Micro : périphériques et vumètre ---------------------------------------------------------
  async function listDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    const saved = localStorage.getItem("micDeviceId") || "";
    ui.device.innerHTML = "";
    const def = new Option("Micro par défaut", "");
    ui.device.add(def);
    devices.forEach((d, i) => { if (d.deviceId && d.deviceId !== "default") ui.device.add(new Option(d.label || `Micro ${i + 1}`, d.deviceId)); });
    if ([...ui.device.options].some((o) => o.value === saved)) ui.device.value = saved;
  }

  async function getStream() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Ce navigateur ne permet pas l'accès au micro (utilisez http://127.0.0.1).");
    const id = ui.device.value;
    return navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: id ? { exact: id } : undefined,
        channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true,
      },
    });
  }

  function startVu(stream) {
    stopVu();
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const data = new Float32Array(analyser.fftSize);
      const loop = () => {
        analyser.getFloatTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += v * v;
        const db = 20 * Math.log10(Math.sqrt(sum / data.length) || 1e-8);
        ui.vu.style.width = `${Math.max(0, Math.min(100, ((db + 60) / 60) * 100))}%`;
        st.vuRaf = requestAnimationFrame(loop);
      };
      loop();
      st.audioCtx = ctx;
    } catch (err) { console.warn("Vumètre indisponible", err); }
  }

  function stopVu() {
    if (st.vuRaf) cancelAnimationFrame(st.vuRaf);
    st.vuRaf = null;
    if (st.audioCtx) st.audioCtx.close().catch(() => {});
    st.audioCtx = null;
    ui.vu.style.width = "0";
  }

  function stopPreview() {
    if (st.previewStream) st.previewStream.getTracks().forEach((t) => t.stop());
    st.previewStream = null;
  }

  ui.test.addEventListener("click", async () => {
    if (st.mode !== "idle") return;
    try {
      stopPreview();
      st.previewStream = await getStream();
      await listDevices();
      startVu(st.previewStream);
      setStatus("Micro actif (test) : parlez pour vérifier le niveau.");
    } catch (err) { setStatus(`Micro inaccessible : ${err.message}`); }
  });
  ui.device.addEventListener("change", () => {
    localStorage.setItem("micDeviceId", ui.device.value);
    if (st.previewStream) ui.test.click();
  });

  // --- Wake Lock, signal de vie, chronomètre ----------------------------------------------------
  async function acquireWakeLock() {
    try {
      if ("wakeLock" in navigator && !st.wakeLock) {
        st.wakeLock = await navigator.wakeLock.request("screen");
        st.wakeLock.addEventListener("release", () => { st.wakeLock = null; });
      }
    } catch (err) { console.warn("Wake Lock refusé", err); }
  }
  function releaseWakeLock() { if (st.wakeLock) st.wakeLock.release().catch(() => {}); st.wakeLock = null; }

  async function heartbeat() {
    if (!st.recId || (st.mode !== "recording" && st.mode !== "paused")) return;
    try { await api("POST", `/api/recordings/${st.recId}/heartbeat`, { state: st.mode, elapsed: elapsed() }); } catch (err) { /* hors ligne : on réessaiera */ }
  }

  function startTimers() {
    stopTimers();
    st.hbTimer = setInterval(heartbeat, HEARTBEAT_MS);
    st.chronoTimer = setInterval(() => { ui.chrono.textContent = fmt(elapsed()); updateUploadStatus(); }, 500);
  }
  function stopTimers() { clearInterval(st.hbTimer); clearInterval(st.chronoTimer); st.hbTimer = st.chronoTimer = null; }

  // --- Envoi des morceaux -------------------------------------------------------------------------
  function chunkUrl(it) {
    return `/api/recordings/${it.recId}/chunks/${it.seq}?segment=${it.segment}&ext=${it.ext}&elapsed=${it.elapsed.toFixed(1)}`;
  }

  function enqueue(blob) {
    if (!blob || blob.size === 0 || !st.recId) return;
    const item = { recId: st.recId, seq: st.nextSeq++, segment: st.segment, ext: st.ext, blob, elapsed: elapsed() };
    if (st.unloading && st.queue.length === 0 && !st.uploading && blob.size < 60000) {
      // Page en cours de fermeture : seule une requête « keepalive » (≤ 64 Ko) peut encore aboutir.
      fetch(chunkUrl(item), { method: "PUT", body: blob, keepalive: true }).catch(() => {});
      return;
    }
    st.queue.push(item);
    pump();
  }

  async function pump() {
    if (st.uploading) return;
    st.uploading = true;
    while (st.queue.length) {
      const it = st.queue[0];
      try {
        const res = await fetch(chunkUrl(it), { method: "PUT", body: it.blob, headers: { "Content-Type": "application/octet-stream" } });
        if (res.status === 409 || res.status === 404) {
          const data = await res.json().catch(() => ({}));
          st.queue.shift();
          setStatus(`Morceau refusé par le serveur : ${data.detail || res.status}`);
          continue;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        st.queue.shift();
        st.lastUploadOk = Date.now();
        st.uploadErrors = 0;
      } catch (err) {
        st.uploadErrors += 1;
        updateUploadStatus();
        await sleep(Math.min(3000 * st.uploadErrors, 15000));
      }
    }
    st.uploading = false;
    updateUploadStatus();
  }

  function updateUploadStatus() {
    if (st.mode === "idle") return;
    const pending = st.queue.length;
    const last = st.lastUploadOk ? `dernier envoi il y a ${Math.round((Date.now() - st.lastUploadOk) / 1000)} s` : "premier envoi dans moins de 30 s";
    const prefix = st.mode === "paused" ? "En pause" : st.mode === "finishing" ? "Finalisation" : "Enregistrement en cours";
    let txt = `${prefix} — ${last}`;
    if (pending) txt += ` — ${pending} morceau(x) en attente d'envoi`;
    if (st.uploadErrors) txt += " — ⚠️ serveur injoignable, nouvel essai…";
    setStatus(txt);
  }

  async function waitQueueEmpty(timeoutMs = 120000) {
    const t0 = Date.now();
    while ((st.queue.length || st.uploading) && Date.now() - t0 < timeoutMs) await sleep(250);
    return st.queue.length === 0;
  }

  // --- Démarrage / reprise / arrêt ---------------------------------------------------------------
  function begin(stream, mime, seg) {
    st.stream = stream;
    st.mime = mime;
    st.ext = extFor(mime);
    st.segment = seg.segment;
    st.nextSeq = seg.next_seq;
    st.elapsedBase = seg.elapsed || 0;
    st.runStart = performance.now();
    st.lastUploadOk = null;
    const options = { audioBitsPerSecond: AUDIO_BPS };
    if (mime) options.mimeType = mime;
    const rec = new MediaRecorder(stream, options);
    rec.ondataavailable = (e) => enqueue(e.data);
    rec.onerror = (e) => setStatus(`Erreur de l'enregistreur : ${e.error?.message || e}`);
    // Arrêt non demandé (micro débranché, périphérique coupé…) : on tente de repartir automatiquement.
    rec.addEventListener("stop", () => { if (st.recorder === rec && st.mode !== "finishing") recoverFromUnexpectedStop(); });
    rec.start(TIMESLICE_MS);
    st.recorder = rec;
    startVu(stream);
    acquireWakeLock();
    startTimers();
    setMode("recording");
    heartbeat();
    updateUploadStatus();
  }

  async function recoverFromUnexpectedStop() {
    const id = st.recId;
    st.elapsedBase = elapsed();
    st.runStart = null;
    if (st.stream) st.stream.getTracks().forEach((t) => t.stop());
    stopVu();
    stopTimers();
    st.recorder = null;
    setMode("idle");
    setStatus("⚠️ L'enregistrement s'est arrêté tout seul (micro déconnecté ?) : envoi des données reçues puis reprise…");
    await waitQueueEmpty();
    ui.device.value = "";  // le micro choisi a peut-être disparu : on repart sur le micro par défaut
    try {
      await resumeExisting(id);
      setStatus("Enregistrement repris automatiquement sur le micro par défaut (nouveau segment).");
    } catch (err) {
      releaseWakeLock();
      setStatus(`⚠️ Reprise impossible (${err.message}). Les données sont conservées : rechargez la page puis « Reprendre l'enregistrement ».`);
    }
  }

  async function startNew() {
    const sel = currentSelection();
    if (sel.error) { alert(sel.error); return; }
    if (!window.MediaRecorder) { alert("Ce navigateur ne prend pas en charge MediaRecorder."); return; }
    stopPreview();
    let stream;
    try { stream = await getStream(); } catch (err) { alert(`Micro inaccessible : ${err.message}`); return; }
    await listDevices();
    const mime = pickMime();
    try {
      const res = await api("POST", "/api/recordings", { ...sel.payload, mime_type: mime });
      st.recId = res.id;
      ui.summary.textContent = `Enregistrement #${res.id} : ${sel.label}`;
      begin(stream, mime, res);
      document.body.dispatchEvent(new Event("refresh-latest"));
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      alert(`Impossible de démarrer : ${err.message}`);
    }
  }

  // Reprend un enregistrement existant dans un nouveau segment. Lève une erreur en cas d'échec.
  async function resumeExisting(id) {
    if (st.mode !== "idle") return;
    stopPreview();
    const stream = await getStream();
    const mime = pickMime();
    try {
      const seg = await api("POST", `/api/recordings/${id}/segments`, { mime_type: mime });
      st.recId = Number(id);
      ui.summary.textContent = `Reprise de l'enregistrement #${id} (segment ${seg.segment}).`;
      document.querySelectorAll(`[data-active-recording="${id}"]`).forEach((el) => el.classList.add("hidden"));
      begin(stream, mime, seg);
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      throw err;
    }
  }

  async function resumeFromBanner(id) {
    try { await resumeExisting(id); } catch (err) { alert(`Reprise impossible : ${err.message}`); }
  }

  function pause() {
    if (st.mode !== "recording") return;
    try { st.recorder.requestData(); } catch (err) { /* ignore */ }
    st.recorder.pause();
    st.elapsedBase = elapsed();
    st.runStart = null;
    setMode("paused");
    heartbeat();
    updateUploadStatus();
  }

  function resume() {
    if (st.mode !== "paused") return;
    st.recorder.resume();
    st.runStart = performance.now();
    setMode("recording");
    heartbeat();
  }

  async function stop() {
    if (st.mode !== "recording" && st.mode !== "paused") return;
    if (!confirm("Arrêter l'enregistrement et lancer le traitement (transcription, mise en forme, publication) ?")) return;
    const rec = st.recorder;
    const stopped = new Promise((resolve) => rec.addEventListener("stop", resolve, { once: true }));
    st.elapsedBase = elapsed();
    st.runStart = null;
    setMode("finishing");
    rec.stop();
    await stopped;  // le dernier morceau est émis avant l'événement « stop »
    st.stream.getTracks().forEach((t) => t.stop());
    stopVu();
    releaseWakeLock();
    stopTimers();
    ui.chrono.textContent = fmt(st.elapsedBase);
    setStatus("Envoi des derniers morceaux…");
    const flushed = await waitQueueEmpty();
    if (!flushed) setStatus("⚠️ Certains morceaux n'ont pas pu être envoyés : l'enregistrement sera finalisé avec ce qui a été reçu.");
    try {
      const res = await api("POST", `/api/recordings/${st.recId}/stop`, { elapsed: st.elapsedBase });
      setStatus(res.ok ? `Enregistrement #${st.recId} terminé : traitement en cours (voir ci-dessous).` : "Aucun audio reçu : enregistrement en erreur.");
    } catch (err) {
      setStatus(`Arrêt non confirmé par le serveur (${err.message}). Utilisez « Finaliser » dans Enregistrements.`);
    }
    st.recId = null;
    st.recorder = null;
    st.stream = null;
    setMode("idle");
    refreshSelection();
    document.body.dispatchEvent(new Event("refresh-latest"));
  }

  async function finalizeExisting(id) {
    if (!confirm("Finaliser cet enregistrement avec les morceaux déjà reçus et lancer le traitement ?")) return;
    try { await api("POST", `/api/recordings/${id}/stop`, {}); location.reload(); } catch (err) { alert(err.message); }
  }

  // --- Import d'un fichier audio -------------------------------------------------------------------
  const imp = { file: $("import-file"), button: $("btn-import"), progress: $("import-progress"), status: $("import-status") };

  function importAudio() {
    if (st.mode !== "idle" || st.importing) return;
    const file = imp.file.files[0];
    if (!file) { alert("Choisissez d'abord un fichier audio."); return; }
    const sel = currentSelection();
    if (sel.error) { alert(sel.error); return; }
    const form = new FormData();
    form.append("file", file);
    const p = sel.payload;
    ["subject_id", "subject_name", "course_type", "teacher", "session_date"].forEach((k) => {
      if (p[k] !== null && p[k] !== undefined) form.append(k, p[k]);
    });
    if (p.event) form.append("event", JSON.stringify(p.event));

    st.importing = true;
    imp.button.disabled = ui.start.disabled = true;
    imp.progress.classList.remove("hidden");
    imp.progress.value = 0;
    imp.status.textContent = `Envoi de « ${file.name} »…`;
    const xhr = new XMLHttpRequest();  // (fetch ne donne pas la progression de l'envoi)
    xhr.open("POST", "/api/recordings/import");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      imp.progress.value = (100 * e.loaded) / e.total;
      imp.status.textContent = `Envoi de « ${file.name} »… ${Math.round(imp.progress.value)} %`;
    };
    const done = (message) => {
      st.importing = false;
      imp.button.disabled = false;
      setMode(st.mode);
      imp.progress.classList.add("hidden");
      imp.status.textContent = message;
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (err) { /* réponse non JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        imp.file.value = "";
        done(`✅ « ${file.name} » importé (enregistrement #${data.id}, ${fmt(data.duration || 0)}) : traitement en cours ci-dessous.`
          + (data.warning ? ` ⚠️ ${data.warning}` : ""));
        document.body.dispatchEvent(new Event("refresh-latest"));
      } else {
        done(`❌ Import refusé : ${data.detail || `erreur ${xhr.status}`}`);
      }
    };
    xhr.onerror = () => done("❌ Échec de l'envoi (serveur injoignable ?).");
    xhr.send(form);
  }

  imp.button.addEventListener("click", importAudio);

  ui.start.addEventListener("click", startNew);
  ui.pause.addEventListener("click", pause);
  ui.resume.addEventListener("click", resume);
  ui.stop.addEventListener("click", stop);
  document.querySelectorAll("[data-resume]").forEach((b) => b.addEventListener("click", () => resumeFromBanner(b.dataset.resume)));
  document.querySelectorAll("[data-finalize]").forEach((b) => b.addEventListener("click", () => finalizeExisting(b.dataset.finalize)));

  // --- Robustesse : fermeture, veille, changement d'onglet ------------------------------------------
  window.addEventListener("beforeunload", (e) => {
    if (st.mode === "recording" || st.mode === "paused" || st.mode === "finishing" || st.queue.length || st.importing) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (st.mode !== "recording") return;
    if (document.visibilityState === "hidden") {
      try { st.recorder.requestData(); } catch (err) { /* ignore */ }  // envoie tout de suite ce qui est en mémoire
    } else {
      acquireWakeLock();  // le verrou est relâché quand l'onglet est masqué
    }
  });
  window.addEventListener("pagehide", () => {
    st.unloading = true;
    if (st.mode === "recording") { try { st.recorder.requestData(); } catch (err) { /* ignore */ } }
  });
  window.addEventListener("pageshow", () => { st.unloading = false; });
  window.addEventListener("online", pump);

  setMode("idle");
  refreshSelection();
  listDevices().catch(() => {});
})();
