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

---

## Phases 2–5 (post-processing stages, validated; on feat branch)
Isolated, removable stages that run after pdf2docx, each additive/high-confidence.
- **Phase 2 — Document intelligence** (`_reflow_docx`): heading promotion to Heading
  styles (0 FP across 16 cross-genre cases, denylist for stamps), consistent heading
  spacing, conservative wrapped-line merge (11 guards, 0 wrong-merges).
- **Phase 3 — Semantic lists** (`_semantic_docx`/`_reconstruct_lists`): literal
  bullets/numbers → real editable numbering.xml lists (numPr). 23 items/5 groups on
  the resume, 0 false conversions, renders as real bullets.
- **Phase 4A — List geometry**: Word-native Symbol bullets (removed serif
  contamination), indent matched to source (bullet x 78→48pt vs original 49pt),
  tightened list spacing. Zero regressions.
- **Phase 4B — Hyperlink/contact recovery** (`_recover_hyperlinks`): reads dropped
  linked text (email/LinkedIn/GitHub) from the source PDF annotations, re-inserts real
  clickable hyperlinks. No dups, idempotent, formatting preserved.
- **Phase 5 — Word-native font mapping** (`_map_fonts_docx`): remaps Linux/open fonts
  (Noto/Liberation/DejaVu/Carlito/Caladea/Nimbus/Latin Modern) to Word-native families
  so MS Word stops substituting. Compact normaliser handles subset prefixes/CamelCase/
  style suffixes; 24/24 unit cases pass; generalises across 4 doc types; **no reflow
  (page count unchanged)**; Word-native + bullet fonts never touched.

**Composite validation (resume):** bold 24 / italic 5 / runs 120 preserved; body font
Arial; bullet fonts Symbol/Wingdings/Courier intact; 3 hyperlinks, 23 list items, 5
headings. Test suite 41/41.

---

## Column Detection: Over- and Under-Segmentation (unified)  [Known open — DEFER]
*(Consolidated 2026-07-11. Merges the earlier "2→3 page overflow" finding with the
Phase 6.9 brochure reading-order defect — they are ONE issue, two symptoms.)*

**Two failure modes, one root cause.** Both live in the same pdf2docx 0.5.8 heuristic,
`pdf2docx/page/RawPage.py :: parse_section` — specifically the hardcoded
`if current_num_col > 2: current_num_col = 1` cap and the adjacent equal-width guard
(demote a 2-col row to 1 unless the two columns are within a 2:1 width ratio, `f=2.0`):

- **Over-segmentation → page overflow.** Columns *over*-detected: one real column read as
  two. Root-caused (forensic) to spurious multi-column section breaks faking two-column
  "Company … Date" rows (e.g. resumes). Creates a bogus Section split → 2→3 page overflow.
  Naive removal → 2 pages but collapses the date alignment AND would break genuine
  multi-column docs, so it must NOT be a blind heuristic.
- **Under-segmentation → reading-order scramble.** Columns *under*-detected: 3 real regions
  (2 text columns + a side panel) hit the `>2 → 1` cap and collapse to a single full-width
  column. Elements are then ordered by Y-band then X, so the regions interleave line-by-line
  into unreadable output (brochure `_D` layouts; content present, not dropped — a
  reading-order defect). See Phase 6.9 diagnosis + Phase 6.10 mechanism investigation.

**They pull in OPPOSITE directions.** Making detection *more* sensitive to catch the
3-region case worsens the over-segmentation (more spurious 2-col splits → more overflow);
making it *less* sensitive to reduce overflow worsens the interleaving. Same knob, opposite
signs — so any fix MUST address both modes together with a shared two-mode regression
harness. Do NOT attempt as two separate one-sided patches; a one-sided tweak silently
regresses the other mode.

