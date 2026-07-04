"""
catalog/registry.py — the runtime Single Source of Truth (all modules).

Reads the COMPILED catalog (catalog/tools.build.json) and exposes one unified,
cross-module API. Everything — Flask routes, SEO pages, sitemap, nav, search,
JSON-LD, and the future public API / mobile app / admin panel — derives here.

Runtime dependency: stdlib json only (no YAML), so it is fast and portable.
"""
import json
import os

from catalog.util import pretty_slug

_HERE = os.path.dirname(os.path.abspath(__file__))
_BUILD = os.path.join(_HERE, "tools.build.json")


class Registry:
    def __init__(self, path: str = _BUILD):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.version = data.get("version", {})
        self.modules = data.get("modules", {})       # {mod: {label, icon, order, categories}}
        self.tools = data.get("tools", [])
        self._by_slug = {t["slug"]: t for t in self.tools}
        self._by_id = {t["id"]: t for t in self.tools}

    # ── single-tool / lookups ───────────────────────────────────────────────
    def get_tool(self, slug):
        return self._by_slug.get(slug)

    by_slug = get_tool                       # aliases (back-compat)

    def by_id(self, tool_id):
        return self._by_id.get(tool_id)

    def all(self):
        return self.tools

    def get_module(self, module):
        """Module metadata + its tools, or None."""
        m = self.modules.get(module)
        if not m:
            return None
        return dict(m, tools=self.module(module))

    def module(self, module):
        return [t for t in self.tools if t.get("module") == module]

    def get_category(self, module, key):
        return (self.modules.get(module, {}).get("categories", {}) or {}).get(key)

    # ── UI collections (cross-module) ───────────────────────────────────────
    def published(self):
        return [t for t in self.tools if t.get("status") == "published"]

    def visible(self):
        return [t for t in self.tools
                if not t.get("hidden") and not t.get("deprecated")]

    def get_featured(self):
        return [t for t in self.visible() if t.get("featured")]

    def get_popular(self):
        return [t for t in self.visible() if t.get("popular")]

    def get_modules(self):
        """Ordered nested navigation: modules -> categories -> tools.
        This single structure drives the whole nav/homepage."""
        out = []
        for mkey in sorted(self.modules, key=lambda k: self.modules[k].get("order", 99)):
            m = self.modules[mkey]
            cats = m.get("categories", {}) or {}
            cat_list = []
            for ckey in sorted(cats, key=lambda k: cats[k].get("order", 99)):
                tools = [t for t in self.visible()
                         if t.get("module") == mkey and t.get("category") == ckey]
                if tools:
                    cat_list.append({"key": ckey, **cats[ckey],
                                     "tools": [self._card(t) for t in tools]})
            out.append({"key": mkey, "label": m.get("label"), "icon": m.get("icon"),
                        "order": m.get("order"), "categories": cat_list})
        return out

    def _card(self, t):
        en = (t.get("content") or {}).get("en") or {}
        return {"slug": t["slug"], "display_name": en.get("display_name") or pretty_slug(t["slug"]),
                "icon": t.get("icon"), "module": t["module"], "category": t["category"],
                "featured": bool(t.get("featured")), "popular": bool(t.get("popular"))}

    # ── routes (cross-module) ───────────────────────────────────────────────
    def get_routes(self, module=None):
        specs = []
        for t in self.tools:
            if module and t.get("module") != module:
                continue
            p = t.get("processing", {})
            specs.append({
                "module":      t["module"],
                "route":       t["route"],
                "op":          t["engine"],
                "output_ext":  p.get("output_ext", "pdf"),
                "output_ext_from": p.get("output_ext_from"),
                "multi":       bool(p.get("multi", False)),
                "field":       p.get("field", "file"),
                "success_msg": t.get("success_msg", "Done"),
                "force_async": bool(p.get("async", False)),
            })
        return specs

    route_specs = get_routes                 # back-compat name used by pdf_routes.py

    def async_ops(self, module=None):
        return {t["engine"] for t in self.tools
                if (module is None or t.get("module") == module)
                and t.get("processing", {}).get("async")}

    # ── search / related / sitemap ──────────────────────────────────────────
    def get_search_index(self):
        idx = []
        for t in self.visible():
            en = (t.get("content") or {}).get("en") or {}
            idx.append({
                "slug": t["slug"], "module": t["module"], "category": t["category"],
                "display_name": en.get("display_name") or pretty_slug(t["slug"]),
                "keywords": sorted(set((en.get("seo_keywords") or []) + (t.get("aliases") or []))),
            })
        return idx

    def display_name(self, slug):
        t = self._by_slug.get(slug) or {}
        return (((t.get("content") or {}).get("en") or {}).get("display_name")
                or pretty_slug(slug))

    def get_related(self, slug):
        t = self._by_slug.get(slug) or {}
        return [{"slug": r, "name": self.display_name(r)} for r in t.get("related", [])]

    def get_sitemap(self):
        """Published, non-deprecated tools for the sitemap, priority-ordered."""
        pub = [t for t in self.tools
               if t.get("status") == "published" and not t.get("deprecated")]
        pub.sort(key=lambda x: x.get("priority", 50))
        return [{"slug": t["slug"], "priority": t.get("priority", 50)} for t in pub]


registry = Registry()
