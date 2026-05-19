"use strict";
/*
 * annotator.js — Adobe Acrobat-style annotation engine, written with
 * pdf-lib so it runs entirely in the browser.
 *
 * Creates real PDF annotation objects (so they appear in Acrobat's
 * Comments panel and carry the LLM answer as popup text). Acrobat
 * regenerates the appearance streams when the file is opened.
 *
 * By Paul Fishwick and Claude Code.
 */
const Annotator = (() => {

  const COLORS = {
    yellow: [1.0, 0.92, 0.23], green: [0.55, 0.90, 0.40],
    blue:   [0.45, 0.74, 1.0], pink:  [1.0, 0.55, 0.78],
    orange: [1.0, 0.70, 0.28], red:   [0.95, 0.35, 0.35],
  };
  // CSS colours matching the PDF colours (for the on-screen overlay).
  const CSS = {
    yellow: "#ffeb3b", green: "#8ce566", blue: "#73bdff",
    pink: "#ff8cc7", orange: "#ffb347", red: "#f25959",
  };
  const TOOLS = ["highlight", "underline", "strikeout", "squiggly",
    "sticky-note", "text-box", "callout", "rectangle", "oval", "arrow"];

  // QuadPoints for a markup annotation: per quad TL, TR, BL, BR.
  function quadPoints(quads) {
    const qp = [];
    quads.forEach((q) => {
      qp.push(q.x0, q.y1, q.x1, q.y1, q.x0, q.y0, q.x1, q.y0);
    });
    return qp;
  }

  // Add one annotation to a pdf-lib PDFDocument. `quads`/`rect` are in PDF
  // points (origin bottom-left). pageIndex is 0-based.
  function apply(pdfDoc, pageIndex, quads, rect, tool, color, comment) {
    const L = PDFLib;
    const ctx = pdfDoc.context;
    const page = pdfDoc.getPage(pageIndex);
    const c = COLORS[color] || COLORS.yellow;
    const markup = { highlight: "Highlight", underline: "Underline",
                     strikeout: "StrikeOut", squiggly: "Squiggly" };
    let dict;

    if (markup[tool]) {
      dict = ctx.obj({
        Type: "Annot", Subtype: markup[tool],
        Rect: [rect.x0, rect.y0, rect.x1, rect.y1],
        QuadPoints: quadPoints(quads), C: c, F: 4,
      });
    } else if (tool === "sticky-note") {
      const x = rect.x1 + 4, y = rect.y1 - 18;
      dict = ctx.obj({
        Type: "Annot", Subtype: "Text", Rect: [x, y, x + 18, y + 18],
        Name: "Comment", C: c, F: 4,
      });
    } else if (tool === "text-box" || tool === "callout") {
      const box = [rect.x1 + 14, rect.y0 - 28, rect.x1 + 200, rect.y1 + 28];
      const o = {
        Type: "Annot", Subtype: "FreeText", Rect: box, C: c, F: 4,
        DA: L.PDFString.of("/Helv 9 Tf 0 0 0 rg"),
      };
      if (tool === "callout") {
        o.IT = "FreeTextCallout";
        o.CL = [rect.x1, (rect.y0 + rect.y1) / 2, box[0], box[3] - 12];
        o.LE = "OpenArrow";
      }
      dict = ctx.obj(o);
    } else if (tool === "rectangle" || tool === "oval") {
      dict = ctx.obj({
        Type: "Annot", Subtype: tool === "rectangle" ? "Square" : "Circle",
        Rect: [rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2],
        C: c, BS: { W: 2, S: "S" }, F: 4,
      });
    } else if (tool === "arrow") {
      const x1 = rect.x0 - 55, y1 = rect.y1 + 22, x2 = rect.x0, y2 = rect.y1;
      dict = ctx.obj({
        Type: "Annot", Subtype: "Line",
        Rect: [Math.min(x1, x2) - 8, Math.min(y1, y2) - 8,
               Math.max(x1, x2) + 8, Math.max(y1, y2) + 8],
        L: [x1, y1, x2, y2], C: c, BS: { W: 2, S: "S" }, F: 4,
      });
      dict.set(L.PDFName.of("LE"), ctx.obj(["None", "OpenArrow"]));
    } else {
      throw new Error("unknown annotation tool: " + tool);
    }

    dict.set(L.PDFName.of("Contents"), L.PDFHexString.fromText(comment || ""));
    dict.set(L.PDFName.of("T"), L.PDFHexString.fromText("LLM Annotation"));
    const ref = ctx.register(dict);

    let annots = page.node.lookupMaybe(L.PDFName.of("Annots"), L.PDFArray);
    if (!annots) {
      annots = ctx.obj([]);
      page.node.set(L.PDFName.of("Annots"), annots);
    }
    annots.push(ref);
    return ref;
  }

  return { COLORS: COLORS, CSS: CSS, TOOLS: TOOLS, apply: apply };
})();
