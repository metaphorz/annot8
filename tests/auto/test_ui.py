"""
test_ui.py — Selenium UI test for ANNOT8.
Requires the server running (./start).

Drives the browser: load page, upload a PDF, render, run a page-scope
chat (real OpenRouter call), and apply an annotation. Screenshots are
saved to tests/auto/ui_*.png. Run:

    venv/bin/python tests/auto/test_ui.py
"""
import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTO = os.path.join(BASE, "tests", "auto")
URL = "http://127.0.0.1:5050"
SAMPLE = os.path.join(AUTO, "sample.pdf")

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          f"{(' — ' + extra) if extra else ''}", flush=True)


def ensure_sample():
    """Make a small native-text PDF if one is not already present."""
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


def shot(driver, name):
    path = os.path.join(AUTO, f"ui_{name}.png")
    driver.save_screenshot(path)
    print(f"    screenshot: {path}", flush=True)


def main():
    ensure_sample()

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1500,950")
    driver = webdriver.Chrome(options=opts)
    wait = WebDriverWait(driver, 30)

    try:
        # ---- Load page ----
        driver.get(URL)
        wait.until(EC.presence_of_element_located((By.ID, "dropZone")))
        wait.until(lambda d: len(d.find_elements(
            By.CSS_SELECTOR, "#toolSelect option")) == 10)
        check("page loads, annotation tools populated", True)
        # Copy/paste is the default LLM-access mode.
        from selenium.webdriver.support.ui import Select
        check("default mode is copy/paste",
              driver.find_element(By.ID, "modeSelect")
              .get_attribute("value") == "copypaste")
        shot(driver, "01_loaded")

        # ---- Upload PDF via the (hidden) file input ----
        driver.find_element(By.ID, "fileInput").send_keys(SAMPLE)
        wait.until(EC.visibility_of_element_located((By.ID, "pageArea")))
        wait.until(lambda d: d.execute_script(
            "return document.getElementById('pageImg').naturalWidth > 0"))
        check("PDF uploaded and page rendered", True)
        shot(driver, "02_uploaded")

        # ---- Copy/paste mode: build a prompt, paste a canned reply ----
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
            check(f"copy/paste prompt built (turn {n})",
                  len(prompt) > 50 and "JSON" in prompt)
            card.find_element(By.CSS_SELECTOR, ".pc-reply").send_keys(reply)
            card.find_element(By.CSS_SELECTOR, ".pc-btn.primary").click()
            WebDriverWait(driver, 20).until(
                lambda d: len(d.find_elements(
                    By.CSS_SELECTOR, ".annotate-status")) >= n
                and "annotated" in d.find_elements(
                    By.CSS_SELECTOR, ".annotate-status")[n - 1].text.lower())
        shot(driver, "03_copypaste")

        texts = [s.text.lower() for s in
                 driver.find_elements(By.CSS_SELECTOR, ".annotate-status")]
        check("copy/paste replies auto-annotated",
              len(texts) == 2 and all("annotated" in t for t in texts),
              " | ".join(texts))
        check("annotation colour cycles per question",
              "yellow" in texts[0] and "green" in texts[1],
              " ; ".join(texts))

        # ---- Switch to OpenRouter API mode and ask a real question ----
        Select(driver.find_element(By.ID, "modeSelect")).select_by_value("api")
        driver.find_element(By.ID, "chatInput").send_keys(
            "What does the clamp do?")
        driver.find_element(By.ID, "sendBtn").click()
        WebDriverWait(driver, 120).until(lambda d: d.find_elements(
            By.CSS_SELECTOR, ".msg.bot:not(.prompt-card)"))
        WebDriverWait(driver, 30).until(
            lambda d: len(d.find_elements(
                By.CSS_SELECTOR, ".annotate-status")) >= 3)
        api_status = driver.find_elements(
            By.CSS_SELECTOR, ".annotate-status")[2].text.lower()
        check("OpenRouter API mode answers + annotates",
              "annotated" in api_status, api_status)
        check("running annotation count shown",
              "annotation" in driver.find_element(By.ID, "annCount").text,
              driver.find_element(By.ID, "annCount").text)
        shot(driver, "04_annotated")

    finally:
        driver.quit()

    print(f"\n{PASS} passed, {FAIL} failed", flush=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
