#!/usr/bin/env python3
"""Generate reports/gold_report.md + gold_report.csv from outputs/gold_summary.json
(written by run_gold.py). Reporting only — reads no PDFs, runs no engine."""
import json, os, csv
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = json.load(open(os.path.join(BASE, "outputs", "gold_summary.json")))
docs = S["docs"]; cats = S["by_category"]

def g(d, k):
    v = d.get(k)
    return v if isinstance(v, (int, float)) else None

# CSV — per document
csv_path = os.path.join(BASE, "reports", "gold_report.csv")
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["class", "file", "ok", "gt_recall", "visual_ssim", "page_ratio",
                "word_native_ratio", "link_recall", "seconds", "peak_mb"])
    for d in sorted(docs, key=lambda x: (x["class"], x["file"])):
        w.writerow([d["class"], d["file"], d["ok"], d.get("gt_recall"),
                    d.get("visual_ssim"), d.get("page_ratio"),
                    d.get("word_native_ratio"), d.get("link_recall"),
                    d.get("seconds"), d.get("peak_mb")])

# Markdown
lb = sorted(cats.items(), key=lambda kv: (kv[1]["gt_recall"] is not None,
            kv[1]["gt_recall"] or 0), reverse=True)
scored = [d for d in docs if isinstance(d.get("gt_recall"), (int, float))]
worst = sorted(scored, key=lambda d: d["gt_recall"])[:15]
best = sorted(scored, key=lambda d: d["gt_recall"], reverse=True)[:10]

L = []
L.append("# PDFWala PDF→Word — Gold-Set Benchmark (114 docs, ground-truth)\n")
L.append(f"- Macro content-recall (GT): **{S['macro_gt_recall']}**  visual SSIM: "
         f"{S['macro_visual_ssim']}  page_ratio: {S['macro_page_ratio']}  "
         f"failure_rate: {S['failure_rate']}\n")
L.append("## Leaderboard by category (ground-truth content recall)\n")
L.append("| rank | category | n | ok | gt_recall | ssim | page_ratio | word_native |")
L.append("|--|--|--|--|--|--|--|--|")
for i, (c, s) in enumerate(lb, 1):
    L.append(f"| {i} | {c} | {s['n']} | {s['ok']} | {s['gt_recall']} | "
             f"{s['visual_ssim']} | {s['page_ratio']} | {s.get('word_native')} |")
L.append("\n## Worst 15\n")
L.append("| category | file | gt_recall | page_ratio | ssim |")
L.append("|--|--|--|--|--|")
for d in worst:
    L.append(f"| {d['class']} | {d['file']} | {d['gt_recall']} | "
             f"{g(d,'page_ratio')} | {g(d,'visual_ssim')} |")
L.append("\n## Best 10\n")
L.append("| category | file | gt_recall |")
L.append("|--|--|--|")
for d in best:
    L.append(f"| {d['class']} | {d['file']} | {d['gt_recall']} |")
open(os.path.join(BASE, "reports", "gold_report.md"), "w").write("\n".join(L) + "\n")
print("wrote reports/gold_report.md and reports/gold_report.csv")
print(f"macro_gt_recall={S['macro_gt_recall']}")
