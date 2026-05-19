"use strict";

// ---- State ----------------------------------------------------------------
let doc = null;          // { id, name, pages }
let currentPage = 1;
let zoom = 1.5;
let cacheBust = 0;       // bumped after each annotation to force re-render
let indexTimer = null;   // poll handle for background indexing
let mode = "copypaste"; // "copypaste" | "api"
let cycleColors = ["yellow", "green", "blue", "pink", "orange", "red"];
let turnIndex = 0;       // question number — drives the annotation colour
let annotationCount = 0; // running total of annotations in the current PDF
const history = [];      // chat messages: { role, content }

// Annotation colour name -> CSS colour (matches the PDF colours).
const CSS_COLORS = {
  yellow: "#ffeb3b", green: "#8ce566", blue: "#73bdff",
  pink: "#ff8cc7", orange: "#ffb347", red: "#f25959",
};

const TOOL_LABELS = {
  "highlight": "Highlight", "underline": "Underline",
  "strikeout": "Strikethrough", "squiggly": "Squiggly underline",
  "sticky-note": "Sticky note (comment)", "text-box": "Text box",
  "callout": "Callout box", "rectangle": "Rectangle",
  "oval": "Oval", "arrow": "Arrow",
};

// ---- Element helpers ------------------------------------------------------
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

// ===========================================================================
// Init
// ===========================================================================
window.addEventListener("DOMContentLoaded", () => {
  loadModels();
  loadTools();

  const dz = $("dropZone");
  $("pickBtn").onclick = () => $("fileInput").click();
  $("fileInput").onchange = (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); };
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });

  $("prevBtn").onclick = () => gotoPage(currentPage - 1);
  $("nextBtn").onclick = () => gotoPage(currentPage + 1);
  $("zoomIn").onclick = () => setZoom(zoom + 0.25);
  $("zoomOut").onclick = () => setZoom(zoom - 0.25);

  $("scopeSelect").onchange = () => { updateChatPlaceholder(); updateModeUI(); };

  // LLM access mode + API key — remembered in this browser.
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
  $("exportBtn").onclick = () => {
    if (doc) window.location = `/api/export/${doc.id}`;
  };
});

// Show only the controls relevant to the chosen LLM-access mode.
function updateModeUI() {
  const cp = mode === "copypaste";
  $("modelRow").classList.toggle("hidden", cp);
  $("apiKeyRow").classList.toggle("hidden", cp);
  $("fullTextRow").classList.toggle("hidden",
    !cp || $("scopeSelect").value !== "document");
}

