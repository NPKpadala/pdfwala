"""
catalog/build.py - compile YAML manifest(s) into runtime artifacts + SEO pages.

    python catalog/build.py

  catalog/manifest/*.yaml   (authored source; one file per MODULE - plugin model)
      -> validate (schema + uniqueness + referential integrity + more)
      -> catalog/tools.build.json   (runtime artifact, read by registry.py)
      -> static/tools.json          (lean public artifact: JS / mobile / API / admin)
      -> static/t/<slug>/index.html (SEO pages for published tools)
      -> static/sitemap.pdf.xml     (published tools + legal pages)

Only build.py needs PyYAML; the running app reads JSON via registry.py.
Build FAILS (exit 1) on any validation error - a broken manifest never ships.
"""
import datetime
import glob
import json
import os
import re
import subprocess
import sys

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from util import pretty_slug  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
STATIC = os.path.join(ROOT, "static")

# Shape version of tools.build.json / tools.json. Bump when the schema changes
# so mobile/API/admin clients can detect an incompatible catalog.
SCHEMA_VERSION = "1.0"

REQUIRED = ["id", "slug", "route", "category", "module", "engine", "processing",
            "icon", "schema_type", "status"]
BOOL_FIELDS = ["featured", "popular", "hidden", "experimental", "deprecated"]
EXT_RE = re.compile(r"^[a-z0-9]{1,6}$")
ROUTE_RE = re.compile(r"^/[a-z0-9][a-z0-9/-]*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load_manifest():
    """Auto-discover every manifest/*.yaml (module names never hardcoded).
    Categories are namespaced PER MODULE (nested nav: module -> category -> tools)."""
    tools, modules = [], {}
    for path in sorted(glob.glob(os.path.join(HERE, "manifest", "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        meta = doc.get("meta") or {}
        mod = meta.get("module") or os.path.splitext(os.path.basename(path))[0]
        modules[mod] = {
            "module": mod,
            "label": meta.get("label", pretty_slug(mod)),
            "icon": meta.get("icon", "file"),
            "order": meta.get("order", 99),
            "manifest_version": meta.get("manifest_version", "0.0.0"),
            "source": os.path.basename(path),
            "categories": doc.get("categories", {}),
        }
        for t in doc.get("tools", []):
            t.setdefault("module", mod)
            tools.append(t)
    return tools, modules


def load_engine_names():
    """Best-effort: the set of registered engine ops, to catch manifest typos.
    Skipped silently if engines can't be imported in this environment."""
    try:
        sys.path.insert(0, ROOT)
        import engines.pdf_engine   # noqa: F401
        import engines.image_engine  # noqa: F401
        import engines.office_engine  # noqa: F401
        from core.pipeline import _ENGINES
        return set(_ENGINES.keys())
    except Exception:
        return None


def validate(tools, modules):
    errs = []
    slugs = {t.get("slug") for t in tools}
    engines = load_engine_names()
    seen_slug, seen_id, seen_route = set(), set(), set()

    for t in tools:
        tid = t.get("id", "?")

        for k in REQUIRED:
            if not t.get(k) and t.get(k) is not False:
                errs.append(f"{tid}: missing required field '{k}'")

        s, i, rkey = t.get("slug"), t.get("id"), (t.get("module"), t.get("route"))
        if s in seen_slug:
            errs.append(f"duplicate slug: {s}")
        if i in seen_id:
            errs.append(f"duplicate id: {i}")
        if rkey in seen_route:
            errs.append(f"duplicate route: {rkey}")
        seen_slug.add(s); seen_id.add(i); seen_route.add(rkey)

        if s and not SLUG_RE.match(s):
            errs.append(f"{tid}: invalid slug '{s}' (lowercase, digits, hyphens)")
        if t.get("route") and not ROUTE_RE.match(t["route"]):
            errs.append(f"{tid}: invalid route '{t.get('route')}'")

        mod = modules.get(t.get("module"))
        if not mod:
            errs.append(f"{tid}: unknown module '{t.get('module')}'")
        elif t.get("category") not in mod.get("categories", {}):
            errs.append(f"{tid}: category '{t.get('category')}' not defined in module "
                        f"'{t.get('module')}'")

        # related: must exist, and a tool must not reference itself (circular)
        for r in t.get("related", []):
            if r not in slugs:
                errs.append(f"{tid}: related '{r}' is not a known tool slug")
            if r == s:
                errs.append(f"{tid}: related references itself (circular)")

        # extensions
        for group in ("supported_extensions", "output_extensions"):
            for e in (t.get(group) or []):
                if not EXT_RE.match(str(e)):
                    errs.append(f"{tid}: invalid extension '{e}' in {group}")

        # output extension: processing.output_ext is the ONLY source of truth
        # (the registry, routes, download names and MIME types all read it)
        if "output_ext" in t:
            errs.append(f"{tid}: top-level 'output_ext' is not allowed — "
                        "set processing.output_ext instead")
        proc = t.get("processing") or {}
        oext = proc.get("output_ext")
        if not oext:
            errs.append(f"{tid}: processing.output_ext is required")
        elif not EXT_RE.match(str(oext)):
            errs.append(f"{tid}: invalid processing.output_ext '{oext}'")
        outs = t.get("output_extensions") or []
        if oext and outs and oext not in outs:
            errs.append(f"{tid}: processing.output_ext '{oext}' not listed in "
                        f"output_extensions {outs}")

        # engine must exist (when we can check)
        if engines is not None and t.get("engine") not in engines:
            errs.append(f"{tid}: engine '{t.get('engine')}' is not registered")

        # boolean category-system fields
        for b in BOOL_FIELDS:
            if b in t and not isinstance(t[b], bool):
                errs.append(f"{tid}: field '{b}' must be true/false")

        # published tools need a complete en translation
        if t.get("status") == "published":
            en = (t.get("content") or {}).get("en")
            if not en:
                errs.append(f"{tid}: status=published but missing content.en")
            else:
                for k in ("seo_title", "seo_description", "h1", "intro"):
                    if not en.get(k):
                        errs.append(f"{tid}: published tool missing content.en.{k}")

    return errs


def git_sha():
    env = os.environ.get("GIT_SHA")
    if env:
        return env.strip()
    try:
        return subprocess.check_output(
            ["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def name_of(slug, by_slug):
    t = by_slug.get(slug) or {}
    return (((t.get("content") or {}).get("en") or {}).get("display_name")
            or pretty_slug(slug))


def render_ctx(t, modules, by_slug):
    c = t["content"]["en"]
    exts = t.get("supported_extensions", ["pdf"])
    cat_label = modules[t["module"]]["categories"][t["category"]]["label"]
    return {
        "slug": t["slug"],
        "category": cat_label,
        "category_slug": t["category"],
        "title": c["seo_title"], "meta_description": c["seo_description"],
        "h1": c["h1"], "intro": c["intro"],
        "what": c["what"], "why": c["why"], "security": c["security"],
        "steps": c["steps"], "features": c["features"],
        "benefits": c["benefits"], "use_cases": c["use_cases"], "faqs": c["faqs"],
        "related": [{"slug": r, "name": name_of(r, by_slug)} for r in t.get("related", [])],
        "widget": {
            "endpoint": "/api/" + t["module"] + t["route"],
            "field": t["processing"].get("field", "file"),
            "multi": bool(t["processing"].get("multi", False)),
            "accept": ",".join("." + e for e in exts),
            "cta": c.get("cta", "Process file"),
            "params": c.get("widget_params", []),
        },
        "cta_h": c.get("cta_h", ""), "cta_p": c.get("cta_p", ""), "cta_btn": c.get("cta_btn", ""),
    }


def public_entry(t):
    en = (t.get("content") or {}).get("en") or {}
    keywords = sorted(set((en.get("seo_keywords") or []) + (t.get("aliases") or [])))
    return {
        "id": t["id"], "slug": t["slug"], "route": t["route"],
        "module": t["module"], "category": t["category"],
        "subcategory": t.get("subcategory"),
        "status": t.get("status"), "priority": t.get("priority", 50),
        "icon": t.get("icon", "file"),
        "featured": bool(t.get("featured")), "popular": bool(t.get("popular")),
        "hidden": bool(t.get("hidden")), "experimental": bool(t.get("experimental")),
        "deprecated": bool(t.get("deprecated")),
        "display_name": en.get("display_name") or pretty_slug(t["slug"]),
        "short_description": en.get("short_description", ""),
        "endpoint": "/api/" + t["module"] + t["route"],
        "field": t["processing"].get("field", "file"),
        "multi": bool(t["processing"].get("multi", False)),
        "accept": ",".join("." + e for e in t.get("supported_extensions", [])),
        "output_ext": t["processing"].get("output_ext"),
        "aliases": t.get("aliases", []),
        "keywords": keywords,
        "flags": t.get("flags", {}),
    }


def main():
    tools, modules = load_manifest()
    errs = validate(tools, modules)
    if errs:
        print("MANIFEST VALIDATION FAILED (%d):" % len(errs))
        for e in errs:
            print("  -", e)
        sys.exit(1)

    by_slug = {t["slug"]: t for t in tools}
    version = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "git_sha": git_sha(),
        "module_versions": {m: modules[m]["manifest_version"] for m in modules},
    }

    # 1. runtime artifact (modules carry their own nested categories)
    build = {"version": version, "modules": modules, "tools": tools}
    with open(os.path.join(HERE, "tools.build.json"), "w", encoding="utf-8") as f:
        json.dump(build, f, ensure_ascii=False, indent=1)

    # 2. lean public artifact (search-ready, all modules)
    pub = {"version": version,
           "modules": modules,
           "tools": [public_entry(t) for t in tools]}
    with open(os.path.join(STATIC, "tools.json"), "w", encoding="utf-8") as f:
        json.dump(pub, f, ensure_ascii=False)

    # 3. SEO pages for published tools
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "seo")),
                      autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    tpl = env.get_template("template.html")
    pages = 0
    for t in tools:
        if t.get("status") != "published":
            continue
        html = tpl.render(t=render_ctx(t, modules, by_slug))
        d = os.path.join(STATIC, "t", t["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        pages += 1

    # 4. sitemap (published, non-deprecated)
    today = datetime.date.today().isoformat()
    urls = ['<url><loc>https://pdf.npkpadala.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>']
    for t in sorted([x for x in tools if x.get("status") == "published" and not x.get("deprecated")],
                    key=lambda x: x.get("priority", 50)):
        urls.append('<url><loc>https://pdf.npkpadala.com/%s/</loc><lastmod>%s</lastmod>'
                    '<changefreq>weekly</changefreq><priority>0.9</priority></url>' % (t["slug"], today))
    for p in ("privacy", "terms", "about", "contact"):
        urls.append('<url><loc>https://pdf.npkpadala.com/%s</loc><changefreq>yearly</changefreq>'
                    '<priority>0.3</priority></url>' % p)
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + "\n".join("  " + u for u in urls) + "\n</urlset>\n")
    with open(os.path.join(STATIC, "sitemap.pdf.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap)

    print("BUILD OK: schema %s | modules %s | %d tools, %d published, sitemap %d urls | sha %s"
          % (SCHEMA_VERSION, ",".join(modules), len(tools), pages, len(urls), version["git_sha"]))


if __name__ == "__main__":
    main()
