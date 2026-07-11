#!/usr/bin/env python3
"""
Regression gate. Compares the latest run_summary.json against a frozen
baseline (reports/baseline.json) and AUTO-FAILS (exit 1) if any document
regresses beyond threshold on any tracked metric.

    python regression_check.py            # check current run vs baseline
    python regression_check.py --freeze   # store the current run AS the baseline

Thresholds (absolute deltas; lower metric value = worse unless noted):
  char_accuracy      : -0.02   (2% text loss fails)
  word_f1            : -0.03
  visual_ssim        : -0.03
  word_native_ratio  : -0.05
  page_ratio         : ±0.0    (any change in |page_ratio-1| worsening > 0.15 fails)
  link_recall        : -0.10
  composite_0_10     : -0.15
Per-document AND macro-average are both checked. New documents (not in baseline)
are reported but never fail the gate.
"""
import json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = os.path.join(BASE, "outputs", "run_summary.json")
BASELINE = os.path.join(BASE, "reports", "baseline.json")

THRESH = {  # metric: (min_allowed_delta, higher_is_better)
    "char_accuracy": (-0.02, True), "word_f1": (-0.03, True),
    "visual_ssim": (-0.03, True), "word_native_ratio": (-0.05, True),
    "link_recall": (-0.10, True), "composite_0_10": (-0.15, True),
}


def load(p):
    with open(p) as f:
        return json.load(f)


def index(summary):
    return {f"{d['class']}/{d['file']}": d for d in summary.get("docs", [])}


def main():
    if "--freeze" in sys.argv:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        cur = load(SUMMARY)
        with open(BASELINE, "w") as f:
            json.dump(cur, f, indent=2, default=str)
        print(f"Baseline frozen: {cur.get('n')} docs, "
              f"macro_composite={cur.get('macro_composite')}")
        return 0
    if not os.path.exists(BASELINE):
        print("No baseline yet. Run with --freeze first."); return 0
    cur, base = load(SUMMARY), load(BASELINE)
    ci, bi = index(cur), index(base)
    failures = []
    for key, b in bi.items():
        c = ci.get(key)
        if not c:
            print(f"  MISSING in current run: {key}"); continue
        for metric, (min_delta, hib) in THRESH.items():
            bv, cv = b.get(metric), c.get(metric)
            if bv is None or cv is None:
                continue
            delta = cv - bv
            if hib and delta < min_delta:
                failures.append((key, metric, bv, cv, round(delta, 4)))
        # page fidelity: worsening away from 1.0 by > 0.15
        bp, cp = b.get("page_ratio"), c.get("page_ratio")
        if bp is not None and cp is not None and abs(cp - 1) - abs(bp - 1) > 0.15:
            failures.append((key, "page_ratio", bp, cp, round(cp - bp, 3)))
    # macro gate
    for metric in ("macro_composite", "macro_char_accuracy", "macro_visual_ssim"):
        bv, cv = base.get(metric), cur.get(metric)
        if bv is not None and cv is not None and cv - bv < -0.05:
            failures.append(("MACRO", metric, bv, cv, round(cv - bv, 4)))
    new = [k for k in ci if k not in bi]
    if new:
        print(f"  {len(new)} new document(s) (not gated): {', '.join(new[:6])}")
    if failures:
        print(f"\n❌ REGRESSION: {len(failures)} metric(s) dropped below threshold")
        for key, metric, bv, cv, delta in failures:
            print(f"    {key:34} {metric:20} {bv} -> {cv}  (Δ {delta})")
        return 1
    print(f"\n✅ NO REGRESSION. macro_composite {base.get('macro_composite')} "
          f"-> {cur.get('macro_composite')} ({cur.get('n_ok')}/{cur.get('n')} ok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
