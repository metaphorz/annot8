"use strict";
/*
 * matcher.js — fuzzy quote location, ported from the Python annotator.
 *
 * A "word" is { text, x0, y0, x1, y1 } in PDF points (origin bottom-left).
 * Anchors on content tokens (>= 3 chars) so common short words and OCR
 * noise don't distort the match; the highlight is trimmed to the densest
 * cluster of matched words.
 *
 * By Paul Fishwick and Claude Code.
 */
const Matcher = (() => {

  const norm = (t) => t.toLowerCase().replace(/[^a-z0-9]/g, "");

  // One rect per text line spanned by words[first..last]; + bounding rect.
  function quadsForRange(words, first, last) {
    const slice = words.slice(first, last + 1);
    const lines = [];
    slice.forEach((w) => {
      const cy = (w.y0 + w.y1) / 2;
      const h = Math.max(w.y1 - w.y0, 4);
      let ln = lines.find((l) => Math.abs(l.cy - cy) < h * 0.6);
      if (!ln) { ln = { cy: cy, ws: [] }; lines.push(ln); }
      ln.ws.push(w);
    });
    const quads = [];
    let rect = null;
    lines.forEach((l) => {
      const r = {
        x0: Math.min(...l.ws.map((w) => w.x0)),
        y0: Math.min(...l.ws.map((w) => w.y0)),
        x1: Math.max(...l.ws.map((w) => w.x1)),
        y1: Math.max(...l.ws.map((w) => w.y1)),
      };
      quads.push(r);
      rect = rect ? {
        x0: Math.min(rect.x0, r.x0), y0: Math.min(rect.y0, r.y0),
        x1: Math.max(rect.x1, r.x1), y1: Math.max(rect.y1, r.y1),
      } : Object.assign({}, r);
    });
    return { quads, rect };
  }

  // Best fuzzy match of `quote` within one page's words.
  function matchQuote(words, quote) {
    const page = [];
    words.forEach((w, i) => {
      const n = norm(w.text);
      if (n.length >= 3) page.push({ n: n, i: i });
    });
    const q = quote.split(/\s+/).map(norm).filter((t) => t.length >= 3);
    if (!page.length || !q.length) return { score: 0, quads: [], rect: null };

    const ptok = page.map((p) => p.n);
    const n = ptok.length;
    const win = Math.min(q.length, n);
    const qcount = new Map();
    q.forEach((t) => qcount.set(t, (qcount.get(t) || 0) + 1));

    const wc = new Map();
    const add = (t, d) => wc.set(t, (wc.get(t) || 0) + d);
    for (let k = 0; k < win; k++) add(ptok[k], 1);
    const overlap = () => {
      let s = 0;
      qcount.forEach((qn, t) => { s += Math.min(wc.get(t) || 0, qn); });
      return s;
    };
    let bestScore = overlap(), bestStart = 0;
    for (let s = 1; s <= n - win; s++) {
      add(ptok[s - 1], -1);
      add(ptok[s + win - 1], 1);
      const o = overlap();
      if (o > bestScore) { bestScore = o; bestStart = s; }
    }

    const qset = new Set(q);
    const hits = [];
    for (let k = bestStart; k < bestStart + win; k++)
      if (qset.has(ptok[k])) hits.push(k);
    if (!hits.length) return { score: 0, quads: [], rect: null };

    // Keep the densest cluster of matched tokens (gaps <= 3).
    const clusters = [[hits[0]]];
    for (let j = 1; j < hits.length; j++) {
      if (hits[j] - hits[j - 1] <= 3) clusters[clusters.length - 1].push(hits[j]);
      else clusters.push([hits[j]]);
    }
    let cluster = clusters[0];
    clusters.forEach((c) => { if (c.length > cluster.length) cluster = c; });

    let firstWi = page[cluster[0]].i;
    let lastWi = page[cluster[cluster.length - 1]].i;
    if (lastWi < firstWi) { const t = firstWi; firstWi = lastWi; lastWi = t; }
    const qr = quadsForRange(words, firstWi, lastWi);
    return { score: bestScore / q.length, quads: qr.quads, rect: qr.rect };
  }

  function findQuoteQuads(words, quote, minScore) {
    const m = matchQuote(words, quote);
    return m.score >= (minScore === undefined ? 0.6 : minScore)
      ? m : { score: 0, quads: [], rect: null };
  }

  // Search the whole document; `pageHint` (1-based) only breaks ties.
  function locate(pagesWords, pageHint, quote, minScore) {
    const thr = minScore === undefined ? 0.55 : minScore;
    let best = null;
    for (let p = 1; p <= pagesWords.length; p++) {
      const words = pagesWords[p - 1];
      if (!words || !words.length) continue;
      const m = matchQuote(words, quote);
      if (!m.rect) continue;
      const dist = -Math.abs(p - pageHint);
      if (!best || m.score > best.score + 1e-9 ||
          (Math.abs(m.score - best.score) < 1e-9 && dist > best.dist)) {
        best = { score: m.score, dist: dist, page: p,
                 quads: m.quads, rect: m.rect };
      }
    }
    return (best && best.score >= thr) ? best : null;
  }

  return { matchQuote: matchQuote, findQuoteQuads: findQuoteQuads,
           locate: locate };
})();
