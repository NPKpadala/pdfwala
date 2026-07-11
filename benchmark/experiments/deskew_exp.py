#!/usr/bin/env python3
# TESTED (Phase 6.6, 2026-07-11): deskew-only + PSM 4/6 sweep vs the production
# plain-grayscale/PSM-3 OCR path, on the 8 ocr_scan docs, to recover the 4 genuine _C failures.
# RESULT: NEGATIVE — every variant regresses or merely ties baseline (deskew PSM3 0.938 / PSM4 0.775
# / PSM6 0.927 / deskew PSM6 0.952 vs baseline 0.952); the 2 dropped header banners were recovered by none. Rejected; do not re-attempt without new evidence.
"""EXPERIMENT (no engine change): compare OCR variants on the 8 ocr_scan docs.
Variants: baseline(plain gray, psm3) | deskew-only(psm3) | psm4 | psm6 | deskew+psm6.
Scored with the current Phase-6.5 scorer. Tracks the 4 genuine failures and
checks paragraph fields don't regress."""
import json, os, sys, glob, re, collections
import numpy as np, cv2, fitz, pytesseract
from PIL import Image
sys.path.insert(0, "/src"); sys.path.insert(0, "/src/benchmark/scripts")
import metrics as M, run_gold as RG
from engines.pdf_engine import _estimate_skew_angle
GOLD = "/src/benchmark/gold_set"

GENUINE = {  # the 4 confirmed genuine failures from Phase 6.4
    "ocr_scan_003_C": [("company", "Cascade Systems"), ("documentKind", "interoffice memorandum"),
                       ("date", "2022-10-15")],
    "ocr_scan_006_C": [("company", "Ironwood Solutions")],
}

def render(page):
    pix = page.get_pixmap(matrix=fitz.Matrix(300/72, 300/72), alpha=False, colorspace=fitz.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)

def deskew(pil):
    gray = np.array(pil.convert("L"))
    angle = _estimate_skew_angle(gray)
    if abs(angle) > 0.3:
        h, w = gray.shape
        m = cv2.getRotationMatrix2D((w/2, h/2), angle, 1.0)
        gray = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(gray), angle

def ocr(img, psm):
    return pytesseract.image_to_string(img, lang="eng", config=f"--psm {psm} --oem 3")

VARIANTS = ["baseline_psm3", "deskew_psm3", "plain_psm4", "plain_psm6", "deskew_psm6"]
recalls = collections.defaultdict(list)
para_pass = collections.defaultdict(lambda: [0, 0])   # variant -> [passed, total] paragraph fields
genuine_hits = collections.defaultdict(list)
angles = {}

def para_fields(gt):
    return [v for v, isp in RG.gt_fields(gt, []) if isp]

for pdf in sorted(glob.glob(os.path.join(GOLD, "pdfs", "ocr_scan*.pdf"))):
    name = os.path.splitext(os.path.basename(pdf))[0]
    gt = json.load(open(os.path.join(GOLD, "ground_truth", name + ".json")))
    doc = fitz.open(pdf)
    imgs = [render(p) for p in doc]; doc.close()
    des = [deskew(im) for im in imgs]
    angles[name] = round(max((a for _, a in des), key=abs), 2) if des else 0.0
    texts = {
        "baseline_psm3": "\n".join(ocr(im, 3) for im in imgs),
        "deskew_psm3":   "\n".join(ocr(d, 3) for d, _ in des),
        "plain_psm4":    "\n".join(ocr(im, 4) for im in imgs),
        "plain_psm6":    "\n".join(ocr(im, 6) for im in imgs),
        "deskew_psm6":   "\n".join(ocr(d, 6) for d, _ in des),
    }
    pf = para_fields(gt)
    for v, t in texts.items():
        cr, f, tot = RG.content_recall(gt, t)
        recalls[v].append(cr)
        dn, nums, dates = RG.norm(t), RG._nums_in_text(t), RG._dates_in_text(t)
        # paragraph pass count (exact OR fuzzy>=0.90)
        for p in pf:
            para_pass[v][1] += 1
            if RG._present(p, dn, nums, dates) or RG._para_similarity(RG.norm(p), dn) >= 0.90:
                para_pass[v][0] += 1
        # genuine-failure recovery
        for key, val in GENUINE.get(name, []):
            if RG._present(val, dn, nums, dates):
                genuine_hits[v].append(f"{name}.{key}")

print("=== detected max skew angle per doc (deg) ===")
for n, a in angles.items(): print(f"  {n}: {a}")
print("\n=== ocr_scan mean recall by variant (8 docs, Phase-6.5 scorer) ===")
for v in VARIANTS:
    print(f"  {v:14} {round(sum(recalls[v])/len(recalls[v]),4)}")
print("\n=== paragraph fields passing (regression guard: baseline is the floor) ===")
for v in VARIANTS:
    p, t = para_pass[v]; print(f"  {v:14} {p}/{t}")
print("\n=== genuine-failure recoveries (any variant recovering the 4 headers/date) ===")
any_hit = False
for v in VARIANTS:
    h = genuine_hits[v]
    if h: any_hit = True
    print(f"  {v:14} {h if h else 'none'}")
if not any_hit:
    print("  >>> NONE of the 4 genuine failures recovered under ANY variant.")
print("\n=== per-doc recall: baseline vs deskew_psm3 ===")
for i, pdf in enumerate(sorted(glob.glob(os.path.join(GOLD, 'pdfs', 'ocr_scan*.pdf')))):
    n = os.path.splitext(os.path.basename(pdf))[0]
    print(f"  {n:18} base={recalls['baseline_psm3'][i]}  deskew={recalls['deskew_psm3'][i]}  angle={angles[n]}")
