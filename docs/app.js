"use strict";
/*
 * app.js — LLM PDF Annotator, 100% client-side build for GitHub Pages.
 * Renders with pdf.js, OCRs with Tesseract.js, annotates with pdf-lib.
 * No backend — the PDF never leaves the browser.
 *
 * By Paul Fishwick and Claude Code.
 */

pdfjsLib.GlobalWorkerOptions.workerSrc = "vendor/pdf.worker.min.js";

// Tesseract.js (OCR) is loaded only when a scanned page needs it, so the
// page never blocks on it and native-text PDFs work without it.
let tesseractLoading = null;
function ensureTesseract() {
  if (window.Tesseract) return Promise.resolve();
  if (tesseractLoading) return tesseractLoading;
  tesseractLoading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "vendor/tesseract.min.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("could not load Tesseract.js"));
    document.head.appendChild(s);
  });
  return tesseractLoading;
}

// ---- State ----------------------------------------------------------------
let pdfjsDoc = null;       // pdf.js document (rendering + native text)
let pdfLibDoc = null;      // pdf-lib document (accumulates real annotations)
let docName = "";
let pageCount = 0;
let currentPage = 1;
let zoom = 1.5;
let mode = "copypaste";    // "copypaste" | "api"
let turnIndex = 0;
let annotationCount = 0;
const wordCache = {};      // pageIdx (0-based) -> words[]
const annotations = [];    // { page, tool, color, quads, rect, comment }
const history = [];        // chat messages

const cycleColors = ["yellow", "green", "blue", "pink", "orange", "red"];
const TOOL_LABELS = {
  "highlight": "Highlight", "underline": "Underline",
  "strikeout": "Strikethrough", "squiggly": "Squiggly underline",
  "sticky-note": "Sticky note (comment)", "text-box": "Text box",
  "callout": "Callout box", "rectangle": "Rectangle",
  "oval": "Oval", "arrow": "Arrow",
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

// ---- Prompts (mirror the server build) ------------------------------------
const PAGE_PROMPT = `You are helping a user analyse a PDF document.
You are given the text of ONE page (the page the user is viewing).
Answer the user's question about it.

Reply ONLY with a JSON object, no other text, in this exact form:
{
  "answer": "<your full answer to the user>",
  "links": [
    {"point": "<short label for the answer>",
     "quote": "<a short VERBATIM passage copied exactly from the page text>"}
  ]
}
Include 0 or 1 link. The quote must be copied character-for-character from the page text so it can be located. Keep it under 25 words.`;

const DOC_PROMPT = `You are analysing a complete PDF document. The context contains the WHOLE document text, with [PAGE n] markers before each page (or the PDF is attached to this chat). Answer the user's request (for example, a summary).

Reply ONLY with a JSON object, no other text, in this exact form:
{
  "answer": "<your full answer / summary>",
  "links": [
    {"point": "<one key point of your answer, one sentence>",
     "quote": "<a short VERBATIM passage copied exactly from the document that supports this point>",
     "page": <the page number where that passage appears>}
  ]
}
Provide one link per key point (typically 4-8 links). Every quote must be copied character-for-character from the document text and be under 25 words, so it can be located and annotated.`;

// ===========================================================================
// Init
// ===========================================================================
window.addEventListener("DOMContentLoaded", () => {
  Annotator.TOOLS.forEach((t) => {
    const o = document.createElement("option");
    o.value = t;
    o.textContent = TOOL_LABELS[t] || t;
    $("toolSelect").appendChild(o);
  });
  cycleColors.forEach((c) => {
    const dot = el("span", "swatch");
    dot.style.background = Annotator.CSS[c] || c;
    dot.title = c;
    $("colorLegend").appendChild(dot);
  });

  const dz = $("dropZone");
  $("pickBtn").onclick = () => $("fileInput").click();
  $("fileInput").onchange = (e) => {
    if (e.target.files[0]) loadPdf(e.target.files[0]);
  };
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault(); dz.classList.add("over");
    }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault(); dz.classList.remove("over");
    }));
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) loadPdf(f);
  });

  $("prevBtn").onclick = () => gotoPage(currentPage - 1);
  $("nextBtn").onclick = () => gotoPage(currentPage + 1);
  $("zoomIn").onclick = () => setZoom(zoom + 0.25);
  $("zoomOut").onclick = () => setZoom(zoom - 0.25);

  $("scopeSelect").onchange = () => { updateChatPlaceholder(); updateModeUI(); };

  mode = localStorage.getItem("annot_mode") || "copypaste";
  $("modeSelect").value = mode;
  $("apiKey").value = localStorage.getItem("annot_key") || "";
  updateModeUI();
  $("modeSelect").onchange = () => {
    mode = $("modeSelect").value;
    localStorage.setItem("annot_mode", mode);
    updateModeUI();
  };
  $("apiKey").onchange = () =>
    localStorage.setItem("annot_key", $("apiKey").value.trim());

  $("sendBtn").onclick = sendChat;
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  $("exportBtn").onclick = exportPdf;
});

