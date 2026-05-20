# Project Plan — LLM-Annotated PDF Tool

## Goal
A local web app to converse with an LLM about a PDF and create annotations in that
PDF that link passages of the document to the LLM's answers.

## Key facts established
- `12629412.pdf` is a 304-page patent, **scanned images only — no text layer**.
- Tesseract OCR 5.5.1 is installed; PyMuPDF can OCR pages to recover word boxes.
- Python venv built with Python 3.13.8 (note: shell `python` alias points at 3.14 —
  always call `venv/bin/python` by path).

## Decisions (confirmed with user)
- **Linking:** Automatic anchoring — the LLM names the relevant PDF passage; the app
  finds that text and places the annotation there.
- **Text source:** Per page, use the **native PDF text layer when present**
  (`page.get_text("words")` for word boxes). Fall back to **OCR only for pages with
  no text** (scanned images), done on-demand the first time that page is used.
- **Models:** Dropdown populated live from OpenRouter's model API (newest OpenAI,
  Google, Anthropic models).
- **Annotation tools:** Mirror Adobe Acrobat's comment/markup set.
- **API key:** `OPENROUTER_API_KEY` read from `~/.env`.

## Architecture
- **Backend:** Flask (`app.py`) — PDF upload, per-page OCR + word-box cache,
  OpenRouter chat proxy, model list, annotation engine (PyMuPDF), export.
- **Frontend:** Single page (`static/index.html` + `app.js` + `style.css`).
  PDF.js renders pages; chat panel; model dropdown; annotation-method dropdown;
  drag-and-drop / file-picker upload.
- **Annotation flow:** chat answer → LLM returns `{answer, source_quote, page}` →
  app searches that page's OCR word index for the quote → bbox → applies the
  selected annotation type with the answer as the annotation's popup/comment text.

## Adobe Acrobat-style annotation tools (all supported by PyMuPDF)
- Sticky Note / Comment  — `add_text_annot`
- Highlight (color choice, e.g. yellow) — `add_highlight_annot`
- Underline — `add_underline_annot`
- Strikethrough — `add_strikeout_annot`
- Squiggly underline — `add_squiggly_annot`
- Text Box (FreeText) — `add_freetext_annot`
- Callout — FreeText with callout line
- Rectangle / Oval — `add_rect_annot` / `add_circle_annot`
- Line / Arrow — `add_line_annot`

## To-do — Phase 1 (core, COMPLETE)
- [x] 1. Scaffolding: `requirements.txt`, dirs (`static/`, `uploads/`, `outputs/`, `tests/auto/`)
- [x] 2. Flask backend skeleton + config (`OPENROUTER_API_KEY` from env)
- [x] 3. PDF upload endpoint (drag-drop + file picker), store + open with PyMuPDF
- [x] 4. Page rendering — server-side PyMuPDF PNG render (annotations baked in)
- [x] 5. Per-page word-box index — native text layer first, OCR fallback, cached
- [x] 6. OpenRouter models endpoint — fetch live, filter newest 3-provider models
- [x] 7. Chat endpoint — proxy to OpenRouter; system prompt for structured answers
- [x] 8. Annotation engine — locate quote, apply chosen tool, store answer as popup
- [x] 9. Frontend UI — PDF viewer + chat + model dropdown + annotation-method dropdown
- [x] 10. Export annotated PDF + download
- [x] 11. `start` / `stop` server scripts
- [x] 12. End-to-end test in `tests/auto/` — 22/22 passing

## Phase 2 — whole-document summary with multiple annotations (COMPLETE)
- [x] Whole-document text mode — background OCR of all pages on upload;
      "Whole document" conversation scope; full text fed to the model
- [x] Multi-link structured answers — one reply yields several {point, quote,
      page}; "one annotation per key point"
- [x] Cross-page quote location — fuzzy whole-document search (the LLM's page
      number is only a hint; its quotes drift from OCR text)
- [x] Batch annotation — `/api/annotate-batch` applies N annotations at once
- [x] OCR-cache persistence — word index cached by file hash, so re-uploading
      the same PDF or restarting the server skips the OCR pass
