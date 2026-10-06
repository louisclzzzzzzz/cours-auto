// Comportements communs : menus « ⋯ », messages, lignes cliquables, envoi automatique des fichiers choisis.
(() => {
  "use strict";

  // --- Menus déroulants (<details class="menu">) : un seul ouvert, fermeture au clic extérieur et avec Échap.
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.menu[open]").forEach((d) => {
      d.open = false;
      d.querySelector("summary")?.focus();
    });
  });
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement) || !d.matches(".menu") || !d.open) return;
    document.querySelectorAll("details.menu[open]").forEach((o) => { if (o !== d) o.open = false; });
    const panel = d.querySelector(".menu-panel");
    if (panel) {  // panneau aligné à droite du bouton, sauf s'il sortirait de l'écran à gauche
      panel.classList.remove("align-left");
      if (panel.getBoundingClientRect().left < 8) panel.classList.add("align-left");
    }
    d.querySelector(".menu-panel input:not([type=hidden])")?.focus();
  }, true);

  // --- Lignes cliquables : toute la ligne mène au lien (qui reste l'élément accessible au clavier).
  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-href]");
    if (!row || e.button !== 0 || e.defaultPrevented) return;
    if (e.target.closest("a, button, input, select, textarea, label, summary, details, form")) return;
    if (String(window.getSelection?.() || "")) return;  // l'utilisateur sélectionne du texte
    if (e.metaKey || e.ctrlKey) window.open(row.dataset.href, "_blank", "noopener");
    else location.href = row.dataset.href;
  });

  // --- Séance affichée (navigation HTMX sans rechargement de la page).
  document.addEventListener("click", (e) => {
    const link = e.target.closest("[data-session-link]");
    if (!link) return;
    document.querySelectorAll("[data-session-link]").forEach((a) => {
      a.classList.toggle("active", a === link);
      if (a === link) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
  });

  // --- Fichier envoyé dès qu'il est choisi (ex. import d'un .ics).
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (input.matches?.("input[type=file][data-autosubmit]") && input.files.length) input.form.requestSubmit();
  });

  // --- Messages (?msg=…) : bouton fermer, disparition des confirmations, URL nettoyée.
  function initToasts() {
    const url = new URL(location.href);
    if (url.searchParams.has("msg")) {
      url.searchParams.delete("msg");
      url.searchParams.delete("level");
      history.replaceState(history.state, "", url.pathname + url.search + url.hash);
    }
    document.querySelectorAll("[data-toast]").forEach((toast) => {
      toast.querySelector("[data-dismiss]")?.addEventListener("click", () => toast.remove());
      if (!toast.classList.contains("ok")) return;  // les erreurs restent jusqu'à fermeture
      const timer = setTimeout(() => toast.remove(), 8000);
      ["mouseenter", "focusin"].forEach((ev) => toast.addEventListener(ev, () => clearTimeout(timer), { once: true }));
    });
  }

  // --- Ancre vers une section repliée (#intitules, #avance…) : on l'ouvre.
  function openTarget() {
    if (!location.hash) return;
    const el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!el) return;
    if (el instanceof HTMLDetailsElement) el.open = true;
    const parent = el.parentElement?.closest("details");
    if (parent) parent.open = true;
  }

  document.addEventListener("DOMContentLoaded", () => { initToasts(); openTarget(); });
  window.addEventListener("hashchange", openTarget);
})();