function updateModeUI() {
  const cp = mode === "copypaste";
  $("modelRow").classList.toggle("hidden", cp);
  $("apiKeyRow").classList.toggle("hidden", cp);
  $("fullTextRow").classList.toggle("hidden",
    !cp || $("scopeSelect").value !== "document");
}

function updateChatPlaceholder() {
  if (!pdfjsDoc) return;
  $("chatInput").placeholder = $("scopeSelect").value === "document"
    ? "Ask the LLM a question about the whole document..."
    : `Ask the LLM about page ${currentPage}...`;
}

function updateAnnCount() {
  $("annCount").textContent = annotationCount
    ? `${annotationCount} annotation${annotationCount === 1 ? "" : "s"}`
    : "";
}

function status(msg) { $("docName").textContent = msg; }

// ===========================================================================
// Load + render
// ===========================================================================
async function loadPdf(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    alert("Please choose a PDF file."); return;
  }
  status(`Loading ${file.name}…`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  try {
    pdfjsDoc = await pdfjsLib.getDocument({ data: bytes.slice() }).promise;
    pdfLibDoc = await PDFLib.PDFDocument.load(bytes.slice(), {
      ignoreEncryption: true,
    });
  } catch (e) {
    status("Could not open PDF: " + e.message);
    return;
  }
  docName = file.name;
  pageCount = pdfjsDoc.numPages;
  currentPage = 1;
  zoom = 1.5;
  turnIndex = 0;
  annotationCount = 0;
  history.length = 0;
  for (const k in wordCache) delete wordCache[k];
  annotations.length = 0;

  updateAnnCount();
  $("chat").replaceChildren();
  $("dropZone").classList.add("hidden");
  $("pageArea").classList.remove("hidden");
  $("exportBtn").disabled = false;
  status(`${docName} — ${pageCount} pages`);
  addMsg("system", "Document loaded — ask a question about it.");
  updateChatPlaceholder();
  updateModeUI();
  await renderPage();
}

function gotoPage(n) {
  if (!pdfjsDoc || n < 1 || n > pageCount) return;
  currentPage = n;
  renderPage();
}

function setZoom(z) {
  zoom = Math.min(3, Math.max(0.5, Math.round(z * 100) / 100));
  $("zoomInfo").textContent = Math.round(zoom * 100) + "%";
  renderPage();
}

async function renderPage() {
  if (!pdfjsDoc) return;
  const page = await pdfjsDoc.getPage(currentPage);
  const viewport = page.getViewport({ scale: zoom });
  const canvas = $("pageCanvas");
  const ctx = canvas.getContext("2d");
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  await page.render({ canvasContext: ctx, viewport: viewport }).promise;
  drawAnnotations(ctx, viewport);

  $("pageInfo").textContent = `Page ${currentPage} / ${pageCount}`;
  $("prevBtn").disabled = currentPage <= 1;
  $("nextBtn").disabled = currentPage >= pageCount;
  updateChatPlaceholder();
}

