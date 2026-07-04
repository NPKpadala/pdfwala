# PDFWala platform architecture

A new engineer should understand the whole platform from this one document in
~30 minutes. **One idea underpins everything:** every tool is defined exactly
once, in a YAML manifest; every other system is *generated* from it.

---

## 1. The big picture

```
                 catalog/manifest/*.yaml          (authored source — one file per MODULE)
                          |
                 python catalog/build.py          (validate -> compile)
                          |
   +----------------------+---------------------------+-------------------------+
   |                      |                           |                         |
catalog/tools.build.json  static/tools.json    static/t/<slug>/index.html   static/sitemap.pdf.xml
 (runtime artifact)       (public: JS/mobile/     (SEO pages, published)
   |                       API/admin)
   v
catalog/registry.py       (typed runtime access — stdlib json only)
   |
   +-- app/routes/tool_factory.py   -> ALL /api/<module>/... tool routes (one factory, one handler)
   +-- app/routes/catalog_routes.py -> /api/catalog/* public catalog API
   +-- tasks/pdf_tasks.py           -> Celery tasks (generated per tool)
   +-- SEO pages, sitemap, search, nav, related, breadcrumbs, JSON-LD
   +-- (future) homepage cards, mobile app, admin panel
```

**Rule:** nothing imports tool data except `registry.py` (runtime) and
`build.py` (compile). The running app never parses YAML — it reads the compiled
JSON, which is fast and consumable by any language/client.

---

## 2. Data flow of a request

```
POST /api/pdf/compress (multipart)
  -> tool_factory._handle(spec)            spec came from the catalog
     -> check_rate_limit
     -> build_ctx (JobContext)
     -> file_service.save_single/multiple  (10 MB cap, magic-byte checks)
     -> resolve output_ext (+ dynamic output_ext_from)
     -> run_or_enqueue:
          sync  -> Pipeline.run(ctx) -> engine (engines/*.py @register)
          async -> Celery task (tasks.<op>) -> worker -> Pipeline.run -> engine
     -> Result (download_url, size, reduction, ...) or async job_id
```

Every generated route uses this identical pipeline. There is no per-tool handler
code.

---

## 3. Manifest schema (per tool)

```yaml
- <<: *base                 # shared defaults (merge key)
  id: compress_pdf          # unique; also the engine op name
  slug: compress-pdf        # unique; URL + tools.json key
  route: /compress          # path under /api/<module>
  module: pdf               # from meta.module by default
  category: optimize        # must exist in this module's categories
  engine: compress_pdf      # @register(...) name in engines/*.py
  status: draft|published   # published -> also gets an SEO page
  priority: 1               # ordering / sitemap
  # category-system flags (drive the UI): featured, popular, hidden,
  #   experimental, deprecated, subcategory
  search_volume, difficulty, monetization_priority
  icon: file
  aliases: [reduce pdf size]        # extra search terms
  processing: { field, multi, output_ext, output_ext_from, async, max_mb, queue }
  supported_extensions: [pdf]
  output_extensions: [pdf]
  flags: { supports_api, supports_batch, supports_ai, supports_mobile, premium }
  related: [merge-pdf, ...]         # slugs; validated
  guide_links, comparison_links, industry_links
  content:                          # i18n: all human copy per locale
    en:
      display_name, short_description, seo_title, seo_description,
      seo_keywords, h1, intro, what, why, steps[], features[], benefits[],
      use_cases[], security, faqs[], widget_params[], cta_*
```

