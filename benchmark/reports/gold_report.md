# PDFWala PDF→Word — Gold-Set Benchmark (114 docs, ground-truth)

- Macro content-recall (GT): **0.959**  visual SSIM: 0.3528  page_ratio: 1.2939  failure_rate: 0.0

## Leaderboard by category (ground-truth content recall)

| rank | category | n | ok | gt_recall | ssim | page_ratio | word_native |
|--|--|--|--|--|--|--|--|
| 1 | resume | 50 | 50 | 1.0 | 0.3111 | 1.26 | 0.0 |
| 2 | contract | 12 | 12 | 0.9874 | 0.6969 | 1.7917 | 0.0 |
| 3 | table_heavy | 8 | 8 | 0.9603 | 0.2003 | 1.0 | 0.0 |
| 4 | ocr_scan | 8 | 8 | 0.952 | 0.0768 | 1.125 | None |
| 5 | form | 8 | 8 | 0.9199 | 0.2836 | 1.0 | 0.0 |
| 6 | brochure | 8 | 8 | 0.8977 | 0.7484 | 1.5 | 0.0 |
| 7 | invoice | 12 | 12 | 0.8961 | 0.2104 | 1.3333 | 0.0 |
| 8 | image_heavy | 8 | 8 | 0.8609 | 0.4128 | 1.25 | 0.0 |

## Worst 15

| category | file | gt_recall | page_ratio | ssim |
|--|--|--|--|--|
| brochure | brochure_008_D | 0.5385 | 1.0 | 0.7036 |
| brochure | brochure_004_D | 0.6429 | 1.0 | 0.664 |
| invoice | invoice_008_B | 0.6667 | 1.0 | 0.2458 |
| form | form_006_C | 0.675 | 1.0 | 0.4583 |
| form | form_003_C | 0.6842 | 1.0 | 0.462 |
| invoice | invoice_002_B | 0.6842 | 1.0 | 0.1755 |
| invoice | invoice_005_B | 0.6923 | 1.0 | 0.221 |
| invoice | invoice_011_B | 0.7097 | 1.0 | 0.2131 |
| ocr_scan | ocr_scan_003_C | 0.7273 | 1.0 | 0.1024 |
| image_heavy | image_heavy_002_B | 0.8 | 1.0 | 0.7601 |
| image_heavy | image_heavy_007_A | 0.8462 | 1.0 | 0.3542 |
| image_heavy | image_heavy_003_C | 0.85 | 1.0 | 0.8764 |
| image_heavy | image_heavy_004_A | 0.8529 | 1.0 | 0.3222 |
| image_heavy | image_heavy_001_A | 0.8667 | 1.0 | 0.2061 |
| image_heavy | image_heavy_006_C | 0.8776 | 1.0 | 0.2863 |

## Best 10

| category | file | gt_recall |
|--|--|--|
| brochure | brochure_001_A | 1.0 |
| brochure | brochure_002_B | 1.0 |
| brochure | brochure_003_C | 1.0 |
| brochure | brochure_005_A | 1.0 |
| brochure | brochure_006_B | 1.0 |
| brochure | brochure_007_C | 1.0 |
| contract | contract_001_A | 1.0 |
| contract | contract_002_B | 1.0 |
| contract | contract_003_C | 1.0 |
| contract | contract_004_A | 1.0 |