// PDF-point rect -> canvas {x,y,w,h}
function vpRect(viewport, q) {
  const r = viewport.convertToViewportRectangle([q.x0, q.y0, q.x1, q.y1]);
  return {
    x: Math.min(r[0], r[2]), y: Math.min(r[1], r[3]),
    w: Math.abs(r[2] - r[0]), h: Math.abs(r[3] - r[1]),
  };
}

function drawAnnotations(ctx, viewport) {
  annotations.filter((a) => a.page === currentPage).forEach((a) => {
    const css = Annotator.CSS[a.color] || "#ffeb3b";
    if (["highlight", "underline", "strikeout", "squiggly"].includes(a.tool)) {
      a.quads.forEach((q) => {
        const r = vpRect(viewport, q);
        if (a.tool === "highlight") {
          ctx.save();
          ctx.globalCompositeOperation = "multiply";
          ctx.fillStyle = css;
          ctx.fillRect(r.x, r.y, r.w, r.h);
          ctx.restore();
        } else {
          ctx.strokeStyle = css;
          ctx.lineWidth = 2;
          const yy = a.tool === "strikeout" ? r.y + r.h / 2 : r.y + r.h - 1;
          ctx.beginPath();
          ctx.moveTo(r.x, yy);
          ctx.lineTo(r.x + r.w, yy);
          ctx.stroke();
        }
      });
    } else {
      const r = vpRect(viewport, a.rect);
      ctx.strokeStyle = css;
      ctx.fillStyle = css;
      ctx.lineWidth = 2.5;
      if (a.tool === "rectangle") {
        ctx.strokeRect(r.x - 2, r.y - 2, r.w + 4, r.h + 4);
      } else if (a.tool === "oval") {
        ctx.beginPath();
        ctx.ellipse(r.x + r.w / 2, r.y + r.h / 2,
          r.w / 2 + 2, r.h / 2 + 2, 0, 0, 2 * Math.PI);
        ctx.stroke();
      } else if (a.tool === "sticky-note") {
        ctx.fillRect(r.x + r.w + 3, r.y, 16, 16);
        ctx.strokeRect(r.x + r.w + 3, r.y, 16, 16);
      } else if (a.tool === "arrow") {
        ctx.beginPath();
        ctx.moveTo(r.x - 48, r.y - 20);
        ctx.lineTo(r.x, r.y);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(r.x, r.y);
        ctx.lineTo(r.x - 10, r.y - 2);
        ctx.lineTo(r.x - 4, r.y - 10);
        ctx.closePath();
        ctx.fill();
      } else { // text-box / callout
        ctx.save();
        ctx.globalAlpha = 0.5;
        ctx.fillRect(r.x + r.w + 12, r.y - 8, 120, 38);
        ctx.restore();
        ctx.strokeRect(r.x + r.w + 12, r.y - 8, 120, 38);
        if (a.tool === "callout") {
          ctx.beginPath();
          ctx.moveTo(r.x + r.w, r.y + r.h / 2);
          ctx.lineTo(r.x + r.w + 12, r.y + 4);
          ctx.stroke();
        }
      }
    }
  });
}

// ===========================================================================
// Text extraction — native (pdf.js) with OCR (Tesseract.js) fallback
// ===========================================================================
async function nativeWords(page) {
  const tc = await page.getTextContent();
  const words = [];
  tc.items.forEach((item) => {
    if (!item.str) return;
    const x = item.transform[4];
    const y = item.transform[5];
    const h = item.height || Math.abs(item.transform[3]) || 8;
    const total = item.str.length || 1;
    const perChar = item.width / total;
    let cx = x;
    item.str.split(/(\s+)/).forEach((part) => {
      const w = part.length * perChar;
      if (part.trim()) {
        words.push({ text: part, x0: cx, y0: y, x1: cx + w, y1: y + h });
      }
      cx += w;
    });
  });
  return words;
}

