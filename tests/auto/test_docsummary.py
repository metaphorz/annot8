"""
test_docsummary.py — fast, cheap end-to-end test of the whole-document
summary + batch-annotation flow, against a small native multi-page PDF.
Exercises all three configured models. Requires the server (./start).

Run:  venv/bin/python tests/auto/test_docsummary.py
"""
import os
import sys
import time

import requests

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTO = os.path.join(BASE, "tests", "auto")
SERVER = "http://127.0.0.1:5050"
SAMPLE = os.path.join(AUTO, "multipage.pdf")

PAGES = [
    ("Abstract", [
        "A thermal management system for electric-vehicle battery packs.",
        "The system maintains cell temperature within a target band using",
        "liquid-cooled plates bonded to the underside of each cell module."]),
    ("Background", [
        "Conventional air cooling cannot remove heat fast enough during",
        "fast charging, causing accelerated capacity loss and fire risk.",
        "Existing liquid systems add weight and leak at the plate joints."]),
    ("Detailed Description", [
        "The cooling plate contains serpentine microchannels that carry a",
        "dielectric coolant pumped by a variable-speed electric pump.",
        "A controller adjusts pump speed based on the hottest measured cell."]),
    ("Claims", [
        "1. A battery thermal system comprising liquid-cooled plates with",
        "serpentine microchannels and a variable-speed dielectric coolant pump.",
        "2. The system of claim 1 wherein a controller targets the hottest cell."]),
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def build_pdf():
    import pymupdf
    doc = pymupdf.open()
    for title, lines in PAGES:
        page = doc.new_page()
        page.insert_text((72, 90), title, fontsize=16)
        y = 130
        for ln in lines:
            page.insert_text((72, y), ln, fontsize=12)
            y += 26
    doc.save(SAMPLE)
    doc.close()


def main():
    build_pdf()
    models = [m["id"] for m in requests.get(f"{SERVER}/api/models").json()["models"]]
    log(f"models: {models}")

    failures = 0
    for model in models:
        log(f"==== model: {model} ====")
        with open(SAMPLE, "rb") as fh:
            up = requests.post(f"{SERVER}/api/upload",
                               files={"pdf": ("multipage.pdf", fh)}).json()
        doc_id = up["doc_id"]

        # Native PDF — indexing is near-instant; wait briefly.
        for _ in range(20):
            if requests.get(f"{SERVER}/api/index-status/{doc_id}").json()["complete"]:
                break
            time.sleep(0.3)

        chat = requests.post(f"{SERVER}/api/chat", json={
            "doc_id": doc_id, "model": model, "scope": "document",
            "messages": [{"role": "user",
                          "content": "Give me a 2 paragraph summary of this "
                                     "document."}]}, timeout=300).json()
        if chat.get("error"):
            log(f"  CHAT ERROR: {chat['error']}")
            failures += 1
            continue

        links = chat.get("links", [])
        log(f"  answer: {chat['answer'][:120]}...")
        log(f"  {len(links)} key points linked")
        for ln in links:
            log(f"    p.{ln.get('page')}: {ln.get('quote','')[:55]!r}")

        if not links:
            log("  WARNING: no links returned")
            failures += 1
            continue

        res = requests.post(f"{SERVER}/api/annotate-batch", json={
            "doc_id": doc_id, "tool": "highlight", "color": "yellow",
            "links": [{"page": ln["page"], "quote": ln["quote"],
                       "comment": ln.get("point", "")} for ln in links]}).json()
        log(f"  annotated {res['applied']}/{len(links)} key points")
        for x in res["results"]:
            log(f"    [{'OK' if x['located'] else 'MISS'}] p.{x['page']}: "
                f"{x['quote'][:50]!r}")
        if res["applied"] == 0:
            failures += 1

    log(f"\nDONE — {failures} model(s) with problems")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
