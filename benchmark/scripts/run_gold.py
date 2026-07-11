#!/usr/bin/env python3
"""Gold-set runner: PDF -> current engine -> DOCX -> render -> score with
GROUND-TRUTH content recall + reference-free metrics. No engine modification."""
import json, os, sys, time, subprocess, tempfile, resource, glob, re
from difflib import SequenceMatcher
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts")); sys.path.insert(0, "/src")
import metrics as M
GOLD = os.path.join(BASE, "gold_set")
OUT = os.path.join(BASE, "outputs", "gold")


# Generator-bookkeeping GT keys that are NEVER rendered as document content.
# Excluded from scoring entirely (neither numerator nor denominator), key+subtree.
# Universal across all 8 categories: docType/layoutVariant/sourceFile/seed.
# Per-schema non-content: currency (ISO code, PDF prints the symbol), scanParams
# (ocr_scan degradation params), layout (brochure layout directives), chartType
# (image_heavy descriptor of which chart to draw — "pie"/"bar"/"line" is never
# rendered as text OR pixels; confirmed absent from PDF text and OCR of the
# render). NB documentKind (ocr_scan) is deliberately NOT here — it IS rendered
# (the memo banner) and stays scored. This is a schema-level filter, NOT an
# invoice special-case. It does NOT exclude GT-vs-PDF "layout-omitted" content
# (e.g. _B customer block / unit-price column): those stay scored as failures —
# a documented GT/template authoring gap.
_META_KEYS = {"docType", "layoutVariant", "sourceFile", "seed",
              "currency", "scanParams", "layout", "chartType"}

# GT keys holding long free-text paragraph content, where exact-string match
# over-penalises single-character OCR noise ("maintain a" -> "maintain @"). ONLY
# leaves reached under such a key are eligible for fuzzy/edit-distance matching;
# every other field (names, dates, numbers, headers, short strings) stays exact
# (post-normalisation). This is scoped by field NAME, not by a blanket similarity
# pass. Confirmed: "paragraphs" occurs only in the ocr_scan schema, so no other
# category is affected. Other categories' long-text fields (contract "text",
# brochure "body", invoice "terms", resume "bullets") are deliberately NOT here —
# they were not analysed for near-miss behaviour and stay exact.
_PARA_KEYS = {"paragraphs"}
_PARA_SIM_THRESHOLD = 0.90

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_FULL = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
               "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
               "november": 11, "december": 12}


def gt_fields(obj, acc, para=False):
    """Collect (value, is_paragraph) leaves (len>=3), SKIPPING metadata keys.
    is_paragraph flags leaves reached under a _PARA_KEYS key (fuzzy-eligible)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _META_KEYS:
                continue
            gt_fields(v, acc, para or (k in _PARA_KEYS))
    elif isinstance(obj, list):
        for v in obj:
            gt_fields(v, acc, para)
    elif isinstance(obj, bool):
        pass  # booleans are state (e.g. checkbox `checked`), never rendered as
              # document text; exclude BEFORE the int check since bool subclasses int
    elif isinstance(obj, (str, int, float)):
        s = str(obj).strip()
        if len(s) >= 3:
            acc.append((s, para))
    return acc


def gt_strings(obj, acc):
    """Collect leaf string/number values (len>=3) from a ground-truth JSON,
    SKIPPING generator-bookkeeping keys and their subtrees (see _META_KEYS)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _META_KEYS:
                continue
            gt_strings(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            gt_strings(v, acc)
    elif isinstance(obj, (str, int, float)):
        s = str(obj).strip()
        if len(s) >= 3:
            acc.append(s)
    return acc


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _canon_num(tok):
    """'$3,432.94' -> '3432.94'; '490.00' -> '490'; '6.5%' -> '6.5'; None if
    not a pure numeric/currency/percent token."""
    m = re.fullmatch(r"[\s$₹€£¥]*(-?[\d,]+(?:\.\d+)?)\s*%?", str(tok).strip())
    if not m:
        return None
    try:
        f = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return str(int(f)) if f == int(f) else ("%f" % f).rstrip("0").rstrip(".")


def _nums_in_text(text):
    out = set()
    for tok in re.findall(r"[$₹€£¥]?\s?-?[\d,]+(?:\.\d+)?%?", text):
        c = _canon_num(tok)
        if c is not None:
            out.add(c)
    return out


def _canon_date(s):
    """Return (year, month, day|None) or None. Handles ISO, 'Nov 6, 2023',
    'November 6 2023', 'Nov 2023'."""
    s = norm(s)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        mo = _MONTH_FULL.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        if mo:
            return (int(m.group(3)), mo, int(m.group(2)))
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{4})", s)
    if m:
        mo = _MONTH_FULL.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        if mo:
            return (int(m.group(2)), mo, None)
    return None


def _dates_in_text(text):
    out = set()
    for m in re.finditer(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        out.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text.lower()):
        mo = _MONTH_FULL.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        if mo:
            out.add((int(m.group(3)), mo, int(m.group(2))))
    for m in re.finditer(r"([a-z]{3,9})\.?\s+(\d{4})", text.lower()):
        mo = _MONTH_FULL.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        if mo:
            out.add((int(m.group(2)), mo, None))
    return out


def _present(value, text_norm, nums, dates):
    """Is the GT value recoverable from the DOCX under text/number/date
    normalisation? (currency symbols, thousands separators, trailing zeros,
    percent signs, and ISO<->month-name date formats all compare equal)."""
    v = norm(value)
    if len(v) >= 3 and v in text_norm:
        return True
    cn = _canon_num(value)
    if cn is not None and cn in nums:
        return True
    dt = _canon_date(value)
    if dt:
        if dt in dates:
            return True
        if dt[2] is None and any(d[0] == dt[0] and d[1] == dt[1] for d in dates):
            return True
        if dt[2] is not None and (dt[0], dt[1], None) in dates:
            return True
    return False