async function ocrWords(page) {
  await ensureTesseract();
  const scale = 2;
  const vp = page.getViewport({ scale: scale });
  const cv = document.createElement("canvas");
  cv.width = vp.width;
  cv.height = vp.height;
  await page.render({ canvasContext: cv.getContext("2d"), viewport: vp })
    .promise;
  const res = await Tesseract.recognize(cv, "eng");
  const pageHeight = page.getViewport({ scale: 1 }).height;
  const words = [];
  (res.data.words || []).forEach((w) => {
    if (!w.text || !w.text.trim()) return;
    const b = w.bbox;
    words.push({
      text: w.text,
      x0: b.x0 / scale, x1: b.x1 / scale,
      y0: pageHeight - b.y1 / scale,
      y1: pageHeight - b.y0 / scale,
    });
  });
  return words;
}

async function getWords(pageIdx) {
  if (wordCache[pageIdx]) return wordCache[pageIdx];
  const page = await pdfjsDoc.getPage(pageIdx + 1);
  let words = await nativeWords(page);
  if (words.length === 0) {
    const saved = $("docName").textContent;
    status(`OCR page ${pageIdx + 1}…`);
    words = await ocrWords(page);
    status(saved);
  }
  wordCache[pageIdx] = words;
  return words;
}

function plainText(words) {
  const lines = [];
  words.forEach((w) => {
    const cy = (w.y0 + w.y1) / 2;
    const h = Math.max(w.y1 - w.y0, 4);
    let ln = lines.find((l) => Math.abs(l.cy - cy) < h * 0.6);
    if (!ln) { ln = { cy: cy, ws: [] }; lines.push(ln); }
    ln.ws.push(w);
  });
  lines.sort((a, b) => b.cy - a.cy);
  return lines.map((l) =>
    l.ws.sort((a, b) => a.x0 - b.x0).map((w) => w.text).join(" ")
  ).join("\n");
}

async function fullText() {
  const parts = [];
  for (let i = 0; i < pageCount; i++) {
    if (pageCount > 30) status(`Reading page ${i + 1}/${pageCount}…`);
    const words = await getWords(i);
    parts.push(`[PAGE ${i + 1}]\n${plainText(words)}`);
  }
  status(`${docName} — ${pageCount} pages`);
  return parts.join("\n\n");
}

// ===========================================================================
// Chat
// ===========================================================================
function addMsg(role, text) {
  const div = el("div", "msg " + role, text);
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}

async function sendChat() {
  if (!pdfjsDoc) { alert("Load a PDF first."); return; }
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;

  const scope = $("scopeSelect").value;
  const color = cycleColors[turnIndex % cycleColors.length];
  turnIndex++;

  input.value = "";
  const userMsg = addMsg("user", text);
  userMsg.style.borderRight = `5px solid ${Annotator.CSS[color] || color}`;
  history.push({ role: "user", content: text });

  if (mode === "copypaste") await buildCopyPastePrompt(text, scope, color);
  else await apiChat(text, scope, color);
}

async function buildContext(scope, includeFull) {
  if (scope === "document") {
    if (includeFull) {
      return `--- FULL DOCUMENT (${pageCount} pages) ---\n` +
        `${await fullText()}\n--- END DOCUMENT ---`;
    }
    return `The user has attached the PDF file "${docName}" ` +
      `(${pageCount} pages) to this conversation — read it to answer.`;
  }
  const words = await getWords(currentPage - 1);
  return `--- PAGE ${currentPage} of ${pageCount} ---\n` +
    `${plainText(words)}\n--- END PAGE ---`;
}

// ---- Copy/paste mode ------------------------------------------------------
async function buildCopyPastePrompt(question, scope, color) {
  $("sendBtn").disabled = true;
  try {
    const includeFull = scope === "document" && $("fullText").checked;
    const system = scope === "document" ? DOC_PROMPT : PAGE_PROMPT;
    const context = await buildContext(scope, includeFull);
    const prompt = `${system}\n\n${context}\n\nQUESTION: ${question}`;
    renderPromptCard(prompt, scope, currentPage, color,
      scope === "document" && !includeFull);
  } catch (e) {
    addMsg("system", "Error building prompt: " + e.message);
  } finally {
    $("sendBtn").disabled = false;
  }
}

