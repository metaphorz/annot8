"""
app.py — Flask backend for the LLM-Annotated PDF tool.

Conversation with an LLM (via OpenRouter) about a PDF, with answers
linked back into the PDF as Adobe Acrobat-style annotations.

By Paul Fishwick and Claude Code.
"""
import os
import re
import json
import uuid
import shutil
import hashlib
import threading

import requests
import pymupdf
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file, send_from_directory

import annotator

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
load_dotenv(os.path.expanduser("~/.env"))
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1"

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")    # untouched originals
WORK_DIR = os.path.join(BASE, "outputs")      # working / annotated copies
WC_DIR = os.path.join(WORK_DIR, "wordcache")  # cached OCR/word index by hash
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)
os.makedirs(WC_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

DOCS = {}            # doc_id -> {name, pages, ocr_done, ocr_complete}
WORD_CACHE = {}      # (doc_id, page_idx) -> (words, is_ocr)


def work_path(doc_id):
    """The working / annotated copy (modified as annotations are added)."""
    return os.path.join(WORK_DIR, f"{doc_id}.pdf")


def orig_path(doc_id):
    """The untouched original — used for text/word extraction so indexing
    never collides with annotation writes to the working copy."""
    return os.path.join(UPLOAD_DIR, f"{doc_id}.pdf")


def file_hash(path):
    """SHA-1 of a file's bytes — identifies identical PDFs across uploads."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_words(doc_id, page_idx):
    """Return (words, is_ocr) for a 0-based page, cached. Native text
    layer first, OCR fallback for image-only pages."""
    key = (doc_id, page_idx)
    if key not in WORD_CACHE:
        doc = pymupdf.open(orig_path(doc_id))
        WORD_CACHE[key] = annotator.extract_words(doc[page_idx])
        doc.close()
    return WORD_CACHE[key]


def index_document(doc_id):
    """Background worker: extract words for every page (native text where
    present, OCR otherwise) so whole-document summaries have full text.
    The result is cached to disk by file hash, so re-uploading the same
    PDF (or restarting the server) skips the slow OCR pass."""
    sha = DOCS[doc_id]["sha"]
    cache = os.path.join(WC_DIR, f"{sha}.json")

    if os.path.exists(cache):
        with open(cache) as f:
            data = json.load(f)
        for i, (words, is_ocr) in enumerate(data):
            WORD_CACHE[(doc_id, i)] = ([tuple(w) for w in words], is_ocr)
        DOCS[doc_id]["ocr_total"] = len(data)
        DOCS[doc_id]["ocr_done"] = len(data)
        DOCS[doc_id]["ocr_complete"] = True
        return

    doc = pymupdf.open(orig_path(doc_id))
    total = doc.page_count
    DOCS[doc_id]["ocr_total"] = total
    for i in range(total):
        key = (doc_id, i)
        if key not in WORD_CACHE:
            WORD_CACHE[key] = annotator.extract_words(doc[i])
        DOCS[doc_id]["ocr_done"] = i + 1
    doc.close()

    data = [[[list(w) for w in WORD_CACHE[(doc_id, i)][0]],
             WORD_CACHE[(doc_id, i)][1]] for i in range(total)]
    with open(cache, "w") as f:
        json.dump(data, f)
    DOCS[doc_id]["ocr_complete"] = True


def full_text(doc_id):
    """Whole-document plain text with [PAGE n] markers."""
    parts = []
    for i in range(DOCS[doc_id]["pages"]):
        words, _ = get_words(doc_id, i)
        parts.append(f"[PAGE {i + 1}]\n{annotator.page_plaintext(words)}")
    return "\n\n".join(parts)


def locate(doc_id, page_hint, quote, min_score=0.55):
    """Find `quote` anywhere in the document. The LLM's `page_hint` is only
    a hint (its page numbers drift), so every page is fuzzy-matched and the
    best scorer wins, with the hint used only to break ties.
    Returns (found_page, quads, rect) or (None, None, None)."""
    best = None  # ((score, -hint_distance), page, quads, rect)
    for p in range(1, DOCS[doc_id]["pages"] + 1):
        words, _ = get_words(doc_id, p - 1)
        score, quads, rect = annotator.match_quote(words, quote)
        if rect is None:
            continue
        key = (score, -abs(p - page_hint))
        if best is None or key > best[0]:
            best = (key, p, quads, rect)
    if best and best[0][0] >= min_score:
        return best[1], best[2], best[3]
    return None, None, None


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# --------------------------------------------------------------------------
# PDF upload / serving
# --------------------------------------------------------------------------
@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify(error="Please supply a .pdf file"), 400

    doc_id = uuid.uuid4().hex[:12]
    original = os.path.join(UPLOAD_DIR, f"{doc_id}.pdf")
    f.save(original)
    shutil.copyfile(original, work_path(doc_id))  # working copy to annotate

    try:
        doc = pymupdf.open(work_path(doc_id))
        pages = doc.page_count
        doc.close()
    except Exception as e:
        return jsonify(error=f"Could not open PDF: {e}"), 400

    DOCS[doc_id] = {"name": f.filename, "pages": pages,
                    "sha": file_hash(original),
                    "ocr_done": 0, "ocr_total": pages, "ocr_complete": False}
    # Index the whole document in the background (instant for native-text
    # PDFs or a cache hit, minutes for a fresh large scanned PDF).
    threading.Thread(target=index_document, args=(doc_id,),
                     daemon=True).start()
    return jsonify(doc_id=doc_id, name=f.filename, pages=pages)


@app.route("/api/index-status/<doc_id>")
def index_status(doc_id):
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    d = DOCS[doc_id]
    return jsonify(done=d["ocr_done"], total=d["ocr_total"],
                   complete=d["ocr_complete"])


@app.route("/api/pdf/<doc_id>")
def serve_pdf(doc_id):
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    # no-cache so the viewer always re-fetches after a new annotation
    resp = send_file(work_path(doc_id), mimetype="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/page/<doc_id>/<int:page>")
def render_page(doc_id, page):
    """Render a 1-based page to PNG, with annotations baked in."""
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    zoom = float(request.args.get("zoom", 1.5))
    doc = pymupdf.open(work_path(doc_id))
    pix = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    png = pix.tobytes("png")
    doc.close()
    resp = app.response_class(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/export/<doc_id>")
def export_pdf(doc_id):
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    name = DOCS[doc_id]["name"].rsplit(".", 1)[0]
    return send_file(work_path(doc_id), mimetype="application/pdf",
                     as_attachment=True,
                     download_name=f"{name}_annotated.pdf")


# --------------------------------------------------------------------------
# Model list — the three OpenRouter models chosen for this tool
# (latest chat model from each of Anthropic, OpenAI, Google).
# --------------------------------------------------------------------------
MODELS = [
    {"id": "anthropic/claude-opus-4.7-fast",
     "name": "Claude Opus 4.7 Fast", "provider": "Anthropic"},
    {"id": "openai/gpt-chat-latest",
     "name": "GPT (chat latest)", "provider": "OpenAI"},
    {"id": "google/gemini-3.1-pro-preview",
     "name": "Gemini 3.1 Pro", "provider": "Google"},
]


@app.route("/api/models")
def models():
    return jsonify(models=MODELS)


# --------------------------------------------------------------------------
# Chat — OpenRouter proxy with structured-answer prompting
# --------------------------------------------------------------------------
PAGE_PROMPT = """You are helping a user analyse a PDF document.
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
Include 0 or 1 link. The quote must be copied character-for-character from \
the page text so it can be located. Keep it under 25 words."""

DOC_PROMPT = """You are analysing a complete PDF document. The user message \
context contains the WHOLE document text, with [PAGE n] markers before each \
page. Answer the user's request (for example, a summary).

Reply ONLY with a JSON object, no other text, in this exact form:
{
  "answer": "<your full answer / summary>",
  "links": [
    {"point": "<one key point of your answer, one sentence>",
     "quote": "<a short VERBATIM passage copied exactly from the document \
that supports this point>",
     "page": <the [PAGE n] number where that passage appears>}
  ]
}
Provide one link per key point (typically 4-8 links). Every quote must be \
copied character-for-character from the document text and be under 25 \
words, so it can be located and annotated."""


def _extract_json(text):
    """Pull the first JSON object out of a model reply."""
    if not text:
        return {"answer": "", "links": []}
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"answer": text.strip(), "links": []}


def assemble_context(doc_id, page, scope, include_full_text):
    """Return (system_prompt, context_block) for a question.

    For document scope, `include_full_text` decides whether the whole
    document text is embedded (always so for the API; optional in
    copy/paste mode, where the user may instead attach the PDF file)."""
    if scope == "document":
        if include_full_text:
            context = (f"--- FULL DOCUMENT ({DOCS[doc_id]['pages']} pages) ---"
                       f"\n{full_text(doc_id)}\n--- END DOCUMENT ---")
        else:
            context = (f'The user has attached the PDF file '
                       f'"{DOCS[doc_id]["name"]}" ({DOCS[doc_id]["pages"]} '
                       f'pages) to this conversation — read it to answer.')
        return DOC_PROMPT, context
    words, is_ocr = get_words(doc_id, page - 1)
    context = (f"--- PAGE {page} of {DOCS[doc_id]['pages']} "
               f"({'OCR text' if is_ocr else 'native text'}) ---\n"
               f"{annotator.page_plaintext(words)}\n--- END PAGE ---")
    return PAGE_PROMPT, context


def _links_from_reply(reply, scope, page):
    """Parse a model reply into (answer, links), dropping quote-less links."""
    parsed = _extract_json(reply)
    links = parsed.get("links") or []
    if scope == "page":
        for ln in links:
            ln["page"] = page
    links = [ln for ln in links if (ln.get("quote") or "").strip()]
    return parsed.get("answer", reply), links


def _indexing_error(doc_id):
    """409 response if a document-scope request needs the not-yet-ready index."""
    d = DOCS[doc_id]
    return jsonify(error=(f"Document still indexing "
                          f"({d['ocr_done']}/{d['ocr_total']} pages). "
                          f"Try again shortly.")), 409


@app.route("/api/build-prompt", methods=["POST"])
def build_prompt():
    """Copy/paste mode: build a self-contained prompt the user pastes into
    their own (company-approved) LLM chat."""
    body = request.get_json(force=True)
    doc_id = body.get("doc_id")
    page = int(body.get("page", 1))
    scope = body.get("scope", "page")
    question = (body.get("question") or "").strip()
    include_full = bool(body.get("include_full_text", False))
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    if scope == "document" and include_full and not DOCS[doc_id]["ocr_complete"]:
        return _indexing_error(doc_id)
    system, context = assemble_context(doc_id, page, scope, include_full)
    prompt = f"{system}\n\n{context}\n\nQUESTION: {question}"
    return jsonify(prompt=prompt)


@app.route("/api/parse-reply", methods=["POST"])
def parse_reply():
    """Copy/paste mode: turn the LLM reply the user pasted back into
    {answer, links} for annotation — no LLM call."""
    body = request.get_json(force=True)
    scope = body.get("scope", "page")
    page = int(body.get("page", 1))
    answer, links = _links_from_reply(body.get("reply", ""), scope, page)
    return jsonify(answer=answer, links=links, scope=scope, page=page)


@app.route("/api/chat", methods=["POST"])
def chat():
    """OpenRouter API mode: call the model directly."""
    body = request.get_json(force=True)
    api_key = (body.get("api_key") or OPENROUTER_KEY or "").strip()
    if not api_key:
        return jsonify(error="No OpenRouter API key provided."), 400

    doc_id = body.get("doc_id")
    model = body.get("model")
    page = int(body.get("page", 1))            # 1-based
    scope = body.get("scope", "page")          # "page" | "document"
    history = body.get("messages", [])         # [{role, content}, ...]
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    if scope == "document" and not DOCS[doc_id]["ocr_complete"]:
        return _indexing_error(doc_id)

    system, context = assemble_context(doc_id, page, scope, True)
    messages = [
        {"role": "system", "content": system},
        {"role": "system", "content": context},
    ] + history

    try:
        r = requests.post(
            f"{OPENROUTER_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_tokens": 16000},
            timeout=300,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        detail = getattr(e, "response", None)
        msg = detail.text if detail is not None else str(e)
        return jsonify(error=f"OpenRouter call failed: {msg}"), 502

    # Robustly pull the text out — some (thinking) models return content
    # null with the text under 'reasoning', or no text at all.
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    reply = (message.get("content") or message.get("reasoning") or "").strip()
    if not reply:
        fr = choice.get("finish_reason", "unknown")
        return jsonify(error=(f"The model returned no text "
                              f"(finish_reason={fr}). Try another model.")), 502

    answer, links = _links_from_reply(reply, scope, page)
    return jsonify(answer=answer, links=links, scope=scope, page=page)


# --------------------------------------------------------------------------
# Annotation
# --------------------------------------------------------------------------
@app.route("/api/tools")
def tools():
    return jsonify(tools=annotator.TOOLS, colors=list(annotator.COLORS))


@app.route("/api/annotate", methods=["POST"])
def annotate():
    body = request.get_json(force=True)
    doc_id = body.get("doc_id")
    page = int(body.get("page", 1))            # 1-based
    quote = (body.get("quote") or "").strip()
    tool = body.get("tool", "highlight")
    color = body.get("color", "yellow")
    comment = body.get("comment", "")
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404
    if not quote:
        return jsonify(error="no source quote to anchor the annotation"), 400

    words, _ = get_words(doc_id, page - 1)
    quads, rect = annotator.find_quote_quads(words, quote)
    if rect is None:
        return jsonify(error="could not locate that passage on the page",
                       located=False), 200

    doc = pymupdf.open(work_path(doc_id))
    annotator.apply_annotation(doc[page - 1], quads, rect, tool, color,
                               comment, author="LLM Annotation")
    doc.save(work_path(doc_id), incremental=True,
             encryption=pymupdf.PDF_ENCRYPT_KEEP)
    doc.close()

    return jsonify(located=True, page=page, tool=tool,
                   rect=[rect.x0, rect.y0, rect.x1, rect.y1])


@app.route("/api/annotate-batch", methods=["POST"])
def annotate_batch():
    """Apply several annotations from one answer (e.g. each key point of a
    document summary). Each link: {page, quote, comment}."""
    body = request.get_json(force=True)
    doc_id = body.get("doc_id")
    tool = body.get("tool", "highlight")
    color = body.get("color", "yellow")
    links = body.get("links", [])
    if doc_id not in DOCS:
        return jsonify(error="unknown doc"), 404

    doc = pymupdf.open(work_path(doc_id))
    results, applied = [], 0
    for ln in links:
        quote = (ln.get("quote") or "").strip()
        comment = ln.get("comment", "")
        page = int(ln.get("page", 1))
        found, quads, rect = (locate(doc_id, page, quote)
                              if quote else (None, None, None))
        if rect is None:
            results.append({"located": False, "quote": quote, "page": page})
            continue
        annotator.apply_annotation(doc[found - 1], quads, rect, tool, color,
                                   comment, author="LLM Annotation")
        applied += 1
        results.append({"located": True, "quote": quote, "page": found})

    if applied:
        doc.save(work_path(doc_id), incremental=True,
                 encryption=pymupdf.PDF_ENCRYPT_KEEP)
    doc.close()
    return jsonify(results=results, applied=applied)


if __name__ == "__main__":
    print(f"OpenRouter key: {'loaded' if OPENROUTER_KEY else 'MISSING'}")
    app.run(host="127.0.0.1", port=5050, debug=True, threaded=True)
