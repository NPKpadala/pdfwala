# PDFWala catalog — Single Source of Truth

Every tool is defined **exactly once**, in `catalog/manifest/*.yaml`. All other
systems derive from it. Nothing else stores tool information.

## Pipeline

```
                       catalog/manifest/pdf.yaml      <-- authored source (YAML)
                                 |
                        python catalog/build.py       <-- validate + compile
                                 |
        +------------------------+-------------------------------+
        |                        |                               |
 catalog/tools.build.json   static/tools.json         static/t/<slug>/index.html
 (runtime artifact,         (lean public artifact:      (SEO landing pages,
  read by registry.py)       JS / mobile / API / admin)  published tools only)
        |                                                        +
        |                                                 static/sitemap.pdf.xml
        v
 catalog/registry.py  (typed runtime access — stdlib json only, no YAML)
        |
        +-- app/routes/pdf_routes.py   ->  Flask routes + API endpoints (registered in a loop)
        +-- (SEO pages / sitemap)      ->  built above from the same manifest
        +-- future: homepage cards, nav, search, breadcrumbs, mobile app, public API, admin
```

## Dependency rule
`manifest (YAML)  ->  build.py  ->  {tools.build.json, tools.json, SEO pages, sitemap}  ->  registry.py  ->  everything else`

- **Nothing** imports tool data except `registry.py` (runtime) and `build.py` (compile).
- `registry.py` reads only the compiled JSON, so the running app has **no YAML/parse cost** and no build-time dependency.
- The app image bakes `catalog/tools.build.json`; deploy does not run a build step. Run `build.py` when the manifest changes and commit the artifacts.

## Why YAML source -> compiled JSON (not tools.py / raw JSON)
- **tools.py (Python):** rejected. Data-as-code couples the manifest to one runtime and language; a future mobile app, public API, or admin panel cannot read it without executing Python; no enforced schema. Data should not be code.
- **raw tools.json (source):** rejected for authoring — no comments, no multiline strings; hand-writing hundreds of rich entries with FAQs is error-prone. But JSON is the ideal **compiled/runtime** format.
- **tools.yaml (source):** chosen. Comments, multiline copy, anchors/merge-keys for shared defaults, clean diffs, non-dev editable, language-neutral, per-locale files for i18n. Its weaknesses (whitespace, no validation) are erased by `build.py`, which validates and compiles to JSON. Runtime reads the JSON (fast, zero deps, universally consumable).

## Localization (built in, no redesign needed)
Language-neutral fields live at the top level (`id`, `slug`, `route`, `category`,
`engine`, `processing`, `flags`, `related`, ...). All human copy lives under
`content.<locale>`. Today only `content.en` exists. To add a language:
add `content.es` (and optionally `slugs.es`) to a tool — no code or architecture
change. The generator loops locales; hreflang pairs are emitted from the same map.

## Adding a tool = change ONE file
1. Add one entry to `catalog/manifest/pdf.yaml` (merge `<<: *base`, then override
   `id, slug, route, category, engine`, plus `processing` if not a simple sync PDF).
   Set `status: draft` for API-only, or `status: published` with a `content.en`
   block to also get an SEO landing page.
2. Run `python catalog/build.py` (validates + regenerates all artifacts).
3. Rebuild/redeploy.

That single entry automatically produces: the Flask route + API endpoint, the SEO
page (if published), the sitemap entry, related-tool links, breadcrumbs, JSON-LD,
Open Graph, and the public `tools.json` for future consumers. No route file, no
task map, no SEO data file to touch.

> The engine implementation (`@register("...")` in `engines/`) and, for async
> tools, the Celery task in `tasks/pdf_tasks.py` still live with the code they
> run — the manifest's `engine` field references them. Migrating the Celery task
> map and the SPA's homepage cards to also derive from `tools.json` are the next
> planned de-duplications.

## Future modules
`module` distinguishes `pdf`, and later `image`, `office`, `ocr`, `ai`, `video`,
`audio`, etc. Each module gets its own `catalog/manifest/<module>.yaml` with the
same schema; `registry.module("image")` and `route_specs("image")` work unchanged.
Adding a module = add a YAML file + a blueprint that loops `route_specs`.