function renderPromptCard(prompt, scope, page, color, needsFile) {
  const card = el("div", "msg bot prompt-card");
  card.appendChild(el("div", "pc-step",
    "Step 1 — copy this prompt into your approved LLM chat:"));

  const promptBox = document.createElement("textarea");
  promptBox.className = "pc-prompt";
  promptBox.readOnly = true;
  promptBox.rows = 6;
  promptBox.value = prompt;
  card.appendChild(promptBox);

  const copyBtn = el("button", "pc-btn", "Copy prompt");
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(prompt);
    copyBtn.textContent = "Copied ✓";
    setTimeout(() => (copyBtn.textContent = "Copy prompt"), 1500);
  };
  card.appendChild(copyBtn);

  if (needsFile) {
    card.appendChild(el("div", "pc-note",
      `Also attach the PDF file "${docName}" to that chat so the LLM can `
      + "read the whole document."));
  }

  card.appendChild(el("div", "pc-step",
    "Step 2 — paste the LLM's full reply here:"));
  const replyBox = document.createElement("textarea");
  replyBox.className = "pc-reply";
  replyBox.rows = 4;
  replyBox.placeholder = "Paste the LLM's reply, then click Apply…";
  card.appendChild(replyBox);

  const applyBtn = el("button", "pc-btn primary", "Apply reply & annotate");
  applyBtn.onclick = () =>
    applyReply(replyBox.value, scope, page, color, applyBtn);
  card.appendChild(applyBtn);

  $("chat").appendChild(card);
  $("chat").scrollTop = $("chat").scrollHeight;
}

async function applyReply(reply, scope, page, color, btn) {
  if (!reply.trim()) return;
  btn.disabled = true;
  btn.textContent = "Applying…";
  const parsed = extractJson(reply);
  const links = normalizeLinks(parsed.links, scope, page);
  btn.textContent = "Applied ✓";
  history.push({ role: "assistant", content: parsed.answer || reply });
  await showAnswer(parsed.answer || reply, links, scope, color);
}

// ---- OpenRouter API mode --------------------------------------------------
async function apiChat(question, scope, color) {
  $("sendBtn").disabled = true;
  const key = $("apiKey").value.trim();
  if (!key) {
    addMsg("system", "Enter your OpenRouter API key first.");
    $("sendBtn").disabled = false;
    return;
  }
  const thinking = addMsg("system",
    scope === "document" ? "Reading the whole document…" : "Thinking…");
  try {
    const system = scope === "document" ? DOC_PROMPT : PAGE_PROMPT;
    const context = await buildContext(scope, true);
    const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "HTTP-Referer": location.origin,
        "X-Title": "LLM PDF Annotator",
      },
      body: JSON.stringify({
        model: $("modelSelect").value,
        max_tokens: 16000,
        messages: [
          { role: "system", content: system },
          { role: "system", content: context },
        ].concat(history),
      }),
    });
    const data = await r.json();
    thinking.remove();
    if (!r.ok) {
      addMsg("system", "OpenRouter error: " +
        (data.error ? data.error.message : r.status));
      return;
    }
    const msg = (data.choices && data.choices[0] && data.choices[0].message)
      || {};
    const replyText = (msg.content || msg.reasoning || "").trim();
    if (!replyText) { addMsg("system", "The model returned no text."); return; }
    const parsed = extractJson(replyText);
    const links = normalizeLinks(parsed.links, scope, currentPage);
    history.push({ role: "assistant", content: parsed.answer || replyText });
    await showAnswer(parsed.answer || replyText, links, scope, color);
  } catch (e) {
    thinking.remove();
    addMsg("system", "Request failed: " + e.message);
  } finally {
    $("sendBtn").disabled = false;
  }
}

function extractJson(text) {
  if (!text) return { answer: "", links: [] };
  try { return JSON.parse(text); } catch (e) { /* fall through */ }
  const m = text.match(/\{[\s\S]*\}/);
  if (m) { try { return JSON.parse(m[0]); } catch (e) { /* fall */ } }
  return { answer: text.trim(), links: [] };
}

