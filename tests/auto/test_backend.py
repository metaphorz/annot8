"""
test_backend.py — end-to-end checks for the LLM PDF Annotator backend.

Run:  venv/bin/python tests/auto/test_backend.py
Covers: upload, page render, native-text annotation, OCR annotation
(on the scanned patent), tools/models endpoints.
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import pymupdf
import app as backend
import annotator

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {name}{(' — ' + extra) if extra else ''}")


def make_native_pdf():
    """Build a small PDF that has a real text layer."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.",
                     fontsize=14)
    page.insert_text((72, 140), "Patent claims describe a novel widget.",
                     fontsize=14)
    buf = doc.tobytes()
    doc.close()
    return buf


def main():
    client = backend.app.test_client()

    # ---- /api/tools ----
    r = client.get("/api/tools")
    tools = r.get_json()
    check("tools endpoint", r.status_code == 200 and "highlight" in tools["tools"])
    check("colors include yellow", "yellow" in tools["colors"])

    # ---- /api/models (network; tolerate failure) ----
    r = client.get("/api/models")
    mj = r.get_json()
    check("models endpoint returns list",
          r.status_code == 200 and len(mj["models"]) > 0,
          f"{len(mj['models'])} models")

    # ---- Upload native-text PDF ----
    pdf_bytes = make_native_pdf()
    r = client.post("/api/upload", data={
        "pdf": (io.BytesIO(pdf_bytes), "sample.pdf")},
        content_type="multipart/form-data")
    up = r.get_json()
    check("upload native PDF", r.status_code == 200 and up["pages"] == 1)
    doc_id = up["doc_id"]

    # ---- Page render ----
    r = client.get(f"/api/page/{doc_id}/1?zoom=1.5")
    check("page render returns PNG",
          r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n")

    # ---- Word extraction (native) ----
    words, is_ocr = backend.get_words(doc_id, 0)
    check("native text layer used (no OCR)", not is_ocr and len(words) > 5)

    # ---- Quote location ----
    quote = "quick brown fox jumps"
    quads, rect = annotator.find_quote_quads(words, quote)
    check("quote located on page", rect is not None and len(quads) >= 1)

    # ---- Annotate (each Acrobat-style tool) ----
    for tool in annotator.TOOLS:
        r = client.post("/api/annotate", json={
            "doc_id": doc_id, "page": 1, "quote": quote,
            "tool": tool, "color": "yellow", "comment": f"test {tool}"})
        j = r.get_json()
        check(f"annotate tool '{tool}'", r.status_code == 200 and j["located"])

    # ---- Verify annotations were written to the PDF ----
    saved = pymupdf.open(backend.work_path(doc_id))
    n_annots = len(list(saved[0].annots()))
    saved.close()
    check("annotations persisted to PDF", n_annots == len(annotator.TOOLS),
          f"{n_annots} annotations")

    # ---- Missing-quote handling ----
    r = client.post("/api/annotate", json={
        "doc_id": doc_id, "page": 1, "quote": "this text is absent xyzzy",
        "tool": "highlight", "color": "yellow", "comment": "x"})
    check("absent quote reported, not crashed",
          r.status_code == 200 and r.get_json()["located"] is False)

    # ---- Phase 2: background indexing + batch annotation ----
    import time
    complete = False
    for _ in range(20):
        st = client.get(f"/api/index-status/{doc_id}").get_json()
        if st["complete"]:
            complete = True
            break
        time.sleep(0.5)
    check("native PDF indexed in background", complete)

    r = client.post("/api/annotate-batch", json={
        "doc_id": doc_id, "tool": "highlight", "color": "blue",
        "links": [
            {"page": 1, "quote": "quick brown fox", "comment": "point one"},
            {"page": 1, "quote": "novel widget", "comment": "point two"},
            {"page": 1, "quote": "absent text qqq", "comment": "miss"},
        ]})
    bj = r.get_json()
    check("batch annotate applies found links",
          r.status_code == 200 and bj["applied"] == 2,
          f"applied {bj['applied']}/3")
    check("batch annotate reports the miss",
          any(x["located"] is False for x in bj["results"]))

    # ---- Copy/paste mode: build-prompt + parse-reply ----
    r = client.post("/api/build-prompt", json={
        "doc_id": doc_id, "page": 1, "scope": "page",
        "question": "What is the widget?"})
    bp = r.get_json()
    check("build-prompt (page) embeds page text + question",
          r.status_code == 200 and "quick brown fox" in bp["prompt"]
          and "What is the widget?" in bp["prompt"])

    r = client.post("/api/build-prompt", json={
        "doc_id": doc_id, "page": 1, "scope": "document",
        "question": "Summarise", "include_full_text": False})
    check("build-prompt (document, attach mode) tells user to attach PDF",
          "attached the PDF file" in r.get_json()["prompt"])

    r = client.post("/api/build-prompt", json={
        "doc_id": doc_id, "page": 1, "scope": "document",
        "question": "Summarise", "include_full_text": True})
    check("build-prompt (document, full-text mode) embeds document text",
          "quick brown fox" in r.get_json()["prompt"])

    canned = ('{"answer": "It is a widget.", "links": ['
              '{"point": "fox", "quote": "quick brown fox jumps"}]}')
    pr = client.post("/api/parse-reply", json={
        "scope": "page", "page": 1, "reply": canned}).get_json()
    check("parse-reply extracts answer + links",
          pr["answer"] == "It is a widget." and len(pr["links"]) == 1
          and pr["links"][0]["page"] == 1)

    # ---- OCR path on the scanned patent ----
    patent = os.path.join(BASE, "12629412.pdf")
    if os.path.exists(patent):
        with open(patent, "rb") as fh:
            r = client.post("/api/upload", data={
                "pdf": (io.BytesIO(fh.read()), "12629412.pdf")},
                content_type="multipart/form-data")
        pj = r.get_json()
        check("upload scanned patent", r.status_code == 200 and pj["pages"] == 304)
        pid = pj["doc_id"]

        print("    (OCR'ing patent page 1 — may take a few seconds...)")
        pwords, p_is_ocr = backend.get_words(pid, 0)
        check("patent page 1 used OCR", p_is_ocr and len(pwords) > 0,
              f"{len(pwords)} OCR words")

        if pwords:
            # Build a quote from 4 consecutive OCR words.
            ptext = annotator.page_plaintext(pwords)
            sample = " ".join(ptext.split()[:4])
            r = client.post("/api/annotate", json={
                "doc_id": pid, "page": 1, "quote": sample,
                "tool": "highlight", "color": "green",
                "comment": "OCR-anchored test"})
            j = r.get_json()
            check("annotate OCR'd patent page", j.get("located") is True,
                  f"quote='{sample}'")
    else:
        print("[SKIP] patent PDF not present")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
