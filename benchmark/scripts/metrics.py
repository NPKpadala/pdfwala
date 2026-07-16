"""
PDFWala PDF→Word benchmark metrics — REFERENCE-FREE.

No ground-truth DOCX is required. Every metric compares the produced DOCX
against the SOURCE PDF, or renders the DOCX back to PDF and compares visually.
This is the same methodology Adobe/ABBYY-style teams use when a gold DOCX is
unavailable, and it is what catches real regressions between engine versions.

Metric groups:
  text     — character/word accuracy of extracted text vs the PDF's own text
  visual   — SSIM + perceptual-hash distance (render DOCX→PDF, compare to source)
  structure— headings / lists / tables / hyperlinks (counts; F1 where inferable)
  geometry — page count, margins (pt), alignment mix
  fonts    — share of runs using a Word-native family (substitution risk)
  perf     — wall time, peak RSS delta (filled by the runner)

All functions are defensive: a failure returns a sentinel, never raises, so one
bad document can't abort a corpus run.
"""
import re
import statistics as _stat

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None
try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None
try:
    from docx import Document
    from docx.oxml.ns import qn
except Exception:  # pragma: no cover
    Document = None

_WORD_NATIVE = {
    "arial", "calibri", "cambria", "times new roman", "georgia", "verdana",
    "tahoma", "segoe ui", "consolas", "courier new", "aptos", "trebuchet ms",
    "garamond", "candara", "symbol", "wingdings", "wingdings 2",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _norm_text(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _pdf_text(pdf_path):
    if fitz is None:
        return ""
    d = fitz.open(pdf_path)
    t = "".join(p.get_text("text") for p in d)
    d.close()
    return t


def _docx_text(docx_path):
    if Document is None:
        return ""
    d = Document(docx_path)
    parts = [p.text for p in d.paragraphs]
    for tbl in d.tables:
        for row in tbl.rows:
            for cell in row.cells:
                parts.append(cell.text)
    # Running headers/footers are real document text (the G6 pass moves
    # repeating body lines into them) — score them too.
    try:
        for sec in d.sections:
            for zone in (sec.header, sec.footer):
                for p in zone.paragraphs:
                    parts.append(p.text)
    except Exception:
        pass
    return "\n".join(parts)


def _levenshtein_ratio(a, b):
    """Similarity in [0,1] = 1 - edit_distance/max_len. Space-normalised."""
    a, b = _norm_text(a), _norm_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    # Wagner–Fischer over words for speed on long docs, chars for short ones.
    la, lb = len(a), len(b)
    if max(la, lb) > 4000:                # word-level for large docs
        a, b = a.split(), b.split()
        la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return 1.0 - prev[lb] / max(la, lb)


# ── text accuracy ────────────────────────────────────────────────────────────
def text_metrics(pdf_path, docx_path):
    pt = _pdf_text(pdf_path)
    dt = _docx_text(docx_path)
    pset = set(_norm_text(pt).lower().split())
    dset = set(_norm_text(dt).lower().split())
    inter = len(pset & dset)
    recall = inter / len(pset) if pset else 0.0      # PDF words present in DOCX
    precision = inter / len(dset) if dset else 0.0   # DOCX words that are real
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "char_accuracy": round(_levenshtein_ratio(pt, dt), 4),
        "word_recall": round(recall, 4),      # coverage of source content
        "word_precision": round(precision, 4),  # no spurious/garbage words
        "word_f1": round(f1, 4),
        "pdf_chars": len(_norm_text(pt)),
        "docx_chars": len(_norm_text(dt)),
    }


# ── visual similarity ────────────────────────────────────────────────────────
def _render_page_gray(pdf_path, page, dpi=100):
    d = fitz.open(pdf_path)
    if page >= len(d):
        d.close(); return None
    pix = d[page].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    d.close()
    return a


def _ssim(a, b):
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    a = a.astype(np.float64); b = b.astype(np.float64)
    mu_a, mu_b = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = ((a - mu_a) * (b - mu_b)).mean()
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / \
           ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))


def _phash(gray):
    img = cv2.resize(gray, (32, 32)).astype(np.float32)
    dct = cv2.dct(img)[:8, :8]
    med = np.median(dct[1:])
    return (dct > med).flatten()


def visual_metrics(source_pdf, rendered_pdf):
    """Compare rendered-DOCX PDF to the source PDF page-by-page. SSIM in [0,1]
    (higher better); pHash Hamming distance (lower better). Page-count-aware."""
    if fitz is None or np is None or cv2 is None:
        return {"visual_ssim": None, "phash_dist": None, "page_ratio": None}
    so = fitz.open(source_pdf); ro = fitz.open(rendered_pdf)
    ns, nr = len(so), len(ro); so.close(); ro.close()
    n = min(ns, nr)
    ssims, hashes = [], []
    for i in range(n):
        a = _render_page_gray(source_pdf, i); b = _render_page_gray(rendered_pdf, i)
        if a is None or b is None:
            continue
        ssims.append(_ssim(a, b))
        ha, hb = _phash(a), _phash(cv2.resize(b, (a.shape[1], a.shape[0])))
        hashes.append(int((ha != hb).sum()))
    return {
        "visual_ssim": round(float(np.mean(ssims)), 4) if ssims else None,
        "phash_dist": round(float(np.mean(hashes)), 2) if hashes else None,
        "page_ratio": round(nr / ns, 3) if ns else None,   # 1.0 = same page count
        "pages_source": ns, "pages_docx": nr,
    }


