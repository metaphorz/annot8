"""
annotator.py — PDF text extraction (native + OCR fallback) and the
Adobe Acrobat-style annotation engine, built on PyMuPDF.

By Paul Fishwick and Claude Code.
"""
import os
import re
from collections import Counter

import pymupdf

# PyMuPDF's OCR needs Tesseract's tessdata directory.
for _d in ("/opt/homebrew/share/tessdata",
           "/opt/homebrew/opt/tesseract/share/tessdata",
           "/usr/local/share/tessdata"):
    if os.path.isdir(_d):
        os.environ.setdefault("TESSDATA_PREFIX", _d)
        break

# A "word" tuple from PyMuPDF get_text("words"):
#   (x0, y0, x1, y1, "word", block_no, line_no, word_no)

OCR_DPI = 300


# --------------------------------------------------------------------------
# Text / word extraction
# --------------------------------------------------------------------------
def extract_words(page):
    """Return (words, is_ocr) for a page.

    Uses the native PDF text layer when present; falls back to OCR
    (Tesseract via PyMuPDF) for image-only pages.
    """
    words = page.get_text("words")
    if words:
        return words, False
    # No text layer — OCR this page.
    ocr_tp = page.get_textpage_ocr(flags=0, dpi=OCR_DPI, full=True)
    words = page.get_text("words", textpage=ocr_tp)
    return words, True


def page_plaintext(words):
    """Join a page's word tuples into readable text (one space per word,
    newline per text line)."""
    lines = {}
    for w in words:
        key = (w[5], w[6])  # (block, line)
        lines.setdefault(key, []).append(w)
    out = []
    for key in sorted(lines):
        line_words = sorted(lines[key], key=lambda w: w[7])
        out.append(" ".join(w[4] for w in line_words))
    return "\n".join(out)


# --------------------------------------------------------------------------
# Quote location  (fuzzy — tolerant of OCR noise and LLM copy drift)
# --------------------------------------------------------------------------
def _norm(token):
    """Lowercase and strip non-alphanumeric chars for tolerant matching."""
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _norm_tokens(words):
    """[(normalized_token, word_index)] for words with non-empty norm form."""
    out = []
    for i, w in enumerate(words):
        n = _norm(w[4])
        if n:
            out.append((n, i))
    return out


def _quads_for_range(words, first, last):
    """One quad per text line spanned by words[first..last]; + bounding rect."""
    by_line = {}
    for w in words[first:last + 1]:
        by_line.setdefault((w[5], w[6]), []).append(w)
    quads, full = [], None
    for key in sorted(by_line):
        lw = by_line[key]
        r = pymupdf.Rect(min(w[0] for w in lw), min(w[1] for w in lw),
                          max(w[2] for w in lw), max(w[3] for w in lw))
        quads.append(r.quad)
        full = r if full is None else (full | r)
    return quads, full


_CLUSTER_GAP = 3   # max gap (in content tokens) within one highlight cluster


def match_quote(words, quote):
    """Best fuzzy match of `quote` within one page's words.

    Anchors on *content* tokens (>= 3 chars — short stopwords like "to",
    "in", "a" and single-character OCR noise are ignored) and slides a
    window the size of the quote, scoring by multiset overlap. The match
    survives OCR errors and word splits/merges; the highlight is then
    trimmed to the densest cluster of matched words so it stays tight.
    Returns (score, quads, rect); score in [0, 1].
    """
    # [(normalized_token, word_index)] keeping only distinctive tokens.
    page = [(t, wi) for t, wi in _norm_tokens(words) if len(t) >= 3]
    q = [t for t in (_norm(t) for t in quote.split()) if len(t) >= 3]
    if not page or not q:
        return 0.0, [], None

    ptok = [t for t, _ in page]
    n = len(ptok)
    win = min(len(q), n)
    qcount = Counter(q)

    def overlap(wc):
        return sum(min(wc[t], qcount[t]) for t in qcount)

    wcount = Counter(ptok[:win])
    best_score, best_start = overlap(wcount), 0
    for s in range(1, n - win + 1):
        wcount[ptok[s - 1]] -= 1
        wcount[ptok[s + win - 1]] += 1
        o = overlap(wcount)
        if o > best_score:
            best_score, best_start = o, s

    # Positions inside the winning window that belong to the quote.
    qset = set(q)
    hits = [k for k in range(best_start, best_start + win) if ptok[k] in qset]
    if not hits:
        return 0.0, [], None

    # Split hits into clusters and keep the densest one, so a few stray
    # matches elsewhere on the page don't stretch the highlight.
    clusters, cur = [], [hits[0]]
    for h in hits[1:]:
        if h - cur[-1] <= _CLUSTER_GAP:
            cur.append(h)
        else:
            clusters.append(cur)
            cur = [h]
    clusters.append(cur)
    cluster = max(clusters, key=len)

    first_wi = page[cluster[0]][1]
    last_wi = page[cluster[-1]][1]
    if last_wi < first_wi:
        first_wi, last_wi = last_wi, first_wi
    quads, rect = _quads_for_range(words, first_wi, last_wi)
    return best_score / len(q), quads, rect


