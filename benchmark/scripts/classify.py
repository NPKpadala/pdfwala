#!/usr/bin/env python3
"""Emit a metadata.json next to every dataset PDF and auto-classify difficulty.

Difficulty (layout complexity, not content):
  Easy    — single column, no tables, mostly text, digital (not scanned)
  Medium  — some images/hyperlinks OR 2 columns OR a few tables
  Hard    — multi-column + tables, or many images, or forms
  Extreme — scanned/image-only (needs OCR), or dense tables + multi-column

Detection uses PyMuPDF only (no ground truth). 'source'/'license' are left blank
for the curator to fill — we never assume a licence.
"""
import json, os, sys, glob

try:
    import fitz
except Exception:
    print("PyMuPDF required"); sys.exit(1)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def analyze(path):
    d = fitz.open(path)
    n = len(d)
    images = forms = links = 0
    text_chars = 0
    col_votes = []
    scanned_pages = 0
    fonts = set()
    for pg in d:
        t = pg.get_text("text"); text_chars += len(t.strip())
        images += len(pg.get_images())
        links += len(pg.get_links())
        try:
            forms += len(list(pg.widgets() or []))
        except Exception:
            pass
        for f in pg.get_fonts(full=True):
            fonts.add(f[3])
        # scanned heuristic: a full-page image and almost no text
        if len(pg.get_images()) >= 1 and len(t.strip()) < 40:
            scanned_pages += 1
        # column heuristic: cluster span x-centres into left/right halves
        xs = [((s["bbox"][0] + s["bbox"][2]) / 2)
              for b in pg.get_text("dict")["blocks"] for l in b.get("lines", [])
              for s in l.get("spans", []) if s["text"].strip()]
        if xs:
            w = pg.rect.width
            left = sum(1 for x in xs if x < w * 0.45)
            right = sum(1 for x in xs if x > w * 0.55)
            col_votes.append(2 if left > 5 and right > 5 else 1)
    bookmarks = len(d.get_toc())
    d.close()
    columns = 2 if col_votes and (sum(1 for c in col_votes if c == 2) > len(col_votes) / 3) else 1
    ocr = scanned_pages >= max(1, n // 2)
    tables_guess = False  # cheap default; camelot/tabula optional, kept out for speed
    # difficulty
    if ocr:
        diff = "Extreme"
    elif columns == 2 and (images > 3 or forms):
        diff = "Hard"
    elif columns == 2 or images > 5 or forms:
        diff = "Medium" if images <= 8 and not forms else "Hard"
    elif images or links:
        diff = "Medium"
    else:
        diff = "Easy"
    return {
        "filename": os.path.basename(path),
        "source": "", "license": "",
        "category": os.path.basename(os.path.dirname(path)),
        "language": "unknown",
        "pages": n,
        "generator": "",
        "contains_tables": tables_guess,
        "contains_images": images > 0,
        "contains_forms": forms > 0,
        "contains_hyperlinks": links > 0,
        "contains_bookmarks": bookmarks > 0,
        "OCR": ocr,
        "columns": columns,
        "text_chars": text_chars,
        "difficulty": diff,
    }


def main():
    pdfs = sorted(glob.glob(os.path.join(BASE, "datasets", "*", "*.pdf")))
    if not pdfs:
        print("No dataset PDFs found."); return
    for p in pdfs:
        meta = analyze(p)
        with open(p + ".metadata.json", "w") as f:
            json.dump(meta, f, indent=2)
        print(f"  {meta['category']:16} {meta['filename'][:30]:30} "
              f"pages={meta['pages']} cols={meta['columns']} ocr={meta['OCR']} "
              f"-> {meta['difficulty']}")


if __name__ == "__main__":
    main()
