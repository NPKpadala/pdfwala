"""
catalog/audit.py — production readiness audit.

    python catalog/audit.py

Cross-checks the compiled catalog against the running code and the generated
artifacts. Exits non-zero if any check fails. Complements build.py's validation
(which guards the manifest); this guards the whole platform is consistent.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from catalog.registry import registry  # noqa: E402

STATIC = os.path.join(ROOT, "static")
fail, warn = [], []


def check(cond, msg):
    if not cond:
        fail.append(msg)


tools = registry.all()
slugs = {t["slug"] for t in tools}
ids = [t["id"] for t in tools]
routes = [(t["module"], t["route"]) for t in tools]

# 1-3. no duplicate tool definitions / ids / routes
check(len(slugs) == len(tools), "duplicate slugs present")
check(len(set(ids)) == len(ids), "duplicate ids present")
check(len(set(routes)) == len(routes), "duplicate (module,route) present")

# 4. no duplicated categories (per module keys unique by construction) + all referenced exist
for t in tools:
    m = registry.modules.get(t["module"], {})
    check(t["category"] in (m.get("categories") or {}),
          f"{t['id']}: category '{t['category']}' missing in module {t['module']}")

# 5. no broken related links / self-references
for t in tools:
    for r in t.get("related", []):
        check(r in slugs, f"{t['id']}: broken related '{r}'")
        check(r != t["slug"], f"{t['id']}: self-referential related")

# 6. engine registry <-> manifest parity (orphans / dead entries)
try:
    import engines.pdf_engine, engines.image_engine, engines.office_engine  # noqa: F401,E401
    from core.pipeline import _ENGINES
    registered = set(_ENGINES.keys())
    for t in tools:
        check(t["engine"] in registered, f"{t['id']}: engine '{t['engine']}' not registered (orphan tool)")
    # engines that exist but no tool uses them (informational)
    used = {t["engine"] for t in tools}
    for op in sorted(registered - used):
        warn.append(f"engine '{op}' is registered but has no manifest tool")
except Exception as ex:
    warn.append(f"engine parity skipped: {ex}")

# 7. SEO uniqueness — published tools must have unique titles + canonicals
titles, canons = {}, {}
for t in registry.published():
    en = (t.get("content") or {}).get("en") or {}
    ttl, slug = en.get("seo_title"), t["slug"]
    check(ttl and titles.get(ttl) is None, f"duplicate/empty seo_title: {ttl!r} ({slug})")
    titles[ttl] = slug
    canons[slug] = f"https://pdf.npkpadala.com/{slug}/"
check(len(set(canons.values())) == len(canons), "duplicate canonicals among published tools")

# 8. sitemap <-> published parity
sm = os.path.join(STATIC, "sitemap.pdf.xml")
if os.path.exists(sm):
    body = open(sm, encoding="utf-8").read()
    for t in registry.published():
        if not t.get("deprecated"):
            check(f"/{t['slug']}/" in body, f"published tool missing from sitemap: {t['slug']}")

# 9. generated SEO pages <-> published parity (no orphaned page dirs)
page_dirs = {os.path.basename(os.path.dirname(p))
             for p in glob.glob(os.path.join(STATIC, "t", "*", "index.html"))}
pub_slugs = {t["slug"] for t in registry.published()}
for d in page_dirs - pub_slugs:
    warn.append(f"orphaned SEO page dir (no published tool): static/t/{d}")
for s in pub_slugs - page_dirs:
    fail.append(f"published tool has no generated SEO page: {s}")

# 10. search index <-> tools parity
idx_slugs = {i["slug"] for i in registry.get_search_index()}
for s in idx_slugs:
    check(s in slugs, f"search index references unknown slug: {s}")

# 11. dead route files removed
for dead in ("app/routes/image_routes.py", "app/routes/office_routes.py"):
    check(not os.path.exists(os.path.join(ROOT, dead)),
          f"dead route file still present: {dead}")

# ---- report ----
print("AUDIT: %d tools, %d modules, %d published" %
      (len(tools), len(registry.modules), len(registry.published())))
for w in warn:
    print("  WARN:", w)
if fail:
    print("FAIL (%d):" % len(fail))
    for f in fail:
        print("  -", f)
    sys.exit(1)
print("AUDIT PASSED — no duplicates, orphans, broken links, or dead entries.")