function normalizeLinks(links, scope, page) {
  if (!Array.isArray(links)) return [];
  return links
    .filter((ln) => ln && (ln.quote || "").trim())
    .map((ln) => ({
      point: ln.point || "",
      quote: ln.quote,
      page: scope === "page" ? page : (parseInt(ln.page, 10) || page),
    }));
}

// ===========================================================================
// Show answer + auto-annotate
// ===========================================================================
async function showAnswer(answer, links, scope, color) {
  const div = el("div", "msg bot", answer);
  if (links.length) {
    const box = el("div", "quote-box");
    box.style.borderLeftColor = Annotator.CSS[color] || color;
    box.appendChild(el("div", null, scope === "document"
      ? `${links.length} key point(s) linked to the document:`
      : `Linked passage (page ${links[0].page}):`));
    links.forEach((ln) => {
      const item = el("div", "link-item");
      if (ln.point && scope === "document") {
        item.appendChild(el("div", null, "• " + ln.point));
      }
      item.appendChild(el("div", "q", `p.${ln.page}: "${ln.quote}"`));
      box.appendChild(item);
    });
    const stat = el("div", "annotate-status", `Annotating in ${color}…`);
    box.appendChild(stat);
    div.appendChild(box);
    $("chat").appendChild(div);
    $("chat").scrollTop = $("chat").scrollHeight;
    await annotateLinks(links, answer, color, stat);
  } else {
    div.appendChild(el("div", "quote-box q",
      "No specific passage was linked to this answer."));
    $("chat").appendChild(div);
    $("chat").scrollTop = $("chat").scrollHeight;
  }
}

async function annotateLinks(links, answer, color, statusEl) {
  const tool = $("toolSelect").value;
  let applied = 0;
  const pages = [];
  for (const ln of links) {
    const found = await locateQuote(ln.page, ln.quote);
    if (!found) continue;
    const comment = (ln.point && links.length > 1) ? ln.point : answer;
    Annotator.apply(pdfLibDoc, found.page - 1, found.quads, found.rect,
      tool, color, comment);
    annotations.push({
      page: found.page, tool: tool, color: color,
      quads: found.quads, rect: found.rect, comment: comment,
    });
    applied++;
    if (pages.indexOf(found.page) < 0) pages.push(found.page);
  }
  if (applied) {
    const dot = el("span", "swatch");
    dot.style.background = Annotator.CSS[color] || color;
    statusEl.replaceChildren(dot, document.createTextNode(
      ` Annotated ${applied}/${links.length} in ${color} — `
      + `page(s) ${pages.sort((a, b) => a - b).join(", ")}`));
    statusEl.classList.add("done");
    annotationCount += applied;
    updateAnnCount();
    await renderPage();
  } else {
    statusEl.textContent =
      "Could not locate the passage(s) in the PDF to annotate.";
  }
}

// Locate a quote: try the hinted page (+/-3) first, then the whole document.
async function locateQuote(pageHint, quote) {
  for (const p of [pageHint, pageHint - 1, pageHint + 1,
                    pageHint - 2, pageHint + 2, pageHint - 3, pageHint + 3]) {
    if (p < 1 || p > pageCount) continue;
    const words = await getWords(p - 1);
    const m = Matcher.findQuoteQuads(words, quote);
    if (m.rect) return { page: p, quads: m.quads, rect: m.rect };
  }
  // Whole-document fuzzy search.
  const pagesWords = [];
  for (let i = 0; i < pageCount; i++) pagesWords.push(await getWords(i));
  return Matcher.locate(pagesWords, pageHint, quote);
}

// ===========================================================================
// Export
// ===========================================================================
async function exportPdf() {
  if (!pdfLibDoc) return;
  const bytes = await pdfLibDoc.save();
  const blob = new Blob([bytes], { type: "application/pdf" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = docName.replace(/\.pdf$/i, "") + "_annotated.pdf";
  a.click();
  URL.revokeObjectURL(a.href);
}
