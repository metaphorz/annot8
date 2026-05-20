# Self-hosting ANNOT8 (enterprise / no external contact)

The browser-only build of ANNOT8 lives in the [`docs/`](docs/) folder. It is
**fully self-contained** — pdf.js, pdf-lib, and the complete Tesseract.js OCR
engine (worker, WASM core, and English language data) are all vendored under
`docs/vendor/`.

Hosting `docs/` on an internal server means **even loading the page never
touches the public internet** — useful where you don't want GitHub (or any
outside host) to see that the tool is being used.

## What contacts the network

| Action | Network contact |
|---|---|
| Loading the page (self-hosted) | Your internal server only |
| Opening / rendering a PDF | None — read in-browser |
| OCR of a scanned PDF | None — engine + language data are vendored |
| Copy/paste mode (the default) | None — the prompt is shown for you to copy |
| Export annotated PDF | None — generated and downloaded locally |
| **OpenRouter API mode** *(opt-in, not the default)* | Sends text to `openrouter.ai` |

In the default **copy/paste** mode, after the page has loaded, ANNOT8 makes
**zero network requests**. Do not use *OpenRouter API mode* for proprietary
documents — copy/paste mode is the one designed for an approved internal LLM.

## How to host it

The `docs/` folder is plain static files. Serve it with any static web
server. It **must be served over HTTP(S)** — opening `index.html` directly
from `file://` will not work, because the app uses Web Workers (pdf.js and
Tesseract) that browsers only allow over http(s).

Copy the `docs/` folder to your server, then pick one:

**Quick internal server (Python):**
```sh
cd docs
python3 -m http.server 8080
# users open http://<your-host>:8080/
```

**nginx** — point a `location` / server root at the `docs/` folder:
```nginx
server {
    listen 8080;
    root /srv/annot8/docs;
    index index.html;
    types { application/wasm wasm; }   # serve .wasm with the right MIME
}
```

**IIS / Apache / SharePoint static hosting** — copy `docs/` into the site
root. Ensure `.wasm` is served as `application/wasm` (modern IIS and Apache
do this by default; if OCR is slow to start, add the MIME mapping).

## Verifying it for yourself

1. Open the self-hosted URL in a browser.
2. Open DevTools → **Network** tab.
3. Load a PDF and run a copy/paste question.
4. Confirm every request is to **your server only** — nothing else.

(For OCR: opening a *scanned* PDF will fetch `vendor/tesseract/*` from your
server — still your server, no external host.)

## Updating

To pull a newer ANNOT8 build, replace the `docs/` folder with the latest
version from the repository. No build step is required.
