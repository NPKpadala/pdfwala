"""
catalog/build.py - compile the YAML manifest into runtime artifacts + SEO pages.

    python catalog/build.py

Pipeline:
  catalog/manifest/*.yaml   (authored source, human-friendly)
      -> validate (schema + uniqueness + referential integrity)
      -> catalog/tools.build.json   (runtime artifact, read by registry.py)
      -> static/tools.json          (lean public artifact: JS / mobile / API / admin)
      -> static/t/<slug>/index.html (SEO pages for published tools)
      -> static/sitemap.pdf.xml     (published tools + legal pages)

Only build.py needs PyYAML; the running app reads JSON via registry.py.
"""
import datetime
import glob
import json
import os
import sys

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from util import pretty_slug  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
STATIC = os.path.join(ROOT, "static")

REQUIRED = ["id", "slug", "route", "category", "module", "engine", "processing"]


def load_manifest():
    tools, categories = [], {}
    for path in sorted(glob.glob(os.path.join(HERE, "manifest", "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        tools.extend(doc.get("tools", []))
        categories.update(doc.get("categories", {}))
    return tools, categories


def validate(tools, categories):
    errs = []
    slugs = {t.get("slug") for t in tools}
    seen_slug, seen_id, seen_route = set(), set(), set()
    for t in tools:
        tid = t.get("id", "?")
        for k in REQUIRED:
            if k not in t:
                errs.append(f"{tid}: missing required field '{k}'")
        s, i, key = t.get("slug"), t.get("id"), (t.get("module"), t.get("route"))
        if s in seen_slug:
            errs.append(f"duplicate slug: {s}")
        if i in seen_id:
            errs.append(f"duplicate id: {i}")
        if key in seen_route:
            errs.append(f"duplicate route: {key}")
        seen_slug.add(s); seen_id.add(i); seen_route.add(key)
        if t.get("category") not in categories:
            errs.append(f"{tid}: unknown category '{t.get('category')}'")
        for r in t.get("related", []):
            if r not in slugs:
                errs.append(f"{tid}: related '{r}' is not a known tool slug")
        if t.get("status") == "published" and "en" not in (t.get("content") or {}):
            errs.append(f"{tid}: status=published but no content.en block")
    return errs


def name_of(slug, by_slug):
    t = by_slug.get(slug) or {}
    return (((t.get("content") or {}).get("en") or {}).get("display_name")
            or pretty_slug(slug))


def render_ctx(t, categories, by_slug):
    """Flatten a tool + locale into the shape seo/template.html expects, so the
    template stays untouched (zero-regression migration)."""
    c = t["content"]["en"]
    exts = t.get("supported_extensions", ["pdf"])
    return {
        "slug": t["slug"],
        "category": categories[t["category"]]["label"],
        "category_slug": t["category"],
        "title": c["seo_title"],
        "meta_description": c["seo_description"],
        "h1": c["h1"],
        "intro": c["intro"],
        "what": c["what"], "why": c["why"], "security": c["security"],
        "steps": c["steps"], "features": c["features"],
        "benefits": c["benefits"], "use_cases": c["use_cases"], "faqs": c["faqs"],
        "related": [{"slug": r, "name": name_of(r, by_slug)} for r in t.get("related", [])],
        "widget": {
            "endpoint": "/api/pdf" + t["route"],
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
    return {
        "id": t["id"], "slug": t["slug"], "route": t["route"],
        "category": t["category"], "module": t["module"],
        "status": t.get("status"), "priority": t.get("priority", 50),
        "display_name": en.get("display_name") or pretty_slug(t["slug"]),
        "short_description": en.get("short_description", ""),
        "flags": t.get("flags", {}),
    }


def main():
    tools, categories = load_manifest()
    errs = validate(tools, categories)
    if errs:
        print("MANIFEST VALIDATION FAILED:")
        for e in errs:
            print("  -", e)
        sys.exit(1)

    by_slug = {t["slug"]: t for t in tools}

    # 1. runtime artifact
    build = {"generated_at": datetime.datetime.utcnow().isoformat() + "Z",
             "tools": tools, "categories": categories}
    with open(os.path.join(HERE, "tools.build.json"), "w", encoding="utf-8") as f:
        json.dump(build, f, ensure_ascii=False, indent=1)

    # 2. lean public artifact
    pub = {"tools": [public_entry(t) for t in tools], "categories": categories}
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
        html = tpl.render(t=render_ctx(t, categories, by_slug))
        d = os.path.join(STATIC, "t", t["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        pages += 1

    # 4. sitemap
    today = datetime.date.today().isoformat()
    urls = ['<url><loc>https://pdf.npkpadala.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>']
    for t in sorted([x for x in tools if x.get("status") == "published"],
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

    print("BUILD OK: %d tools total, %d published (SEO pages), sitemap %d urls"
          % (len(tools), pages, len(urls)))


if __name__ == "__main__":
    main()