Module-level (top of each file): `meta: {module, label, icon, order,
manifest_version}` and a `categories:` block (that module's categories).

---

## 4. Build process (`catalog/build.py`)

1. **Auto-discover** every `catalog/manifest/*.yaml` (module names never hardcoded).
2. **Validate — fails the build on:** duplicate slug/id/route, self-referential
   `related`, unknown module/category, invalid slug/route/extension, **missing
   engine** (checked against the live `@register` registry), published tools
   missing translations/SEO fields, non-boolean flags.
3. **Compile** `tools.build.json` (runtime) and `tools.json` (public, search-ready),
   each stamped with `schema_version`, per-module `manifest_version`, build time,
   git sha.
4. **Generate** SEO pages for published tools and the sitemap.

Only `build.py` needs PyYAML; the app reads JSON.

---

## 5. How each system is generated

- **Routes** — `tool_factory.register_catalog_routes(app)` loops
  `registry.get_routes(module)` for every module and registers each route with
  the single `_handle`. Special endpoints (canvas, text-editor, health, download,
  metrics, catalog) stay in their own blueprints.
- **Celery tasks** — `tasks/pdf_tasks.py` generates one `tasks.<op>` per tool and
  builds `PDF_TASK_MAP` in a loop. Names are identical to legacy, so in-flight
  jobs and routing are unaffected.
- **SEO pages** — `build.py` renders `seo/template.html` per published tool
  (unique title/meta/canonical, OG/Twitter, WebApplication + BreadcrumbList +
  FAQPage JSON-LD, content, related links, embedded working widget).
- **Sitemap** — published, non-deprecated tools, priority-ordered.
- **Search** — `registry.get_search_index()` yields `{slug, module, category,
  display_name, keywords}` across every module; `/api/catalog/search` and the
  frontend filter it. Platform-wide by construction.
- **Nav / cards / breadcrumbs** — `registry.get_modules()` returns the nested
  tree (module -> category -> tool cards); the frontend renders from it.

---

## 6. The registry API (`catalog/registry.py`)

Cross-module, read-only: `get_tool(slug)`, `get_module(m)`, `get_category(m,k)`,
`get_featured()`, `get_popular()`, `get_modules()` (nested nav), `get_routes(m)`,
`async_ops(m)`, `get_search_index()`, `get_related(slug)`, `get_sitemap()`,
`published()`, `visible()`, `display_name(slug)`, plus `.version` and `.modules`.

Public HTTP mirror: `/api/catalog`, `/api/catalog/{version,modules,categories,
tools,tools/<slug>,search}` — the same data for web, mobile, integrations, admin.

---

## 7. How to change things

**Add a tool** — add one entry to the module's YAML, `python catalog/build.py`,
redeploy. Its API route, Celery task, SEO page (if published), sitemap entry,
search entry, related links and catalog entry all appear automatically.

**Add a module** — create `catalog/manifest/<module>.yaml` (with `meta.module`
+ `categories` + `tools`), implement the engines (`@register`), `build.py`. The
route factory auto-creates `/api/<module>/...`; the catalog/search/nav include it.
No routing code.

**Add a category** — add a key under a module's `categories:` block and set tools'
`category` to it.

**Add a language** — add `content.<locale>` (and optionally `slugs.<locale>`) to
tools. Structure is unchanged; the generator loops locales and can emit hreflang.

**Admin panel (future)** — enable/disable (`status`), hide (`hidden`), feature
(`featured`), reorder (`priority`), edit SEO/descriptions are all manifest fields.
An admin edits the manifest/JSON and rebuilds — no application code changes.

---

## 8. Guardrails

- `python catalog/build.py` — validates + compiles (fails on bad manifest).
- `python catalog/audit.py` — production readiness: no duplicate tools/routes/
  categories/SEO, no orphaned tools/pages, no broken related/sitemap/search
  entries, engine↔manifest parity, dead route files removed.

Run both before shipping a catalog change.

---

## 9. What is intentionally NOT generated (special cases)

Canvas editor, PDF text-editor round-trip, health, ready/live, metrics, download,
job status, and the catalog API — these bypass the standard tool pipeline and
live in their own blueprints (`pdf_routes.py`, `system_routes.py`,
`catalog_routes.py`).

---

## 10. Directory map

```
catalog/
  manifest/pdf.yaml image.yaml office.yaml   source of truth (YAML)
  build.py     compile + validate + generate
  audit.py     production readiness checks
  registry.py  runtime access (reads tools.build.json)
  util.py      pure helpers (pretty_slug)
  tools.build.json   compiled runtime artifact
  ARCHITECTURE.md    this file
app/routes/
  tool_factory.py    universal route factory + handler (all tool routes)
  pdf_routes.py      SPECIAL pdf endpoints only (canvas, text editor)
  catalog_routes.py  /api/catalog/*
  system_routes.py   download, health, jobs, metrics
seo/template.html    the reusable tool-page template
static/tools.json    public catalog for JS/mobile/API/admin
static/t/<slug>/     generated SEO pages
```
