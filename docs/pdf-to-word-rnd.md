# PDF → Word — R&D Log

## Pipeline
`engines/pdf_engine.py::pdf_to_word` — pdf2docx==0.5.8 (extraction+layout+DOCX),
async (queue `office`), chunked+merged for >100 pages. Architecture frozen.

## Phase 1 — Text-repair post-processor  [STABLE, deployed 2026-07-08]
Dictionary-gated repair on the generated DOCX at the run level (formatting
preserved). Bundled 414k-word list `engines/data/en_words.txt.gz`.
- Ligature reinsertion (fi/fl/ff/ffi/ffl) — only when the result is a real word;
  Unicode ligatures U+FB00-06 normalized; interior placeholder dots stripped.
- Smart de-hyphenation incl. cross-run splits ("secur-"|"ing"); rejoins soft
  wraps and `-ment/-tion/-ing` suffixes; keeps compounds (enterprise-scale,
  Hyper-V) and meaning-change hyphens (re-cover, co-op).
- Punctuation spacing cleanup.

### Validation (Phase 1.5)
- Cross-genre corpus (10 types), 103 must-not-change tokens + 9 meaning-change:
  **0 false positives, 0 meaning-changes, 0 formatting damage.**
- Real resume DOCX: 7 ligature corruptions -> 0, 4 soft hyphens rejoined,
  17/118 runs repaired, bold/italic/fonts identical.
- De-hyphenation 7/7; ligature 10/13 (3 = dictionary collisions, inherent).
- Perf: ~80 ms + 2.3 MB per doc; wordlist 215 ms/16 MB one-time (lazy, cold).
- Production: 3 async jobs completed live (nginx→gunicorn→celery→redis→download);
  clean docs pass through unchanged (no regression).

### Benchmark scores (this resume) vs iLovePDF
| Category | Before | After (Phase 1) | iLovePDF |
|---|---|---|---|
| Text accuracy | 6 | 8.5 | 9.5 |
| Paragraphs | 6.5 | 6.5 | 8.5 |
| Fonts | 5 | 5 | 8 |
| Lists | 5 | 5 | 8 |
| Layout | 6 | 6 | 8.5 |
| Hyperlinks | 6 | 6 | 9 |
| Overall | ~5.8 | ~6.6 | ~8.5 |

### Known limitations (open)
- Font family lost (→ NotoSans). Layout via absolute EMU indents; title merged.
- Lists mostly literal "•" (not semantic). GitHub link dropped. No table model.
- Space-split ligatures ("of ce"→office) NOT handled (merge-across-space too risky).
- Dictionary-collision ligature words unfixable (eld/elds/benet).
- Image-only PDFs → empty DOCX (needs OCR fallback).

## Roadmap
- Phase 2 (next): paragraph reconstruction — wrapped-line merge, paragraph
  grouping, spacing normalization, heading detection, reading order.
- Phase 3: semantic lists + font-family mapping.
- Phase 4: layout (columns/margins), table detection, hyperlink recovery.
- Phase 5 (R&D): OCR fallback + ML layout (docTR/LayoutParser); multi-candidate
  selection like Compress.
