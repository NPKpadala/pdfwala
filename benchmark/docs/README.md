# PDFWala PDF→Word Benchmark Framework

A permanent, **reference-free**, regression-gated benchmark that makes every
future PDF→Word engine change *benchmark-driven* instead of anecdotal.

> No ground-truth DOCX is required. Every metric compares the produced DOCX
> against the **source PDF** (or renders the DOCX back to PDF and compares
> visually). This is the only workable approach for arbitrary real-world PDFs,
> and it is exactly what catches regressions between engine versions.

## Folder layout
```
benchmark/
  datasets/        source PDFs, one folder per document class (22 classes)
    resumes/ invoices/ contracts/ research-papers/ manuals/ books/
    magazines/ newspapers/ government-forms/ medical-reports/ bank-statements/
    purchase-orders/ engineering-drawings/ financial-reports/ brochures/
    image-heavy/ table-heavy/ ocr-scans/ handwritten/ encrypted/
    multilingual/ mixed-layout/
  ground_truth/    (optional) hand-made reference DOCX for a small gold subset
  outputs/         generated DOCX + rendered PDF + per-doc JSON + run_summary.json
  reports/         report.html / report.md / report.csv + baseline.json
  scripts/         the framework (below)
  docs/            this README
```

## Scripts
| script | purpose |
|---|---|
| `classify.py` | writes `<pdf>.metadata.json` per file + auto difficulty (Easy/Medium/Hard/Extreme) via PyMuPDF |
| `run_benchmark.py` | PDF → engine → DOCX → render-back-to-PDF → `metrics.py` → per-doc JSON + macro summary |
| `metrics.py` | all reference-free metrics (text/char/word, visual SSIM+pHash, structure, geometry, fonts) + a 0–10 composite |
| `make_report.py` | aggregates `run_summary.json` → HTML + Markdown + CSV (macro-averaged by class) |
| `regression_check.py` | `--freeze` stores a baseline; default run **auto-fails (exit 1)** if any metric regresses beyond threshold |

## What is measured (per document, macro-averaged across the corpus)
- **Text**: char accuracy (Levenshtein ratio vs PDF text), word recall/precision/F1
- **Visual**: SSIM + perceptual-hash distance (rendered DOCX vs source PDF), page-count ratio
- **Structure**: headings, real list items (`numPr`), tables, hyperlinks + link-recall vs PDF
- **Geometry**: left/right/top margin drift (pt)
- **Fonts**: share of runs using a Word-native family (substitution risk)
- **Perf**: wall time, peak RSS delta, failure rate

## How to run (inside the pdfwala container)
```bash
D=/home/opc/pdfwala
run() { sudo docker run --rm -v $D:/src -e PYTHONPATH=/src \
        -e OUTPUT_FOLDER=/tmp -e TEMP_FOLDER=/tmp pdfwala-app:latest \
        sh -c "cd /src && python benchmark/scripts/$1"; }

run classify.py            # (re)generate metadata + difficulty
run run_benchmark.py       # convert + score every dataset PDF
run make_report.py         # HTML / MD / CSV
run "regression_check.py"  # gate vs frozen baseline (exit 1 = regression)
```

## Regression workflow (the quality gate)
1. On a **known-good** engine: `run_benchmark.py` then
   `regression_check.py --freeze` → stores `reports/baseline.json`.
2. Make an engine change (on a branch).
3. `run_benchmark.py` → `regression_check.py`. Non-zero exit = a metric dropped
   below threshold on **any** document or on the macro average → the change is
   rejected until explained/fixed.

Thresholds (absolute deltas, tune in `regression_check.py`): char −0.02,
word_f1 −0.03, visual_ssim −0.03, word_native −0.05, link_recall −0.10,
composite −0.15, page_ratio worsening > 0.15.

## Adding PDFs (legal sources only)
Drop a PDF into the matching `datasets/<class>/` folder, then re-run
`classify.py`. Use only **public-domain / Creative-Commons / government /
university / vendor-demo / officially-licensed** samples; record `source` and
`license` in the generated `metadata.json`. Never add copyrighted books or
scraped documents.

## Avoiding single-document overfitting
- Verdicts use **macro averages by class**, never one file.
- Keep a **holdout** subset (e.g. 20%) used only for the final regression gate,
  not during development.
- A change must raise the **macro** score without regressing **any** class.

## Known metric caveats (honest)
- `char_accuracy` compares DOCX text to the PDF's *raw* text, so our beneficial
  de-hyphenation/ligature repair slightly *lowers* it (the DOCX is intentionally
  cleaner than the source). It is still a valid **regression** signal.
- Full-page `SSIM` is low in absolute terms (whitespace-dominated); treat it as a
  **relative** signal between versions, not an absolute fidelity percentage.
- Tables detection in `classify.py` is a cheap default; enable Camelot/Tabula for
  a precise table corpus when needed.