def find_quote_quads(words, quote, min_score=0.6):
    """Locate `quote` on a single page. Returns (quads, rect) or ([], None)."""
    score, quads, rect = match_quote(words, quote)
    return (quads, rect) if score >= min_score else ([], None)


# --------------------------------------------------------------------------
# Annotation engine — Adobe Acrobat-style tools
# --------------------------------------------------------------------------
COLORS = {
    "yellow": (1.0, 0.92, 0.23),
    "green":  (0.55, 0.90, 0.40),
    "blue":   (0.45, 0.74, 1.0),
    "pink":   (1.0, 0.55, 0.78),
    "orange": (1.0, 0.70, 0.28),
    "red":    (0.95, 0.35, 0.35),
}

# Annotation tools mirrored from Adobe Acrobat's comment/markup set.
TOOLS = [
    "highlight", "underline", "strikeout", "squiggly",
    "sticky-note", "text-box", "callout", "rectangle", "oval", "arrow",
]


def apply_annotation(page, quads, rect, tool, color, comment, author):
    """Add one annotation of the given Acrobat-style `tool` to `page`,
    anchored to the located quote (`quads` / `rect`). `comment` is the
    LLM answer, stored as the annotation's popup content / displayed text.

    Returns the pymupdf.Annot created.
    """
    rgb = COLORS.get(color, COLORS["yellow"])

    if tool in ("highlight", "underline", "strikeout", "squiggly"):
        adder = {
            "highlight": page.add_highlight_annot,
            "underline": page.add_underline_annot,
            "strikeout": page.add_strikeout_annot,
            "squiggly":  page.add_squiggly_annot,
        }[tool]
        annot = adder(quads)
        annot.set_colors(stroke=rgb)
        annot.set_info(content=comment, title=author)
        annot.update()

    elif tool == "sticky-note":
        point = pymupdf.Point(rect.x1 + 6, rect.y0)
        annot = page.add_text_annot(point, comment, icon="Comment")
        annot.set_colors(stroke=rgb)
        annot.set_info(title=author)
        annot.update()

    elif tool in ("text-box", "callout"):
        # Place the note box in the right margin beside the quote.
        pw = page.rect.width
        box = pymupdf.Rect(pw - 200, rect.y0, pw - 24, rect.y0 + 90)
        callout = None
        if tool == "callout":
            callout = [pymupdf.Point(rect.x1, (rect.y0 + rect.y1) / 2),
                       pymupdf.Point(box.x0, box.y0 + 12)]
        annot = page.add_freetext_annot(
            box, comment, fontsize=9, text_color=0,
            fill_color=rgb, border_width=1, callout=callout)
        annot.set_info(title=author)
        annot.update()

    elif tool in ("rectangle", "oval"):
        pad = pymupdf.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2)
        annot = (page.add_rect_annot(pad) if tool == "rectangle"
                 else page.add_circle_annot(pad))
        annot.set_colors(stroke=rgb)
        annot.set_border(width=1.5)
        annot.set_info(content=comment, title=author)
        annot.update()

    elif tool == "arrow":
        p1 = pymupdf.Point(rect.x0 - 60, rect.y0 - 24)
        p2 = pymupdf.Point(rect.x0, rect.y0)
        annot = page.add_line_annot(p1, p2)
        annot.set_colors(stroke=rgb)
        annot.set_border(width=1.5)
        annot.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE,
                            pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        annot.set_info(content=comment, title=author)
        annot.update()

    else:
        raise ValueError(f"unknown annotation tool: {tool}")

    return annot