def _para_similarity(g, t):
    """Best LOCAL char-similarity (0..1) of paragraph text `g` within docx text
    `t` (both normalised). Anchored on the longest common block and scored inside
    a single len(g)-sized window, so repeated boilerplate cannot stitch a false
    match across the document. Used ONLY for _PARA_KEYS fields."""
    if not g or not t:
        return 0.0
    m = SequenceMatcher(None, g, t, autojunk=False).find_longest_match(0, len(g), 0, len(t))
    if m.size == 0:
        return 0.0
    start = max(0, m.b - m.a - 5)              # align window to g's start
    window = t[start:start + len(g) + 10]
    return SequenceMatcher(None, g, window, autojunk=False).ratio()


def content_recall(gt_json, docx_text):
    fields = gt_fields(gt_json, [])            # [(value, is_paragraph), ...]
    if not fields:
        return None, 0, 0
    dt = norm(docx_text)
    nums = _nums_in_text(docx_text)
    dates = _dates_in_text(docx_text)
    found = 0
    for val, is_para in fields:
        if _present(val, dt, nums, dates):
            found += 1
        elif is_para and _para_similarity(norm(val), dt) >= _PARA_SIM_THRESHOLD:
            found += 1                          # long free-text: tolerate OCR noise
    return round(found / len(fields), 4), found, len(fields)


def render(docx, out_dir):
    env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="lo_"))
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf",
                        "--outdir", out_dir, docx], capture_output=True, timeout=180, env=env)
    except Exception:
        pass
    c = os.path.join(out_dir, os.path.splitext(os.path.basename(docx))[0] + ".pdf")
    return c if os.path.exists(c) else None


def convert(pdf, docx):
    from core.context import JobContext
    import engines.pdf_engine as PE
    r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    try:
        PE.pdf_to_word(JobContext(operation="pdf_to_word", input_path=pdf,
                                  output_path=docx, params={}))
        ok = os.path.exists(docx) and os.path.getsize(docx) > 0
    except Exception as ex:
        return False, time.time() - t0, 0, str(ex)
    r1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ok, time.time() - t0, max(0, r1 - r0) / 1024.0, None


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for pdf in sorted(glob.glob(os.path.join(GOLD, "pdfs", "*.pdf"))):
        name = os.path.splitext(os.path.basename(pdf))[0]
        cat = re.sub(r"_\d+_[A-Z]$", "", name)
        gtp = os.path.join(GOLD, "ground_truth", name + ".json")
        docx = os.path.join(OUT, name + ".docx")
        ok, secs, mb, err = convert(pdf, docx)
        row = {"file": name, "class": cat, "ok": ok, "seconds": round(secs, 2),
               "peak_mb": round(mb, 1), "error": err}
        if ok:
            try:
                gt = json.load(open(gtp)) if os.path.exists(gtp) else {}
                dt = M._docx_text(docx)
                cr, f, t = content_recall(gt, dt)
                row.update(gt_recall=cr, gt_found=f, gt_total=t)
            except Exception as ex:
                row["gt_error"] = str(ex)
            rend = render(docx, OUT)
            if rend:
                try:
                    row.update(M.structure_metrics(pdf, docx))
                    row.update(M.font_metrics(docx))
                    row.update(M.visual_metrics(pdf, rend))
                except Exception as ex:
                    row["metric_error"] = str(ex)
        rows.append(row)
        print(f"  [{cat:12}] {name:20} ok={ok} gt_recall={row.get('gt_recall')} "
              f"ssim={row.get('visual_ssim')} pages={row.get('page_ratio')} {secs:.1f}s")
    # aggregate
    import collections
    bycat = collections.defaultdict(list)
    for r in rows:
        bycat[r["class"]].append(r)
    def avg(rs, k):
        v = [x[k] for x in rs if isinstance(x.get(k), (int, float))]
        return round(sum(v) / len(v), 4) if v else None
    cats = {c: {"n": len(rs), "ok": sum(1 for x in rs if x["ok"]),
                "gt_recall": avg(rs, "gt_recall"), "visual_ssim": avg(rs, "visual_ssim"),
                "page_ratio": avg(rs, "page_ratio"), "word_native": avg(rs, "word_native_ratio"),
                "link_recall": avg(rs, "link_recall"), "seconds": avg(rs, "seconds")}
            for c, rs in sorted(bycat.items())}
    agg = {"n": len(rows), "ok": sum(1 for r in rows if r["ok"]),
           "macro_gt_recall": avg(rows, "gt_recall"),
           "macro_visual_ssim": avg(rows, "visual_ssim"),
           "macro_page_ratio": avg(rows, "page_ratio"),
           "failure_rate": round(1 - sum(1 for r in rows if r["ok"]) / len(rows), 3),
           "by_category": cats, "docs": rows}
    json.dump(agg, open(os.path.join(BASE, "outputs", "gold_summary.json"), "w"),
              indent=2, default=str)
    print("\n=== BY CATEGORY ===")
    for c, s in cats.items():
        print(f"  {c:12} n={s['n']:2} ok={s['ok']:2} gt_recall={s['gt_recall']} "
              f"ssim={s['visual_ssim']} pages={s['page_ratio']} native={s['word_native']}")
    print(f"\nMACRO gt_recall={agg['macro_gt_recall']} ssim={agg['macro_visual_ssim']} "
          f"page_ratio={agg['macro_page_ratio']} failure_rate={agg['failure_rate']} "
          f"({agg['ok']}/{agg['n']} ok)")


if __name__ == "__main__":
    main()
