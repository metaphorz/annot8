"""
test_static.py — Selenium test for the static (browser-only) build in docs/.
Requires a static server on http://localhost:8000 serving docs/:

    venv/bin/python -m http.server 8000 --directory docs

Run:  venv/bin/python tests/auto/test_static.py

Exercises the no-backend pipeline: load PDF, render, copy/paste flow with a
canned reply, auto-annotation, colour cycling, and export.
"""
import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTO = os.path.join(BASE, "tests", "auto")
URL = "http://localhost:8000/"
SAMPLE = os.path.join(AUTO, "sample.pdf")

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          f"{(' — ' + extra) if extra else ''}", flush=True)


def ensure_sample():
    if os.path.exists(SAMPLE):
        return
    import pymupdf
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 100),
                  "A widget comprises a rotating disc and a magnetic clamp.",
                  fontsize=13)
    p.insert_text((72, 130),
                  "The clamp secures the disc during high-speed rotation.",
                  fontsize=13)
    d.save(SAMPLE)
    d.close()


def main():
    ensure_sample()
    dldir = os.path.join(AUTO, "downloads")
    os.makedirs(dldir, exist_ok=True)
    for f in os.listdir(dldir):
        os.remove(os.path.join(dldir, f))

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1500,950")
    opts.add_experimental_option("prefs", {
        "download.default_directory": dldir,
        "download.prompt_for_download": False,
    })
    driver = webdriver.Chrome(options=opts)
    wait = WebDriverWait(driver, 30)

    try:
        driver.get(URL)
        wait.until(lambda d: len(d.find_elements(
            By.CSS_SELECTOR, "#toolSelect option")) == 10)
        check("static page loads, 10 tools populated", True)

        # No JavaScript errors on load (favicon 404 is harmless).
        errors = [e for e in driver.get_log("browser")
                  if e["level"] == "SEVERE" and "favicon" not in e["message"]]
        check("no severe JS errors on load", len(errors) == 0,
              "; ".join(e["message"][:90] for e in errors[:2]))
        driver.save_screenshot(os.path.join(AUTO, "static_01_loaded.png"))

        # ---- Load a PDF (stays in the browser) ----
        driver.find_element(By.ID, "fileInput").send_keys(SAMPLE)
        wait.until(lambda d: d.execute_script(
            "return document.getElementById('pageCanvas').width > 0"))
        check("PDF rendered to canvas", True)
        driver.save_screenshot(os.path.join(AUTO, "static_02_rendered.png"))

        # ---- Copy/paste flow with two canned replies (colour cycle) ----
        canned = [
            ('{"answer": "A widget with a magnetic clamp.", "links": ['
             '{"point": "clamp", "quote": "The clamp secures the disc '
             'during high-speed rotation", "page": 1}]}'),
            ('{"answer": "It has a rotating disc.", "links": ['
             '{"point": "disc", "quote": "A widget comprises a rotating '
             'disc", "page": 1}]}'),
        ]
        for n, reply in enumerate(canned, 1):
            driver.find_element(By.ID, "chatInput").send_keys(f"Question {n}?")
            driver.find_element(By.ID, "sendBtn").click()
            WebDriverWait(driver, 20).until(lambda d: len(d.find_elements(
                By.CSS_SELECTOR, ".prompt-card")) >= n)
            card = driver.find_elements(By.CSS_SELECTOR, ".prompt-card")[n - 1]
            prompt = card.find_element(
                By.CSS_SELECTOR, ".pc-prompt").get_attribute("value")
            check(f"prompt built client-side (turn {n})",
                  len(prompt) > 50 and "JSON" in prompt)
            card.find_element(By.CSS_SELECTOR, ".pc-reply").send_keys(reply)
            card.find_element(By.CSS_SELECTOR, ".pc-btn.primary").click()
            WebDriverWait(driver, 30).until(
                lambda d: len(d.find_elements(
                    By.CSS_SELECTOR, ".annotate-status")) >= n
                and "annotated" in d.find_elements(
                    By.CSS_SELECTOR, ".annotate-status")[n - 1].text.lower())

        texts = [s.text.lower() for s in
                 driver.find_elements(By.CSS_SELECTOR, ".annotate-status")]
        check("client-side annotation applied",
              len(texts) == 2 and all("annotated" in t for t in texts),
              " | ".join(texts))
        check("annotation colour cycles per question",
              "yellow" in texts[0] and "green" in texts[1],
              " ; ".join(texts))
        check("annotation count shown",
              "annotation" in driver.find_element(By.ID, "annCount").text,
              driver.find_element(By.ID, "annCount").text)
        driver.save_screenshot(os.path.join(AUTO, "static_03_annotated.png"))

        # ---- Export produces an annotated PDF download ----
        driver.find_element(By.ID, "exportBtn").click()
        deadline = time.time() + 20
        out = None
        while time.time() < deadline:
            done = [f for f in os.listdir(dldir) if f.endswith(".pdf")]
            if done:
                out = os.path.join(dldir, done[0])
                break
            time.sleep(0.5)
        check("export downloads an annotated PDF", out is not None)
        if out:
            import pymupdf
            d = pymupdf.open(out)
            n = sum(len(list(d[i].annots())) for i in range(d.page_count))
            d.close()
            check("exported PDF carries the annotations", n == 2,
                  f"{n} annotations")

    finally:
        driver.quit()

    print(f"\n{PASS} passed, {FAIL} failed", flush=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
