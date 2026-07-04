"""
catalog/registry.py - the runtime Single Source of Truth.

Reads the COMPILED manifest (catalog/tools.build.json, produced by build.py from
the YAML source) and exposes typed access. Everything - Flask routes, SEO pages,
sitemap, nav, related links, JSON-LD, future API/mobile/admin - derives from here.

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
        self.tools = data["tools"]
        self.categories = data.get("categories", {})
        self.version = {
            "schema_version": data.get("schema_version"),
            "modules": data.get("modules", {}),
            "generated_at": data.get("generated_at"),
            "git_sha": data.get("git_sha"),
        }
        self.generated_at = data.get("generated_at")
        self._by_slug = {t["slug"]: t for t in self.tools}
        self._by_id = {t["id"]: t for t in self.tools}

    # ---- lookups ----
    def all(self):
        return self.tools

    def by_slug(self, slug):
        return self._by_slug.get(slug)

    def by_id(self, tool_id):
        return self._by_id.get(tool_id)

    def module(self, module):
        return [t for t in self.tools if t.get("module") == module]

    def published(self):
        return [t for t in self.tools if t.get("status") == "published"]

    def visible(self):
        """Tools that should appear in the UI (not hidden, not deprecated)."""
        return [t for t in self.tools
                if not t.get("hidden") and not t.get("deprecated")]

    def featured(self):
        return [t for t in self.visible() if t.get("featured")]

    def popular(self):
        return [t for t in self.visible() if t.get("popular")]

    def in_category(self, category):
        return [t for t in self.tools if t.get("category") == category]

    def grouped_by_category(self):
        """Ordered {category_key: {meta, tools[]}} for nav / grid rendering."""
        out = {}
        for key in sorted(self.categories, key=lambda k: self.categories[k].get("order", 99)):
            out[key] = {"meta": self.categories[key],
                        "tools": [t for t in self.visible() if t.get("category") == key]}
        return out

    def display_name(self, slug):
        t = self._by_slug.get(slug) or {}
        name = (((t.get("content") or {}).get("en") or {}).get("display_name"))
        return name or pretty_slug(slug)

    # ---- derived contracts ----
    def route_specs(self, module="pdf"):
        """One spec per tool route so the Flask blueprint can register them in a
        loop instead of a hand-maintained list."""
        specs = []
        for t in self.tools:
            if t.get("module") != module:
                continue
            p = t.get("processing", {})
            specs.append({
                "route":       t["route"],
                "op":          t["engine"],
                "output_ext":  p.get("output_ext", "pdf"),
                "multi":       bool(p.get("multi", False)),
                "field":       p.get("field", "file"),
                "success_msg": t.get("success_msg", "Done"),
                "force_async": bool(p.get("async", False)),
            })
        return specs

    def async_ops(self, module="pdf"):
        return {t["engine"] for t in self.module(module)
                if t.get("processing", {}).get("async")}


registry = Registry()