# ── structure ────────────────────────────────────────────────────────────────
def structure_metrics(pdf_path, docx_path):
    out = {"headings": 0, "list_items": 0, "tables": 0, "hyperlinks_docx": 0,
           "hyperlinks_pdf": 0, "link_recall": None}
    if Document is None:
        return out
    d = Document(docx_path)
    out["headings"] = sum(1 for p in d.paragraphs
                          if p.style.name.lower().startswith("heading"))
    out["list_items"] = sum(1 for p in d.paragraphs
                            if p._p.pPr is not None and p._p.pPr.find(qn("w:numPr")) is not None)
    out["tables"] = len(d.tables)
    out["hyperlinks_docx"] = sum(len(p._p.findall(qn("w:hyperlink"))) for p in d.paragraphs)
    if fitz is not None:
        src = fitz.open(pdf_path)
        pdf_links = set()
        for pg in src:
            for l in pg.get_links():
                if l.get("uri"):
                    pdf_links.add(l["uri"])
        src.close()
        out["hyperlinks_pdf"] = len(pdf_links)
        if pdf_links:
            # count distinct URIs present in the DOCX relationships
            rels = {r.target_ref for r in d.part.rels.values() if "hyperlink" in r.reltype}
            out["link_recall"] = round(len(pdf_links & rels) / len(pdf_links), 3)
    return out


# ── geometry ─────────────────────────────────────────────────────────────────
def geometry_metrics(source_pdf, rendered_pdf):
    if fitz is None:
        return {}

    def margins(pdf):
        d = fitz.open(pdf); pg = d[0]
        xs0, xs1, ys0 = [], [], []
        for blk in pg.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if sp["text"].strip():
                        xs0.append(sp["bbox"][0]); xs1.append(sp["bbox"][2]); ys0.append(sp["bbox"][1])
        w = pg.rect.width; d.close()
        return (min(xs0) if xs0 else 0, w - max(xs1) if xs1 else 0, min(ys0) if ys0 else 0)

    sl, sr, st = margins(source_pdf); rl, rr, rt = margins(rendered_pdf)
    return {
        "margin_left_drift": round(rl - sl, 1),
        "margin_right_drift": round(rr - sr, 1),
        "margin_top_drift": round(rt - st, 1),
    }


# ── fonts ────────────────────────────────────────────────────────────────────
def font_metrics(docx_path):
    if Document is None:
        return {}
    d = Document(docx_path)
    fams = [r.font.name for p in d.paragraphs for r in p.runs if r.font.name]
    if not fams:
        return {"word_native_ratio": None, "font_families": []}
    native = sum(1 for f in fams if f.strip().lower() in _WORD_NATIVE)
    return {
        "word_native_ratio": round(native / len(fams), 3),
        "font_families": sorted(set(fams)),
    }


# ── aggregate one document ───────────────────────────────────────────────────
def score_document(source_pdf, docx_path, rendered_pdf):
    """Combine all metric groups + a single 0-10 composite quality score."""
    m = {}
    for fn in (lambda: text_metrics(source_pdf, docx_path),
               lambda: structure_metrics(source_pdf, docx_path),
               lambda: geometry_metrics(source_pdf, rendered_pdf),
               lambda: font_metrics(docx_path),
               lambda: visual_metrics(source_pdf, rendered_pdf)):
        try:
            m.update(fn())
        except Exception as ex:
            m["_error"] = str(ex)
    m["composite_0_10"] = _composite(m)
    return m


def _composite(m):
    """Weighted 0-10 quality score from the reference-free metrics. Weights are
    documented so they can be tuned deliberately, not silently."""
    parts = []
    def add(v, w, lo=0.0, hi=1.0):
        if v is None:
            return
        parts.append((max(lo, min(hi, v)) - lo) / (hi - lo) * w)
    total_w = 0
    for v, w, lo, hi in [
        (m.get("char_accuracy"), 3.0, 0.0, 1.0),      # text fidelity
        (m.get("word_f1"), 2.0, 0.0, 1.0),
        (m.get("visual_ssim"), 2.0, 0.0, 1.0),        # layout look
        (m.get("word_native_ratio"), 1.0, 0.0, 1.0),  # Word rendering
        (1.0 - min(1.0, abs((m.get("page_ratio") or 1.0) - 1.0)), 1.0, 0.0, 1.0),  # page fidelity
        (m.get("link_recall"), 1.0, 0.0, 1.0),
    ]:
        if v is not None:
            add(v, w, lo, hi); total_w += w
    return round(sum(parts) / total_w * 10, 2) if total_w else None
