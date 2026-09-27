// Rendu Markdown (marked) + formules LaTeX (KaTeX) : `$...$` en ligne, `$$...$$` en bloc.
// Les formules sont extraites avant marked (sinon `_` et `*` y seraient pris pour de l'emphase),
// puis réinjectées rendues par KaTeX.
(() => {
  "use strict";

  const PH = (i) => `KATEXPLACEHOLDER${i}X`;

  function protect(src) {
    const maths = [];
    const codes = [];
    // 1) Mettre de côté le code (blocs et en ligne) pour ne pas y chercher de formules.
    let text = src.replace(/(^|\n)(```|~~~)[^\n]*\n[\s\S]*?\n\2[^\n]*(?=\n|$)/g, (m) => { codes.push(m); return `CODEPLACEHOLDER${codes.length - 1}X`; });
    text = text.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (m) => { codes.push(m); return `CODEPLACEHOLDER${codes.length - 1}X`; });
    // 2) Formules en bloc puis en ligne.
    text = text.replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => { maths.push({ tex: tex.trim(), display: true }); return `\n\n${PH(maths.length - 1)}\n\n`; });
    text = text.replace(/(^|[^\\$])\$(?!\s)((?:\\.|[^$\\\n])+?)(?<!\s)\$(?!\d)/g, (m, pre, tex) => { maths.push({ tex, display: false }); return pre + PH(maths.length - 1); });
    // 3) Restaurer le code.
    text = text.replace(/CODEPLACEHOLDER(\d+)X/g, (m, i) => codes[Number(i)]);
    return { text, maths };
  }

  function renderMath(m) {
    try {
      return window.katex.renderToString(m.tex, { displayMode: m.display, throwOnError: false, strict: "ignore", output: "html" });
    } catch (err) {
      return `<code>${m.tex.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))}</code>`;
    }
  }

  function renderMarkdown(src) {
    if (!window.marked || !window.katex) return null;
    const { text, maths } = protect(src || "");
    let html = window.marked.parse(text, { gfm: true, breaks: false });
    html = html.replace(/<p>\s*KATEXPLACEHOLDER(\d+)X\s*<\/p>/g, (m, i) => renderMath(maths[Number(i)]));
    html = html.replace(/KATEXPLACEHOLDER(\d+)X/g, (m, i) => renderMath(maths[Number(i)]));
    return html;
  }

  function renderAll(root) {
    (root || document).querySelectorAll("[data-md-render]").forEach((el) => {
      const src = el.previousElementSibling;
      if (!src || !src.classList.contains("md-source")) return;
      const html = renderMarkdown(src.value);
      if (html !== null) el.innerHTML = html;
    });
  }

  window.renderMarkdown = renderMarkdown;
  window.renderAllMarkdown = renderAll;
  document.addEventListener("DOMContentLoaded", () => renderAll());
  document.addEventListener("htmx:afterSwap", (e) => renderAll(e.detail.target));
})();
