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

---

## BASELINE (frozen 2026-07-08)
Production baseline = tag `pdf2word-phase1-stable` (engine commit `c8965de`,
deployed). Overall quality **~6.6/10** (was 5.8; iLovePDF ~8.5). All future
PDF→Word work is measured against this baseline. Do not regress it.

## Remaining issues — PRIORITY ORDER
1. **Paragraph reconstruction** (Phase 2, next) — wrapped lines not merged; title
   merged into one paragraph; erratic per-paragraph EMU spacing; headings not
   promoted to styles. Highest perceived-quality gap after text.
2. **Semantic lists** — 23 literal "•" chars instead of real numbering.xml list
   items (not editable/ATS-friendly). Biggest *visual* gap vs iLovePDF.
3. **Font-family mapping** — everything falls back to NotoSans; original family
   lost.
4. **Layout** — absolute EMU left-indents instead of margins/alignment/tab stops;
   no column detection.
5. **Hyperlinks** — GitHub link dropped (email+LinkedIn survive); capture all.
6. **Tables** — no table model for tabular content (two-col rows via tabs).
7. **OCR fallback** — image-only PDFs produce empty DOCX; needs OCR path.
8. **Inherent/low-ROI** — space-split ligatures ("of ce"→office) and
   dictionary-collision words (eld/elds/benet) unfixable by current approach.

## PHASE 2 — Paragraph Reconstruction (exact plan for next session)
Scope ONLY: paragraph grouping, wrapped-line detection, spacing normalization,
heading detection, block ordering. IGNORE tables/fonts/OCR/images/hyperlinks.
Architecture unchanged; operate as a post-process on the pdf2docx DOCX, after the
Phase-1 text-repair pass (add a `_reflow_docx(path)` alongside `_repair_docx`).

Implementation steps (each independently testable, commit per step):
1. **Wrapped-line merge** — merge paragraph N into N-1 when ALL hold: N-1 text
   does not end in `.?!:;`, N starts lowercase (or with a lowercase continuation),
   same `alignment`, |left_indent difference| small, same style, and no large
   vertical gap (space_before below a threshold). Concatenate run lists (preserve
   each run's formatting) with a single space if needed.
2. **Guards (zero-regression)** — NEVER merge: across a heading; a list paragraph
   (starts with •/-/digit+./letter+)); across a blank paragraph; on alignment
   change; into/through the centered header block; when N is short + Title/large
   font. These guards are the crux — false merges are the main risk.
3. **Heading detection** — promote standalone short lines that are all-caps or
   bold-and-larger-than-body to Heading 1/2 styles (keeps section structure and
   improves navigation/reading order).
4. **Spacing normalization** — replace erratic per-paragraph space_before EMU
   values with consistent style-based spacing (e.g. 6pt body, 12pt before
   headings); keep alignment.
5. **Block ordering / reading order** — verify top-to-bottom, left-to-right order
   from pdf2docx; only reorder when clearly wrong (rare; low priority within P2).

### Benchmark harness to BUILD FIRST (before any merge code)
- Labeled corpus: resume, invoice, research paper, manual, legal — each with a
  hand-set ground-truth paragraph count and heading list (store as JSON sidecars).
- Metrics per doc: paragraphs merged, **wrong-merges (false positives)**, heading
  precision/recall, spacing consistency, and a run-level formatting diff (bold/
  italic/font counts must be unchanged — same check as Phase 1.5).
- **Ship gate:** 0 wrong-merges on the corpus AND 0 formatting damage AND no
  regression on the frozen Phase-1 text-repair metrics. Benchmark after each step.

### Where to start next session (no rework needed)
- Files: `engines/pdf_engine.py` (add `_reflow_docx`, call it in `pdf_to_word`
  right after `_repair_docx`). Reuse `_valid_word`/wordlist if helpful.
- Test assets from today: `/tmp/p2w/pdfwala.docx` (real resume), corpus builder
  approach in `/tmp/p2w/`. Build the labeled corpus + harness first.
- Deploy/verify per [[pdfwala-test-deploy]]; async job polling; SNI curl.