- [x] Three fixed OpenRouter models (Claude Opus 4.7 Fast, GPT chat-latest,
      Gemini Pro latest)

Notable fix: the first summary run found all 4 key points but annotated 0/4 —
the matcher required an exact run on the LLM-named page. Replaced with a fuzzy
multiset-overlap matcher that searches every page, tolerating OCR noise, word
splits, and the LLM's page-number drift.

## Review — Phase 1
Built a local web app (Flask backend + vanilla HTML/CSS/JS frontend):
- **Upload:** drag-drop or file picker; original kept in `uploads/`, working
  copy in `outputs/`.
- **Viewer:** pages rendered server-side by PyMuPDF as PNG (annotations baked
  in) — works identically for normal and scanned PDFs; page nav + zoom.
- **Text:** native PDF text layer used when present; Tesseract OCR fallback
  for image-only pages, cached per page.
- **Chat:** OpenRouter proxy; model dropdown populated live (newest 10 each of
  OpenAI / Google / Anthropic). Current page's text is the context; the model
  replies as JSON `{answer, source_quote}`.
- **Annotation:** 10 Adobe Acrobat-style tools (highlight, underline,
  strikethrough, squiggly, sticky note, text box, callout, rectangle, oval,
  arrow) + 6 colors. The quoted passage is located in the page word-index and
  the annotation is placed there with the LLM answer as popup content.
- **Export:** download the annotated PDF.
- **Tests:** `tests/auto/test_backend.py` — 22/22 pass, incl. live OCR of the
  patent and all 10 tools.
