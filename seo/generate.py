# -*- coding: utf-8 -*-
"""Static SEO page generator. Run: python seo/generate.py
Writes static/t/<slug>/index.html for every tool in data.TOOLS and rebuilds the
sitemap (only real content pages are listed - thin SPA shells are excluded)."""
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(__file__))
import data  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.normpath(os.path.join(HERE, "..", "static"))

env = Environment(
    loader=FileSystemLoader(HERE),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True, lstrip_blocks=True,
)
tpl = env.get_template("template.html")

count = 0
for t in data.TOOLS:
    html = tpl.render(t=t)
    outdir = os.path.join(STATIC, "t", t["slug"])
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    count += 1

today = datetime.date.today().isoformat()
urls = ['<url><loc>https://pdf.npkpadala.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>']
for t in data.TOOLS:
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

print("generated %d tool pages + sitemap (%d urls)" % (count, len(urls)))