// ===========================================================================
// Dropdowns
// ===========================================================================
async function loadModels() {
  const sel = $("modelSelect");
  try {
    const r = await fetch("/api/models");
    const { models } = await r.json();
    const groups = {};
    models.forEach((m) => (groups[m.provider] = groups[m.provider] || []).push(m));
    Object.keys(groups).forEach((prov) => {
      const og = document.createElement("optgroup");
      og.label = prov;
      groups[prov].forEach((m) => {
        const o = document.createElement("option");
        o.value = m.id;
        o.textContent = m.name;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
  } catch (e) {
    sel.appendChild(el("option", null, "Could not load models"));
  }
}

async function loadTools() {
  const r = await fetch("/api/tools");
  const { tools, colors } = await r.json();
  tools.forEach((t) => {
    const o = document.createElement("option");
    o.value = t;
    o.textContent = TOOL_LABELS[t] || t;
    $("toolSelect").appendChild(o);
  });
  if (colors && colors.length) cycleColors = colors;
  // Show the colour cycle as a row of dots.
  cycleColors.forEach((c) => {
    const dot = el("span", "swatch");
    dot.style.background = CSS_COLORS[c] || c;
    dot.title = c;
    $("colorLegend").appendChild(dot);
  });
}

// ===========================================================================
// Upload + background indexing
// ===========================================================================
async function uploadFile(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    alert("Please choose a PDF file."); return;
  }
  $("docName").textContent = `Uploading ${file.name}...`;
  const fd = new FormData();
  fd.append("pdf", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const data = await r.json();
  if (!r.ok) { $("docName").textContent = "Upload failed: " + data.error; return; }

  doc = { id: data.doc_id, name: data.name, pages: data.pages };
  currentPage = 1;
  history.length = 0;
  cacheBust = 0;
  turnIndex = 0;
  annotationCount = 0;
  updateAnnCount();
  $("chat").replaceChildren();
  $("dropZone").classList.add("hidden");
  $("pageArea").classList.remove("hidden");
  $("exportBtn").disabled = false;

  // Whole-document questions are the default. The background index just
  // needs to finish before they can be answered (instant for normal PDFs).
  $("scopeSelect").value = "document";
  $("scopeSelect").querySelector('option[value="document"]').textContent =
    "The whole document (indexing...)";
  updateChatPlaceholder();
  updateModeUI();

  addMsg("system", "Document loaded and being indexed. Ask questions about " +
    "the whole PDF — a large scanned PDF takes a little while to index.");
  renderPage();
  pollIndex();
}

function pollIndex() {
  if (indexTimer) clearInterval(indexTimer);
  const docOpt = $("scopeSelect").querySelector('option[value="document"]');
  indexTimer = setInterval(async () => {
    if (!doc) { clearInterval(indexTimer); return; }
    try {
      const r = await fetch(`/api/index-status/${doc.id}`);
      const s = await r.json();
      if (s.complete) {
        clearInterval(indexTimer);
        docOpt.textContent = "The whole document";
        $("docName").textContent = `${doc.name} — ${doc.pages} pages (indexed)`;
        addMsg("system", "Indexing complete — whole-document questions are "
          + "ready.");
      } else {
        docOpt.textContent =
          `The whole document (indexing ${s.done}/${s.total})`;
        $("docName").textContent =
          `${doc.name} — ${doc.pages} pages (indexing ${s.done}/${s.total})`;
      }
    } catch (e) { /* keep polling */ }
  }, 1500);
}

function gotoPage(n) {
  if (!doc || n < 1 || n > doc.pages) return;
  currentPage = n;
  renderPage();
}

function setZoom(z) {
  zoom = Math.min(3, Math.max(0.5, Math.round(z * 100) / 100));
  $("zoomInfo").textContent = Math.round(zoom * 100) + "%";
  renderPage();
}

function renderPage() {
  if (!doc) return;
  $("pageImg").src =
    `/api/page/${doc.id}/${currentPage}?zoom=${zoom}&c=${cacheBust}`;
  $("pageInfo").textContent = `Page ${currentPage} / ${doc.pages}`;
  $("prevBtn").disabled = currentPage <= 1;
  $("nextBtn").disabled = currentPage >= doc.pages;
  updateChatPlaceholder();
}

function updateChatPlaceholder() {
  if (!doc) return;
  $("chatInput").placeholder = $("scopeSelect").value === "document"
    ? "Ask the LLM a question about the whole document..."
    : `Ask the LLM about page ${currentPage}...`;
}

function updateAnnCount() {
  $("annCount").textContent = annotationCount
    ? `${annotationCount} annotation${annotationCount === 1 ? "" : "s"}`
    : "";
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
  if (!doc) { alert("Load a PDF first."); return; }
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;

  const scope = $("scopeSelect").value;
  // Each question gets the next colour in the cycle.
  const color = cycleColors[turnIndex % cycleColors.length];
  turnIndex++;

  input.value = "";
  const userMsg = addMsg("user", text);
  userMsg.style.borderRight = `5px solid ${CSS_COLORS[color] || color}`;
  history.push({ role: "user", content: text });

  if (mode === "copypaste") {
    await buildCopyPastePrompt(text, scope, color);
  } else {
    await apiChat(scope, color);
  }
}

// ---- OpenRouter API mode --------------------------------------------------
async function apiChat(scope, color) {
  $("sendBtn").disabled = true;
  const thinking = addMsg("system",
    scope === "document" ? "Reading the whole document..." : "Thinking...");
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        doc_id: doc.id,
        model: $("modelSelect").value,
        api_key: $("apiKey").value.trim(),
        page: currentPage,
        scope: scope,
        messages: history,
      }),
    });
    const data = await r.json();
    thinking.remove();
    if (!r.ok) { addMsg("system", "Error: " + data.error); return; }

    history.push({ role: "assistant", content: data.answer });
    showAnswer(data.answer, data.links || [], data.scope, color);
  } catch (e) {
    thinking.remove();
    addMsg("system", "Request failed: " + e);
  } finally {
    $("sendBtn").disabled = false;
  }
}

