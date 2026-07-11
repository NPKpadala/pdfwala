#!/usr/bin/env python3
"""Aggregate outputs/run_summary.json into reports/ as HTML + Markdown + CSV
(JSON is already the run_summary). Reference-free scores, macro-averaged by
document class so no single file dominates the verdict."""
import json, os, csv, collections

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUM = os.path.join(BASE, "outputs", "run_summary.json")
REP = os.path.join(BASE, "reports")

COLS = ["class", "file", "ok", "composite_0_10", "char_accuracy", "word_f1",
        "visual_ssim", "phash_dist", "page_ratio", "word_native_ratio",
        "headings", "list_items", "tables", "hyperlinks_docx", "link_recall",
        "seconds", "peak_mb"]


def main():
    if not os.path.exists(SUM):
        print("Run run_benchmark.py first."); return
    with open(SUM) as f:
        agg = json.load(f)
    docs = agg.get("docs", [])
    os.makedirs(REP, exist_ok=True)

    # CSV
    with open(os.path.join(REP, "report.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for d in docs:
            w.writerow(d)

    # per-class macro averages
    byclass = collections.defaultdict(list)
    for d in docs:
        if d.get("composite_0_10") is not None:
            byclass[d["class"]].append(d)
    def cavg(rows, k):
        v = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        return round(sum(v) / len(v), 3) if v else "-"
    class_rows = [(c, len(r), cavg(r, "composite_0_10"), cavg(r, "char_accuracy"),
                   cavg(r, "visual_ssim"), cavg(r, "word_native_ratio"))
                  for c, r in sorted(byclass.items())]

    # Markdown
    md = ["# PDFWala PDF→Word Benchmark Report", "",
          f"- Documents: **{agg.get('n')}**  ok: **{agg.get('n_ok')}**  "
          f"failure_rate: **{agg.get('failure_rate')}**",
          f"- MACRO composite: **{agg.get('macro_composite')}/10**  "
          f"char: {agg.get('macro_char_accuracy')}  ssim: {agg.get('macro_visual_ssim')}  "
          f"word-native: {agg.get('macro_word_native_ratio')}", "",
          "## By document class", "",
          "| class | n | composite | char_acc | ssim | word_native |",
          "|---|---|---|---|---|---|"]
    for c, n, comp, ch, ss, wn in class_rows:
        md.append(f"| {c} | {n} | {comp} | {ch} | {ss} | {wn} |")
    md += ["", "## Per document", "",
           "| class | file | score | char | ssim | pages | fonts_native |",
           "|---|---|---|---|---|---|---|"]
    for d in docs:
        md.append(f"| {d.get('class')} | {d.get('file')} | {d.get('composite_0_10')} | "
                  f"{d.get('char_accuracy')} | {d.get('visual_ssim')} | "
                  f"{d.get('page_ratio')} | {d.get('word_native_ratio')} |")
    with open(os.path.join(REP, "report.md"), "w") as f:
        f.write("\n".join(md))

    # HTML
    def cell(v):
        return "" if v is None else v
    rows_html = "".join(
        f"<tr><td>{d.get('class')}</td><td>{d.get('file')}</td>"
        f"<td>{cell(d.get('composite_0_10'))}</td><td>{cell(d.get('char_accuracy'))}</td>"
        f"<td>{cell(d.get('visual_ssim'))}</td><td>{cell(d.get('page_ratio'))}</td>"
        f"<td>{cell(d.get('word_native_ratio'))}</td></tr>" for d in docs)
    class_html = "".join(
        f"<tr><td>{c}</td><td>{n}</td><td>{comp}</td><td>{ch}</td><td>{ss}</td><td>{wn}</td></tr>"
        for c, n, comp, ch, ss, wn in class_rows)
    html = f"""<!doctype html><meta charset=utf-8>
<title>PDFWala PDF→Word Benchmark</title>
<style>body{{font:14px system-ui;margin:2rem;color:#111}}
table{{border-collapse:collapse;margin:1rem 0}}td,th{{border:1px solid #ccc;padding:4px 8px}}
th{{background:#f3f4f6}}h1{{margin:0}}.k{{color:#555}}</style>
<h1>PDFWala PDF→Word Benchmark</h1>
<p class=k>Documents {agg.get('n')} · ok {agg.get('n_ok')} · failure_rate {agg.get('failure_rate')}</p>
<p><b>MACRO composite {agg.get('macro_composite')}/10</b> · char {agg.get('macro_char_accuracy')}
· ssim {agg.get('macro_visual_ssim')} · word-native {agg.get('macro_word_native_ratio')}</p>
<h2>By class</h2><table><tr><th>class<th>n<th>composite<th>char<th>ssim<th>word_native</tr>{class_html}</table>
<h2>Per document</h2><table><tr><th>class<th>file<th>score<th>char<th>ssim<th>pages<th>fonts_native</tr>{rows_html}</table>
"""
    with open(os.path.join(REP, "report.html"), "w") as f:
        f.write(html)
    print(f"Wrote report.md / report.html / report.csv to {REP}")


if __name__ == "__main__":
    main()