- **Run:** `./start` (→ http://127.0.0.1:5050), `./stop`.

Known limit (addressed by Phase 2): chat context is the *current page only*,
and each answer yields a *single* annotation.

## Review — Phase 2
- **Whole-document scope:** background OCR of all pages on upload (cached to
  disk by file hash — re-uploads/restarts skip the OCR); "Whole document"
  conversation scope feeds the full text to the model.
- **Multi-point summaries:** one answer returns several {point, quote, page};
  `/api/annotate-batch` places one annotation per key point.
- **Fuzzy whole-document quote location:** scores every page on content-token
  multiset overlap and trims the highlight to the densest cluster — tolerates
  OCR noise, word splits, and large LLM page-number drift.
- **Models:** dropdown fixed to Claude Opus 4.7 Fast, GPT chat-latest,
  Gemini 3.1 Pro.
- **Bug fixed mid-test:** thinking models can return `content: null`; chat now
  sets `max_tokens` and falls back to `reasoning`, with a clear error if empty.

### Verified end to end
- `tests/auto/test_backend.py` — 25/25 (incl. live OCR + all 10 tools).
- `tests/auto/test_ui.py` — Selenium UI: 5/5 (upload → chat → annotate).
- `tests/auto/test_docsummary.py` — all 3 models summarise a multi-page PDF
  and batch-annotate (16/16 key points placed).
- `tests/auto/test_summary.py` — the 304-page patent: full OCR index, Gemini
  2-paragraph summary, 5/5 key points located and highlighted. The LLM's page
  guesses were off by 39-79 pages; the fuzzy search corrected every one.
- Result: `outputs/12629412_annotated.pdf` (5 highlights).

### How to run
1. `./start` → open http://127.0.0.1:5050  (`./stop` to stop)
2. Drag-drop a PDF; pick a scope and an annotation tool.
3. Ask questions; answers auto-annotate; "Export annotated PDF" to download.

## Phase 3 — two LLM-access modes + UX refinements
For users who can only use a company-approved LLM (no API access):
- **Copy/paste mode (default):** the app builds a self-contained prompt; the
  user pastes it into their approved LLM chat, pastes the reply back, and the
  app annotates locally. For whole-document questions the user can attach the
  PDF file in their chat, or embed the full text. Endpoints `/api/build-prompt`
  and `/api/parse-reply` (no LLM call).
- **OpenRouter API mode:** key entered in the UI (remembered in `localStorage`),
  falls back to `~/.env`.
Other refinements this phase:
- Whole-document is the default scope from upload (was: only after indexing).
- Annotations are applied **automatically** after each answer (no button).
- The highlight **colour cycles per question** (yellow→green→blue→…); a running
  annotation count shows in the header.
- Matcher anchors on content tokens and trims to the densest cluster, so
  highlights stay tight.

### Verified
- `test_backend.py` — 29/29 (incl. build-prompt / parse-reply).
- `test_ui.py` — 9/9 Selenium: copy/paste flow (canned reply, colour cycle)
  + OpenRouter API mode.

## Phase 4 (planned) — static, browser-only build for GitHub Pages
Goal: a 100% client-side version in `docs/` so GitHub Pages serves a working
site (no Python backend). The Flask app stays at the repo root for local use.
Repo pushed to https://github.com/metaphorz/annot8.

Confirmed with user: all 10 annotation tools; both LLM modes (copy/paste + API).

Libraries (CDN, no build step): **pdf.js** (render + native text), **Tesseract.js**
(in-browser OCR), **pdf-lib** (write annotations into the PDF).

Architecture:
- The PDF is held in memory (FileReader). pdf.js renders pages; pdf-lib holds
  the working copy that accumulates annotations; export = `pdf-lib` save.
- On-screen annotations shown via an HTML overlay layer; the exported PDF
  carries real pdf-lib annotation objects (same {page, rect, tool, colour} data).
- The fuzzy quote-matcher is ported from `annotator.py` to JS.

Known constraint: in-browser OCR is slow, so for *scanned* PDFs OCR runs
per-page on demand; whole-document scope is best for native-text PDFs.

### To-do — Phase 4 (COMPLETE)
- [x] 1. `docs/` scaffolding — index.html, style.css, app.js, matcher.js,
      annotator.js
- [x] 2. PDF load (FileReader) + pdf.js render + page nav / zoom
- [x] 3. Native text extraction via pdf.js text layer → word boxes
- [x] 4. OCR fallback — Tesseract.js per page, pixel→PDF coordinate mapping
- [x] 5. Port the fuzzy matcher (matchQuote / findQuoteQuads / locate) to JS
- [x] 6. Annotation engine in pdf-lib — all 10 Acrobat-style tools + colours
- [x] 7. On-canvas drawing for live annotation display
- [x] 8. Copy/paste LLM flow (build prompt, paste reply, parse) — client-side
- [x] 9. OpenRouter API mode — browser fetch, key in localStorage
- [x] 10. Per-question colour cycling, auto-annotate, annotation count
- [x] 11. Export annotated PDF (pdf-lib save → download)
- [x] 12. Enable GitHub Pages (main branch, /docs)
- [x] 13. Selenium test of the static build — `test_static.py` 10/10

### Review — Phase 4
- The static app lives in `docs/`; the Flask app stays at the repo root.
- **Libraries are vendored** in `docs/vendor/` (pdf.js, pdf-lib, Tesseract.js)
  — no external CDN, so it works offline and behind corporate proxies.
- Tesseract.js is lazy-loaded only when a scanned page needs OCR, so the page
  never blocks on it and native-text PDFs work without it.
- Caveat: Tesseract's runtime assets (WASM core + language data) still load
  from a CDN on first OCR use — native-text PDFs are fully self-contained;
  full OCR vendoring can come later.
- `test_static.py` — 10/10 Selenium against the browser-only build (load,
  render, copy/paste flow, colour cycle, export with real annotations).

## Phase 5 — enterprise data-leak hardening
Driven by an enterprise requirement: nothing the user enters may leak.
- **Audit:** the only `fetch()` in the static app is the OpenRouter call
  (API mode only). Empirical capture of a full copy/paste session: every
  request went to the page's own host — zero third-party traffic.
- **Tesseract fully vendored:** worker, WASM core (SIMD/LSTM), and English
  language data live in `docs/vendor/tesseract/`. OCR of a scanned PDF makes
  zero external requests (verified by network capture on the patent).
- **Self-hosting guide:** `SELF-HOSTING.md` — how to run `docs/` on an
  internal server so even page load never contacts GitHub.
- API mode kept as-is (opt-in; the default copy/paste mode never uses it).
- Renamed to **ANNOT8 — crafted by Paul Fishwick & Claude Code**.