// ---- Copy/paste mode ------------------------------------------------------
async function buildCopyPastePrompt(text, scope, color) {
  $("sendBtn").disabled = true;
  const includeFull = scope === "document" && $("fullText").checked;
  try {
    const r = await fetch("/api/build-prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        doc_id: doc.id, page: currentPage, scope: scope,
        question: text, include_full_text: includeFull,
      }),
    });
    const data = await r.json();
    if (!r.ok) { addMsg("system", "Error: " + data.error); return; }
    renderPromptCard(data.prompt, scope, currentPage, color,
                     scope === "document" && !includeFull);
  } catch (e) {
    addMsg("system", "Request failed: " + e);
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
      `Also attach the PDF file "${doc.name}" to that chat so the LLM `
      + "can read the whole document."));
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
  if (!reply.trim()) { return; }
  btn.disabled = true;
  btn.textContent = "Applying…";
  try {
    const r = await fetch("/api/parse-reply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: scope, page: page, reply: reply }),
    });
    const data = await r.json();
    if (!r.ok) {
      btn.disabled = false;
      btn.textContent = "Apply reply & annotate";
      addMsg("system", "Error: " + data.error);
      return;
    }
    btn.textContent = "Applied ✓";
    history.push({ role: "assistant", content: data.answer });
    showAnswer(data.answer, data.links || [], data.scope, color);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Apply reply & annotate";
    addMsg("system", "Failed: " + e);
  }
}

function showAnswer(answer, links, scope, color) {
  const div = el("div", "msg bot", answer);

  if (links.length) {
    const box = el("div", "quote-box");
    box.style.borderLeftColor = CSS_COLORS[color] || color;
    const heading = scope === "document"
      ? `${links.length} key point(s) linked to the document:`
      : `Linked passage (page ${links[0].page}):`;
    box.appendChild(el("div", null, heading));

    links.forEach((ln) => {
      const item = el("div", "link-item");
      if (ln.point && scope === "document") {
        item.appendChild(el("div", null, "• " + ln.point));
      }
      item.appendChild(el("div", "q", `p.${ln.page}: "${ln.quote}"`));
      box.appendChild(item);
    });

    // Annotations are applied automatically — this line reports the result.
    const status = el("div", "annotate-status", `Annotating in ${color}…`);
    box.appendChild(status);
    div.appendChild(box);
    $("chat").appendChild(div);
    $("chat").scrollTop = $("chat").scrollHeight;
    annotateBatch(links, answer, status, color);
  } else {
    div.appendChild(el("div", "quote-box q",
      "No specific passage was linked to this answer."));
    $("chat").appendChild(div);
    $("chat").scrollTop = $("chat").scrollHeight;
  }
}

// ===========================================================================
// Annotation — applied automatically after every answer, one colour per turn
// ===========================================================================
async function annotateBatch(links, answer, status, color) {
  // Each annotation's popup content = its key point (or the answer if single).
  const payload = links.map((ln) => ({
    page: ln.page,
    quote: ln.quote,
    comment: (ln.point && links.length > 1) ? ln.point : answer,
  }));
  try {
    const r = await fetch("/api/annotate-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        doc_id: doc.id,
        tool: $("toolSelect").value,
        color: color,
        links: payload,
      }),
    });
    const data = await r.json();
    if (!r.ok) { status.textContent = "Annotation error: " + data.error; return; }

    const pages = [...new Set(data.results.filter((x) => x.located)
      .map((x) => x.page))].sort((a, b) => a - b);
    if (data.applied) {
      const dot = el("span", "swatch");
      dot.style.background = CSS_COLORS[color] || color;
      status.replaceChildren(dot, document.createTextNode(
        ` Annotated ${data.applied}/${links.length} in ${color} — `
        + `page(s) ${pages.join(", ")}`));
      status.classList.add("done");
    } else {
      status.textContent =
        "Could not locate the passage(s) in the PDF to annotate.";
    }
    annotationCount += data.applied;
    updateAnnCount();
    cacheBust++;
    renderPage();
  } catch (e) {
    status.textContent = "Annotation failed: " + e;
  }
}
