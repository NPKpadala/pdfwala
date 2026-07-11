#!/usr/bin/env python3
"""
PDFWala PDF→Word benchmark runner.

    PDF  →  PDFWala engine (pdf_to_word)  →  DOCX
                                              │
                        LibreOffice render ───┘→  rendered PDF
                                              │
                                     metrics.py →  per-doc JSON

Discovers every PDF under datasets/<class>/, converts each with the CURRENT
engine, renders the DOCX back to PDF, scores it reference-free, and writes
outputs/<class>/<name>.json plus a run-level summary. Designed to run INSIDE
the pdfwala container with the repo mounted at /src:

  docker run --rm -v /home/opc/pdfwala:/src -e PYTHONPATH=/src \
     -e OUTPUT_FOLDER=/tmp -e TEMP_FOLDER=/tmp pdfwala-app:latest \
     python /src/benchmark/scripts/run_benchmark.py

Never deploys, never commits — measurement only.
"""
import json, os, sys, time, subprocess, tempfile, resource, glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, "/src")
import metrics as M


def render_docx(docx_path, out_dir):
    """DOCX → PDF via headless LibreOffice (the comparison target)."""
    env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="lo_"))
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf",
                        "--outdir", out_dir, docx_path],
                       capture_output=True, timeout=180, env=env)
    except Exception:
        pass
    cand = os.path.join(out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    return cand if os.path.exists(cand) else None


def convert(pdf_path, docx_path):
    """Run the production engine; return (ok, seconds, peak_mb, result)."""
    from core.context import JobContext
    import engines.pdf_engine as PE
    r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    try:
        res = PE.pdf_to_word(JobContext(operation="pdf_to_word",
                                        input_path=pdf_path, output_path=docx_path,
                                        params={}))
        ok = os.path.exists(docx_path) and os.path.getsize(docx_path) > 0
    except Exception as ex:
        return False, time.time() - t0, 0, {"error": str(ex)}
    r1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ok, time.time() - t0, max(0, (r1 - r0)) / 1024.0, res


def main():
    datasets = os.path.join(BASE, "datasets")
    out_root = os.path.join(BASE, "outputs")
    pdfs = sorted(glob.glob(os.path.join(datasets, "*", "*.pdf")))
    if not pdfs:
        print("No PDFs under benchmark/datasets/<class>/. Drop samples and re-run.")
        return
    summary = []
    for pdf in pdfs:
        cls = os.path.basename(os.path.dirname(pdf))
        name = os.path.splitext(os.path.basename(pdf))[0]
        odir = os.path.join(out_root, cls); os.makedirs(odir, exist_ok=True)
        docx = os.path.join(odir, name + ".docx")
        ok, secs, mb, res = convert(pdf, docx)
        row = {"file": name, "class": cls, "ok": ok,
               "seconds": round(secs, 2), "peak_mb": round(mb, 1),
               "engine_result": res}
        if ok:
            rendered = render_docx(docx, odir)
            if rendered:
                row.update(M.score_document(pdf, docx, rendered))
        with open(os.path.join(odir, name + ".json"), "w") as f:
            json.dump(row, f, indent=2, default=str)
        summary.append(row)
        c = row.get("composite_0_10")
        print(f"  [{cls:16}] {name[:28]:28} ok={ok} "
              f"score={c} ssim={row.get('visual_ssim')} "
              f"char={row.get('char_accuracy')} {secs:.1f}s")
    # run-level summary + macro averages (avoid single-doc overfitting)
    scored = [r for r in summary if r.get("composite_0_10") is not None]
    def avg(k):
        vals = [r[k] for r in scored if isinstance(r.get(k), (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None
    agg = {"n": len(summary), "n_ok": sum(1 for r in summary if r["ok"]),
           "failure_rate": round(1 - sum(1 for r in summary if r["ok"]) / len(summary), 3),
           "macro_composite": avg("composite_0_10"),
           "macro_char_accuracy": avg("char_accuracy"),
           "macro_word_f1": avg("word_f1"),
           "macro_visual_ssim": avg("visual_ssim"),
           "macro_word_native_ratio": avg("word_native_ratio"),
           "macro_seconds": avg("seconds"), "docs": summary}
    with open(os.path.join(out_root, "run_summary.json"), "w") as f:
        json.dump(agg, f, indent=2, default=str)
    print(f"\nMACRO composite={agg['macro_composite']} "
          f"char={agg['macro_char_accuracy']} ssim={agg['macro_visual_ssim']} "
          f"failure_rate={agg['failure_rate']}  ({agg['n_ok']}/{agg['n']} ok)")


if __name__ == "__main__":
    main()