**Current impact:** over-segmentation/page-overflow affects **35/114 docs (31%)**;
reading-order scramble affects **2/114 docs** (both brochure `_D`). Brochure recall 0.898,
would reach ~1.0 if resolved (**macro +0.006**). Low corpus impact for the reading-order
half; the overflow half is systemic but was already deferred as HIGH regression risk.
Confirmed at 8x scale via gold_set_v2 (Phase 7.0c): all 8 new brochure `_D` docs reproduce
the interleaving (0.50–0.69 recall vs 1.0 for A/B/C), 0 new bucket-c defects — still deferred, same reasoning.

**Dependency reality:** pdf2docx **0.5.8 is its final release and is effectively
unmaintained** (Artifex-hosted, MIT-relicensed, no active development). Any fix means
**forking/patching a frozen dependency** — not filing an upstream issue and waiting.

**Revisit conditions (BOTH must hold):**
1. The gold-set corpus grows enough genuine multi-column/sidebar documents that this is a
   material fraction of real usage — not 2 synthetic docs.
2. The fix is scoped as ONE unified column-detection project covering both failure modes,
   with a two-mode regression harness AND direct MS Word rendering verification (not just
   LibreOffice) — not attempted piecemeal.

**Refs:** Phase 6.9 (brochure root-cause diagnosis), Phase 6.10 (parse_section mechanism +
risk assessment), and this entry's predecessor (the original 2→3 page-overflow finding,
now merged here). Related engine subsystem: `_reflow_docx` / two-column-row reconstruction.

## Phase 6 — Vector-graphics recovery  [IMPLEMENTED 2026-07-16, DARK — flag VECTOR_RASTERIZE, default off]

**Problem:** pdf2docx silently drops path-based artwork with no curves (bar charts,
straight-line diagrams): caption survives, drawing vanishes. (Curve-bearing figures —
pies, donuts, line charts with markers — pdf2docx's own figure rasterizer embeds
correctly; measured, do NOT take those over: doing so regressed SSIM up to −0.61.)

**Design (engines/pdf_engine.py, `_vg_*`):**
1. Detect regions via `get_drawings()`: hairlines (table grids/underlines/signature
   rules) never seed; clusters need *chart evidence* (fills / curves / dense polyline /
   thick diagonal stroke ≥2pt) covering ≥5% of area; text-line barriers keep captions
   out of regions; vetoes: <40×40pt, >85% page, ≥90% page-width/height or 2+ edges
   (banners/sidebars), text coverage >4% (charts measured 0.0; furniture ≥0.042),
   raster-image overlap >50%; **curve-skip** (pdf2docx handles those itself).
2. Redact detected regions from a temp copy (LINE_ART_REMOVE_IF_TOUCHED, keep images)
   so pdf2docx cannot mangle the artwork or scatter its label text; convert that copy.
3. Rasterize each region (200 dpi, white bg) and insert inline at its reading-order
   slot: immediately above its caption line (anchor 'below') / after the preceding
   line — caption stays a sibling paragraph. EMU width = source pt size, clamped.

**Gold results (117 docs, flag ON vs OFF):** recall unchanged on ALL docs (macro
0.975); 108/114 baseline docs byte-identical; new chart class recall 1.0 with artwork
present in all 3; image_heavy_001_A SSIM +0.42. BUT 5 chart-dense docs lose SSIM
(−0.09..−0.26) with page overflow (+0.5..+1): pdf2docx's text layout is already ~1.28×
looser than source, so pages with multiple recovered charts cannot absorb the re-added
artwork height — no placement fixes this (floating anchors would overlap the text
pdf2docx pulled up into the collapsed art gaps).

**Status: DARK.** Enable only after the page-density/page_ratio phase tightens
pdf2docx output. Traps verified 0-FP: ruled/merged-cell tables, landscape wide table,
watermarks, brochure banner fills, resume sidebars, invoice logo boxes, form shading.
Test corpus: gold `chart_00{1,2,3}` (bar/pie/line, in manifest), /tmp/bev synthetics.
