"""
test_summary.py — live end-to-end test of the whole-document summary flow
against the running server (./start must be up).

Uploads the scanned patent, waits for background OCR indexing of all pages,
asks for a 2-paragraph summary in 'document' scope, then batch-annotates the
key points. Run:  venv/bin/python tests/auto/test_summary.py
"""
import os
import sys
import time

import requests

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = "http://127.0.0.1:5050"
PATENT = os.path.join(BASE, "12629412.pdf")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    # ---- Upload ----
    log("Uploading patent...")
    with open(PATENT, "rb") as fh:
        up = requests.post(f"{SERVER}/api/upload",
                           files={"pdf": ("12629412.pdf", fh)}).json()
    doc_id = up["doc_id"]
    log(f"Uploaded: doc_id={doc_id}, {up['pages']} pages")

    # ---- Wait for background OCR indexing of all pages ----
    log("Indexing (OCR) all pages — monitoring progress...")
    start = time.time()
    while True:
        s = requests.get(f"{SERVER}/api/index-status/{doc_id}").json()
        log(f"  indexed {s['done']}/{s['total']} pages")
        if s["complete"]:
            break
        time.sleep(15)
    log(f"Indexing complete in {time.time() - start:.0f}s")

    # ---- Pick a large-context model (Gemini) ----
    models = requests.get(f"{SERVER}/api/models").json()["models"]
    google = [m for m in models if m["provider"] == "Google"]
    model = google[0]["id"] if google else models[0]["id"]
    log(f"Using model: {model}")

    # ---- Ask for a 2-paragraph whole-document summary ----
    log("Requesting 2-paragraph document summary...")
    chat = requests.post(f"{SERVER}/api/chat", json={
        "doc_id": doc_id, "model": model, "scope": "document",
        "messages": [{"role": "user",
                      "content": "Give me a 2 paragraph summary of this "
                                 "patent."}]}, timeout=300).json()
    if chat.get("error"):
        log(f"CHAT ERROR: {chat['error']}")
        sys.exit(1)
    log("--- SUMMARY ---")
    print(chat["answer"], flush=True)
    links = chat.get("links", [])
    log(f"{len(links)} key points linked:")
    for ln in links:
        print(f"  p.{ln.get('page')}: {ln.get('point','')!r} "
              f"<- {ln.get('quote','')!r}", flush=True)

    # ---- Batch-annotate the key points ----
    if links:
        log("Batch-annotating key points...")
        payload = [{"page": ln["page"], "quote": ln["quote"],
                    "comment": ln.get("point", "")} for ln in links]
        res = requests.post(f"{SERVER}/api/annotate-batch", json={
            "doc_id": doc_id, "tool": "highlight", "color": "yellow",
            "links": payload}).json()
        log(f"Annotated {res['applied']}/{len(links)} key points")
        for x in res["results"]:
            mark = "OK  " if x["located"] else "MISS"
            log(f"  [{mark}] p.{x['page']}: {x['quote'][:60]!r}")
        log(f"Export URL: {SERVER}/api/export/{doc_id}")

    log("DONE")


if __name__ == "__main__":
    main()
